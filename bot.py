"""
APK Builder Bot
টেলিগ্রামে Android প্রজেক্টের .zip পাঠান → GitHub Actions-এ বিল্ড ও সাইন হয় → signed release APK ফেরত পাবেন।
"""
import asyncio
import base64
import io
import logging
import os
import re
import secrets
import time
import zipfile
from pathlib import Path

import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ───────────────────────── CONFIG ─────────────────────────
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN", "8665718016:AAESRaJr7C_hWNQaNY8XiHRsVnoGzqv3lcE"
).strip()
# GitHub টোকেন এখানে বসবে (classic token, scope: repo + workflow)
GH_TOKEN = os.environ.get("GH_TOKEN", "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").strip()
GH_REPO_NAME = os.environ.get("GH_REPO_NAME", "apk-builder").strip()

def _parse_ids(raw: str) -> set[int]:
    return {int(p) for p in raw.replace(" ", "").split(",") if p.lstrip("-").isdigit()}


ALLOWED_CHAT_IDS = _parse_ids(os.environ.get("ALLOWED_CHAT_IDS", "7935895229"))

MAX_ZIP_BYTES = 20 * 1024 * 1024  # টেলিগ্রাম বট ডাউনলোড লিমিট
FIND_RUN_TRIES = 30  # x5s
RUN_POLL_INTERVAL = 10
MAX_BUILD_SECONDS = 45 * 60

FILES_DIR = Path(__file__).parent / "builder_files"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("apkbot")


class BuildError(Exception):
    pass


