#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║          YT-DLP Telegram Bot             ║
║   Download videos & audio from anywhere  ║
╚══════════════════════════════════════════╝
"""

import os
import re
import asyncio
import logging
import shutil
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.helpers import escape_markdown

import yt_dlp


# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000"))

_default_dl = Path.home() / "Downloads" / "ytdlp_bot"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", _default_dl))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("ytdlp-bot")


# ─────────────────────────────────────────
# Video formats (H.264 preferred, max 1080p)
# ─────────────────────────────────────────
VIDEO_FORMATS = [
    ("1080p HD", "bestvideo[vcodec!*=hevc][height<=1080]+bestaudio/best[height<=1080]", "mp4"),
    ("720p HD",  "bestvideo[vcodec!*=hevc][height<=720]+bestaudio/best[height<=720]", "mp4"),
    ("480p SD",  "bestvideo[vcodec!*=hevc][height<=480]+bestaudio/best[height<=480]", "mp4"),
    ("360p SD",  "bestvideo[vcodec!*=hevc][height<=360]+bestaudio/best[height<=360]", "mp4"),
]

AUDIO_FORMATS = [
    ("MP3 320", "mp3", "320"),
    ("MP3 192", "mp3", "192"),
    ("M4A best", "m4a", "0"),
    ("OPUS best", "opus", "0"),
]


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
URL_RE = re.compile(r"https?://[^\s]+")


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def format_size(b: int | None) -> str:
    if not b:
        return "? MB"
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


def sanitize(name: str, n: int = 64) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name)[:n].strip()


def progress_bar(pct: float, w: int = 14) -> str:
    filled = int(w * pct / 100)
    return "[" + "█" * filled + "░" * (w - filled) + f"] {pct:.0f}%"



# ─────────────────────────────────────────
# Progress
# ─────────────────────────────────────────
class Progress:
    def __init__(self):
        self.pct = 0.0
        self.speed = ""
        self.eta = ""

    def hook(self, d: dict):
        if d["status"] == "downloading":
            pct = d.get("_percent_str", "0%").replace("%", "").replace(",", ".")
            try:
                self.pct = float(pct)
            except:
                pass
            self.speed = d.get("_speed_str", "")
            self.eta = d.get("_eta_str", "")


# ─────────────────────────────────────────
# yt-dlp core
# ─────────────────────────────────────────
async def _do_download(url: str, opts: dict) -> Path | None:
    before = set(DOWNLOAD_DIR.iterdir())
    loop = asyncio.get_event_loop()

    def run():
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])

    try:
        await loop.run_in_executor(None, run)

        after = list(DOWNLOAD_DIR.iterdir())
        files = [
            f for f in after
            if f not in before and f.is_file() and not f.name.endswith((".part", ".ytdl"))
        ]

        return max(files, key=lambda f: f.stat().st_mtime) if files else None

    except Exception as e:
        logger.error(e)
        return None


async def download_video(url: str, prog: Progress):
    opts = {
        "format": "bestvideo[vcodec!*=hevc][height<=1080]+bestaudio/best[height<=1080]",
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",

        "concurrent_fragment_downloads": 4,
        "retries": 10,
        "continuedl": True,

        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],

        "postprocessor_args": [
            "-c:v", "libx264",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
        ],

        "progress_hooks": [prog.hook],
        "quiet": True,
    }

    return await _do_download(url, opts)


async def download_audio(url: str, prog: Progress):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        "progress_hooks": [prog.hook],
        "quiet": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    return await _do_download(url, opts)


# ─────────────────────────────────────────
# Telegram handlers
# ─────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a link.")


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = extract_url(update.message.text)
    if not url:
        return

    prog = Progress()

    msg = await update.message.reply_text("Downloading...")

    async def ticker():
        while True:
            await asyncio.sleep(2)
            try:
                await msg.edit_text(
                    f"⬇️ {progress_bar(prog.pct)} {prog.speed} ETA {prog.eta}"
                )
            except:
                pass

    task = asyncio.create_task(ticker())

    fp = await download_video(url, prog)

    task.cancel()

    if not fp:
        await msg.edit_text("Failed.")
        return

    size = fp.stat().st_size

    await msg.edit_text(f"Uploading {format_size(size)}")

    with open(fp, "rb") as f:
        await update.message.reply_video(f, supports_streaming=True)

    try:
        fp.unlink()
    except:
        pass

    await msg.edit_text("Done!")


# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("Set BOT_TOKEN")
        return

    if not shutil.which("ffmpeg"):
        print("FFmpeg missing")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("Bot running")
    app.run_polling()


if __name__ == "__main__":
    main()