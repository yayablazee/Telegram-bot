#!/usr/bin/env python3
import os
import logging
import sqlite3
import tempfile
import subprocess
from datetime import datetime
from urllib.parse import urlparse

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, CallbackQueryHandler,
    Filters, CallbackContext
)

# ------------------ CONFIG ------------------
TOKEN = os.getenv("BOT_TOKEN")
# Optional: external downloader API for Snapchat/IG/TT/FB if you prefer (set in Render env)
DOWNLOADER_API = os.getenv("DOWNLOADER_API", "https://api.ryzendesu.com/download?url=")
# path for sqlite db
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
# path to yt-dlp binary if using system/venv version; yt-dlp will be installed in venv
YTDLP = "yt-dlp"

# ------------------ LOGGING ------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------ DATABASE ------------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    joined_at TEXT
                )""")
    c.execute("""CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    url TEXT,
                    platform TEXT,
                    action TEXT,
                    result TEXT,
                    timestamp TEXT
                )""")
    conn.commit()
    return conn

DB = init_db()

def add_user(user):
    c = DB.cursor()
    c.execute("SELECT id FROM users WHERE id = ?", (user.id,))
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO users (id, username, first_name, last_name, joined_at) VALUES (?, ?, ?, ?, ?)",
            (user.id, user.username or "", user.first_name or "", user.last_name or "", datetime.utcnow().isoformat())
        )
        DB.commit()

def log_action(user_id, url, platform, action, result):
    c = DB.cursor()
    c.execute(
        "INSERT INTO logs (user_id, url, platform, action, result, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, url, platform, action, result, datetime.utcnow().isoformat())
    )
    DB.commit()

# ------------------ UTIL ------------------
def detect_platform(url: str) -> str:
    u = url.lower()
    if "tiktok.com" in u or "vt.tiktok" in u:
        return "tiktok"
    if "instagram.com" in u or "instagr.am" in u:
        return "instagram"
    if "facebook.com" in u or "fb.watch" in u:
        return "facebook"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "snapchat.com" in u or "snapchat" in u:
        return "snapchat"
    return "unknown"

# ------------------ DOWNLOADERS ------------------
def download_via_api(url: str):
    """
    Use an external downloader API as a fallback (for TT/IG/FB/Snapchat).
    The API should return JSON with {"status":"success","result":{"url":"direct_download_link"}}
    """
    try:
        api_url = f"{DOWNLOADER_API}{url}"
        r = requests.get(api_url, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "success" and data.get("result") and data["result"].get("url"):
            return data["result"]["url"]
        return None
    except Exception as e:
        logger.exception("API download failed: %s", e)
        return None

def download_with_ytdlp(url: str, audio_only=False):
    """
    Use yt-dlp to download the media to a temporary file.
    Returns path to file or None.
    """
    try:
        tmpdir = tempfile.mkdtemp(prefix="dl_")
        out_template = os.path.join(tmpdir, "%(title).50s.%(ext)s")
        cmd = [YTDLP, "--no-warnings", "-o", out_template]
        if audio_only:
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        # limit rate and filesize if you want (optional)
        cmd.append(url)
        logger.info("Running yt-dlp: %s", " ".join(cmd))
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if proc.returncode != 0:
            logger.error("yt-dlp failed: %s", proc.stderr.decode("utf-8", errors="ignore"))
            return None
        # find the downloaded file
        for root, dirs, files in os.walk(tmpdir):
            for f in files:
                return os.path.join(root, f)
        return None
    except Exception as e:
        logger.exception("yt-dlp exception: %s", e)
        return None

# ------------------ TELEGRAM HANDLERS ------------------
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    add_user(user)
    keyboard = [
        [InlineKeyboardButton("Download Video", callback_data="dl_video")],
        [InlineKeyboardButton("Download Audio (MP3)", callback_data="dl_audio")],
        [InlineKeyboardButton("Remove Watermark", callback_data="remove_wm")],
        [InlineKeyboardButton("YouTube Search", callback_data="yt_search")],
        [InlineKeyboardButton("Show Your Stats", callback_data="stats")],
        [InlineKeyboardButton("About", callback_data="about")]
    ]
    update.message.reply_text(
        "📥 *Downloader Bot Ready*\n\nSend a supported link (TikTok, Instagram, Facebook, YouTube, Snapchat) or use the menu below.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def menu_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data
    if data in ("dl_video", "dl_audio", "remove_wm"):
        query.edit_message_text("Send the link you want to process (the bot will use the selected action).")
        # store state
        context.user_data["action"] = data
    elif data == "yt_search":
        query.edit_message_text("Send search query for YouTube (e.g., 'lofi hip hop beats').")
        context.user_data["action"] = "yt_search"
    elif data == "stats":
        uid = query.from_user.id
        c = DB.cursor()
        c.execute("SELECT COUNT(*) FROM logs WHERE user_id = ?", (uid,))
        count = c.fetchone()[0]
        query.edit_message_text(f"📊 You have made {count} download attempts with the bot.")
    elif data == "about":
        query.edit_message_text("Downloader Bot — supports TikTok/IG/FB/YouTube/Snapchat. Use only for content you are allowed to download.")

def handle_text(update: Update, context: CallbackContext):
    user = update.effective_user
    add_user(user)
    text = update.message.text.strip()
    action = context.user_data.get("action")

    # If user asked for YouTube search:
    if action == "yt_search":
        do_youtube_search(update, context, text)
        context.user_data.pop("action", None)
        return

    # If text is not a URL and no action set:
    if "http" not in text:
        update.message.reply_text("❌ Please send a valid link or use /start to open the menu.")
        return

    # default action: try to download video
    platform = detect_platform(text)
    update.message.reply_text(f"⏳ Attempting to download from {platform} — please wait...")

    # Choose method per platform
    file_path = None
    direct_url = None

    if platform == "youtube":
        # For YouTube we use yt-dlp
        audio_only = (action == "dl_audio")
        file_path = download_with_ytdlp(text, audio_only=audio_only)
        result_desc = "success" if file_path else "failed"
        log_action(user.id, text, platform, action or "dl_video", result_desc)
        if file_path:
            send_file(update, context, file_path, audio_only)
            # cleanup
            try:
                os.remove(file_path)
            except:
                pass
            return
        else:
            update.message.reply_text("❌ YouTube download failed with yt-dlp.")
            return

    # For other platforms, try external API which returns direct link
    direct_url = download_via_api(text)
    if direct_url:
        # If user asked for audio-only, try to use yt-dlp on direct URL or send as audio
        if action == "dl_audio":
            file_path = download_with_ytdlp(direct_url, audio_only=True)
            if file_path:
                send_file(update, context, file_path, audio_only=True)
                try:
                    os.remove(file_path)
                except:
                    pass
                log_action(user.id, text, platform, "dl_audio", "success")
                return
        else:
            # send direct URL as video (Telegram will fetch)
            try:
                update.message.reply_video(direct_url)
                log_action(user.id, text, platform, action or "dl_video", "success")
            except Exception as e:
                logger.exception("Sending direct video failed: %s", e)
                update.message.reply_text("❌ Sending video failed; maybe file too large or unsupported.")
                log_action(user.id, text, platform, action or "dl_video", f"send_failed:{e}")
            return

    # fallback: try yt-dlp on original URL for platforms that yt-dlp supports
    file_path = download_with_ytdlp(text, audio_only=(action=="dl_audio"))
    if file_path:
        send_file(update, context, file_path, audio_only=(action=="dl_audio"))
        try:
            os.remove(file_path)
        except:
            pass
        log_action(user.id, text, platform, action or "dl_video", "success")
        return

    # if everything fails:
    update.message.reply_text("❌ Download failed. Either unsupported link, the platform blocks downloads, or the downloader API is down.")
    log_action(user.id, text, platform, action or "dl_video", "failed")

def do_youtube_search(update: Update, context: CallbackContext, query_text: str):
    update.message.reply_text(f"🔎 Searching YouTube for: {query_text}\nPlease wait...")
    # Use yt-dlp to search and return top 5 results (ytsearch:key)
    try:
        # yt-dlp can use "ytsearch5:<query>" to get results
        search_query = f"ytsearch5:{query_text}"
        tmp = download_with_ytdlp(search_query, audio_only=False)
        # download_with_ytdlp won't return here for search; instead we should call yt-dlp JSON output
        # We'll call yt-dlp via subprocess to get JSON metadata without downloading
        cmd = [YTDLP, "--dump-json", f"ytsearch5:{query_text}"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        out = proc.stdout.decode("utf-8", errors="ignore")
        # yt-dlp prints multiple JSON objects (one per line). parse first 5 titles/urls
        lines = [l for l in out.splitlines() if l.strip()]
        results = []
        import json
        for i, line in enumerate(lines[:5]):
            j = json.loads(line)
            title = j.get("title")
            url = j.get("webpage_url")
            results.append((title, url))
        if not results:
            update.message.reply_text("No results found.")
            return
        text = "Top results:\n\n" + "\n".join([f"{i+1}. {r[0]}\n{r[1]}" for i, r in enumerate(results)])
        update.message.reply_text(text)
    except Exception as e:
        logger.exception("YouTube search error: %s", e)
        update.message.reply_text("Search failed. Try again later.")
    finally:
        context.user_data.pop("action", None)

def send_file(update: Update, context: CallbackContext, file_path: str, audio_only=False):
    chat = update.effective_chat
    try:
        if audio_only:
            with open(file_path, "rb") as fh:
                update.message.reply_audio(fh)
        else:
            # if the file is big, you may need to send as document
            size = os.path.getsize(file_path)
            with open(file_path, "rb") as fh:
                if size > 50 * 1024 * 1024:
                    update.message.reply_document(fh)
                else:
                    update.message.reply_video(fh)
    except Exception as e:
        logger.exception("send_file error: %s", e)
        update.message.reply_text("Failed to send file (maybe too large). You can try downloading locally.")

def help_command(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Send a supported link (TikTok, Instagram, Facebook, YouTube, Snapchat) or use /start.\n"
        "Use only for content you have rights to download."
    )

def errors(update: Update, context: CallbackContext):
    logger.exception("Update caused error: %s", context.error)

def main():
    if not TOKEN:
        logger.error("BOT_TOKEN not set in env vars")
        return
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CallbackQueryHandler(menu_callback))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    dp.add_error_handler(errors)

    logger.info("Bot starting polling...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