# ───────────────────────── GITHUB CLIENT ─────────────────────────
class GitHub:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self.c = client
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.repo = ""  # owner/name
        self.branch = "main"

    async def req(self, method: str, path: str, **kw) -> httpx.Response:
        url = path if path.startswith("http") else f"https://api.github.com{path}"
        last_exc = None
        for _ in range(3):
            try:
                return await self.c.request(method, url, headers=self.headers, **kw)
            except httpx.HTTPError as e:
                last_exc = e
                await asyncio.sleep(2)
        raise BuildError(f"GitHub কানেকশন সমস্যা: {last_exc!r}")

    # ---- repo bootstrap ----
    async def ensure_repo(self) -> str:
        r = await self.req("GET", "/user")
        if r.status_code != 200:
            raise BuildError(
                f"GH_TOKEN কাজ করছে না (HTTP {r.status_code}). নতুন টোকেন লাগবে (scope: repo + workflow)। bot.py-তে GH_TOKEN লাইনে বসান।"
            )
        login = r.json()["login"]
        self.repo = f"{login}/{GH_REPO_NAME}"

        r = await self.req("GET", f"/repos/{self.repo}")
        if r.status_code == 404:
            r = await self.req(
                "POST",
                "/user/repos",
                json={
                    "name": GH_REPO_NAME,
                    "private": True,
                    "auto_init": True,
                    "description": "APK builder (managed by Telegram bot)",
                },
            )
            if r.status_code not in (200, 201):
                raise BuildError(f"রিপো বানানো যায়নি: {r.status_code} {r.text[:200]}")
            await asyncio.sleep(3)
            r = await self.req("GET", f"/repos/{self.repo}")
        if r.status_code != 200:
            raise BuildError(f"রিপো পাওয়া যায়নি: {r.status_code} {r.text[:200]}")
        self.branch = r.json().get("default_branch", "main")
        return self.repo

    async def ensure_file(self, path: str, content: bytes, overwrite: bool):
        r = await self.req(
            "GET", f"/repos/{self.repo}/contents/{path}", params={"ref": self.branch}
        )
        sha = None
        if r.status_code == 200:
            info = r.json()
            sha = info["sha"]
            if not overwrite:
                return
            existing = base64.b64decode(info.get("content", "").replace("\n", ""))
            if existing == content:
                return
        elif r.status_code != 404:
            raise BuildError(f"{path} চেক ফেইল: {r.status_code} {r.text[:200]}")

        body = {
            "message": f"bot: update {path}",
            "content": base64.b64encode(content).decode(),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        r = await self.req("PUT", f"/repos/{self.repo}/contents/{path}", json=body)
        if r.status_code not in (200, 201):
            hint = ""
            if r.status_code in (403, 404) and "workflow" in path:
                hint = " (টোকেনে `workflow` scope দিন)"
            raise BuildError(f"{path} আপলোড ফেইল: {r.status_code} {r.text[:200]}{hint}")

    async def setup(self) -> str:
        await self.ensure_repo()
        await self.ensure_file(
            ".github/workflows/build.yml", (FILES_DIR / "build.yml").read_bytes(), True
        )
        await self.ensure_file(
            "keystore/release.jks.b64", (FILES_DIR / "release.jks.b64").read_bytes(), False
        )
        await self.ensure_file(
            "keystore/keystore.properties",
            (FILES_DIR / "keystore.properties").read_bytes(),
            False,
        )
        return self.repo

    # ---- build flow ----
    async def upload_job(self, job_id: str, data: bytes) -> str:
        r = await self.req(
            "PUT",
            f"/repos/{self.repo}/contents/jobs/{job_id}.zip",
            json={
                "message": f"job {job_id}",
                "content": base64.b64encode(data).decode(),
                "branch": self.branch,
            },
            timeout=180,
        )
        if r.status_code not in (200, 201):
            raise BuildError(f"জিপ আপলোড ফেইল: {r.status_code} {r.text[:200]}")
        return r.json()["content"]["sha"]

    async def delete_job(self, job_id: str, sha: str):
        try:
            await self.req(
                "DELETE",
                f"/repos/{self.repo}/contents/jobs/{job_id}.zip",
                json={"message": f"cleanup {job_id}", "sha": sha, "branch": self.branch},
            )
        except Exception as e:
            log.warning("cleanup failed: %r", e)

    async def dispatch(self, job_id: str):
        r = await self.req(
            "POST",
            f"/repos/{self.repo}/actions/workflows/build.yml/dispatches",
            json={"ref": self.branch, "inputs": {"job_id": job_id}},
        )
        if r.status_code != 204:
            raise BuildError(f"বিল্ড চালু করা যায়নি: {r.status_code} {r.text[:200]}")

    async def find_run(self, job_id: str) -> int:
        title = f"build {job_id}"
        for _ in range(FIND_RUN_TRIES):
            await asyncio.sleep(5)
            r = await self.req(
                "GET",
                f"/repos/{self.repo}/actions/workflows/build.yml/runs",
                params={"event": "workflow_dispatch", "per_page": 20},
            )
            if r.status_code == 200:
                for run in r.json().get("workflow_runs", []):
                    if run.get("display_title") == title:
                        return run["id"]
        raise BuildError("GitHub Actions রান খুঁজে পাওয়া যায়নি।")

    async def wait_run(self, run_id: int, progress) -> str:
        start = time.time()
        while True:
            r = await self.req("GET", f"/repos/{self.repo}/actions/runs/{run_id}")
            if r.status_code == 200:
                run = r.json()
                if run["status"] == "completed":
                    return run.get("conclusion") or "failure"
            elapsed = int(time.time() - start)
            if elapsed > MAX_BUILD_SECONDS:
                raise BuildError("বিল্ড ৪৫ মিনিটের বেশি লাগছে, টাইমআউট।")
            await progress(f"⚙️ বিল্ড চলছে... {elapsed // 60}m {elapsed % 60}s")
            await asyncio.sleep(RUN_POLL_INTERVAL)

    async def fetch_apks(self, run_id: int, job_id: str) -> list[tuple[str, bytes]]:
        r = await self.req("GET", f"/repos/{self.repo}/actions/runs/{run_id}/artifacts")
        arts = [
            a
            for a in r.json().get("artifacts", [])
            if a["name"] == f"apk-{job_id}" and not a.get("expired")
        ]
        if not arts:
            raise BuildError("বিল্ড সফল, কিন্তু APK আর্টিফ্যাক্ট পাওয়া যায়নি।")
        r = await self.req("GET", arts[0]["archive_download_url"], timeout=180)
        if r.status_code != 200:
            raise BuildError(f"APK ডাউনলোড ফেইল: {r.status_code}")
        out = []
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for n in z.namelist():
                if n.lower().endswith(".apk"):
                    out.append((os.path.basename(n), z.read(n)))
        if not out:
            raise BuildError("আর্টিফ্যাক্টে কোনো APK নেই।")
        return out

    async def log_tail(self, run_id: int) -> str:
        for _ in range(4):
            r = await self.req("GET", f"/repos/{self.repo}/actions/runs/{run_id}/logs", timeout=120)
            if r.status_code == 200:
                try:
                    z = zipfile.ZipFile(io.BytesIO(r.content))
                    steps = sorted(
                        n for n in z.namelist() if "/" in n and n.endswith(".txt")
                    )
                    chosen = ""
                    for n in steps:
                        txt = z.read(n).decode("utf-8", "ignore")
                        if "##[error]" in txt:
                            chosen = txt
                            break
                    if not chosen and steps:
                        chosen = z.read(steps[-1]).decode("utf-8", "ignore")
                    lines = [
                        re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", ln)
                        for ln in chosen.splitlines()
                    ]
                    return "\n".join(lines)[-3000:]
                except Exception as e:
                    log.warning("log parse failed: %r", e)
                    return ""
            await asyncio.sleep(3)
        return ""


# ───────────────────────── HANDLERS ─────────────────────────
def allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_chat.id):
        return await update.effective_message.reply_text("⛔ আপনার অনুমতি নেই।")
    await update.effective_message.reply_text(
        "👋 APK Builder Bot\n\n"
        "আপনার Android Studio প্রজেক্টের .zip ফাইল এখানে পাঠান, আমি signed release APK বানিয়ে দেব।\n\n"
        "📌 নিয়ম:\n"
        "• জিপ ২০MB-এর কম হতে হবে (build, .gradle, .idea ফোল্ডার বাদ দিয়ে জিপ করুন)\n"
        "• প্রজেক্টে settings.gradle থাকতে হবে\n"
        "• বিল্ডে সাধারণত ৩–১০ মিনিট লাগে\n\n"
        "/keystore – সাইনিং কীস্টোর ফাইল ও পাসওয়ার্ড ব্যাকআপ নিন"
    )


async def keystore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_chat.id):
        return
    props = (FILES_DIR / "keystore.properties").read_text()
    pw = re.search(r"KS_PASS=(.+)", props).group(1).strip()
    data = base64.b64decode((FILES_DIR / "release.jks.b64").read_text())
    await update.effective_message.reply_document(
        document=data,
        filename="release.jks",
        caption=f"🔑 Keystore (PKCS12)\nAlias: release\nPassword: {pw}\n\nএটা সংরক্ষণ করুন। Play Store আপডেটে এই একই কী লাগবে।",
    )


async def handle_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not allowed(update.effective_chat.id):
        return await msg.reply_text("⛔ আপনার অনুমতি নেই।")

    doc = msg.document
    if not doc or not (doc.file_name or "").lower().endswith(".zip"):
        return await msg.reply_text("❌ শুধু .zip ফাইল পাঠান।")
    if doc.file_size and doc.file_size > MAX_ZIP_BYTES:
        return await msg.reply_text(
            f"❌ জিপ {doc.file_size / 1048576:.1f}MB — টেলিগ্রাম বটের লিমিট ২০MB। "
            "build, .gradle, .idea ফোল্ডার বাদ দিয়ে আবার জিপ করুন।"
        )

    gh: GitHub = context.application.bot_data["gh"]
    status = await msg.reply_text("📥 জিপ নামানো হচ্ছে...")
    last_text = {"v": ""}

    async def progress(text: str):
        if text == last_text["v"]:
            return
        last_text["v"] = text
        try:
            await status.edit_text(text)
        except Exception:
            pass

    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    sha = None
    try:
        if not gh.repo:
            await gh.setup()
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
            if not any(
                n.endswith(("settings.gradle", "settings.gradle.kts")) for n in names
            ):
                raise BuildError(
                    "জিপে settings.gradle পাওয়া যায়নি। এটা Android Gradle প্রজেক্টের জিপ নয়।"
                )
        except zipfile.BadZipFile:
            raise BuildError("জিপ ফাইলটি নষ্ট বা পড়া যাচ্ছে না।")

        await progress("☁️ GitHub-এ আপলোড হচ্ছে...")
        sha = await gh.upload_job(job_id, data)
        await gh.dispatch(job_id)
        await progress("🚀 বিল্ড কিউতে দেওয়া হয়েছে...")
        run_id = await gh.find_run(job_id)
        conclusion = await gh.wait_run(run_id, progress)

        if conclusion == "success":
            await progress("📦 APK নামানো হচ্ছে...")
            apks = await gh.fetch_apks(run_id, job_id)
            for name, content in apks:
                await msg.reply_document(
                    document=content,
                    filename=name,
                    caption=f"✅ {name}\n{len(content) / 1048576:.1f} MB • signed release",
                    read_timeout=180,
                    write_timeout=180,
                )
            try:
                await status.delete()
            except Exception:
                pass
        else:
            tail = await gh.log_tail(run_id)
            text = f"❌ বিল্ড ফেইল ({conclusion})"
            if tail:
                text += f"\n\n{tail}"
            await status.edit_text(text[:4000])
    except BuildError as e:
        await progress(f"❌ {e}")
    except Exception:
        log.exception("unexpected error")
        await progress("❌ অপ্রত্যাশিত সমস্যা হয়েছে। Railway লগ দেখুন।")
    finally:
        if sha:
            await gh.delete_job(job_id, sha)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception", exc_info=context.error)


# ───────────────────────── LIFECYCLE ─────────────────────────
async def post_init(app):
    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True)
    gh = GitHub(client, GH_TOKEN)
    app.bot_data["http"] = client
    app.bot_data["gh"] = gh

    me = await app.bot.get_me()
    log.info("Bot started as @%s", me.username)

    try:
        repo = await gh.setup()
        note = f"✅ APK Builder অনলাইন।\nGitHub রিপো: {repo} (private)\nজিপ পাঠান, APK নিন।"
    except BuildError as e:
        log.error("GitHub setup failed: %s", e)
        note = f"⚠️ বট চালু হয়েছে, কিন্তু GitHub সেটআপ ফেইল:\n{e}"
    except Exception as e:
        log.exception("GitHub setup crashed")
        note = f"⚠️ GitHub সেটআপে সমস্যা: {e!r}"

    for cid in ALLOWED_CHAT_IDS:
        try:
            await app.bot.send_message(cid, note)
        except Exception:
            pass
        break


async def post_shutdown(app):
    client = app.bot_data.get("http")
    if client:
        await client.aclose()


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("keystore", keystore_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_zip))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
