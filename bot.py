import os
import logging
import sqlite3
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

# -------------------- DB --------------------
conn = sqlite3.connect("filestore.db")
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS files(
    code TEXT PRIMARY KEY,
    file_id TEXT,
    caption TEXT,
    file_type TEXT
)
""")
conn.commit()


# -------------------- COMMANDS --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! Send a file and then use:\n"
        "filestore – to generate link for that file\n"
        "batch – to start batch mode\n"
        "batchdone – finish batch"
    )


async def filestore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to a file!")

    msg = update.message.reply_to_message
    code = os.urandom(4).hex()
    file_id, caption, file_type = extract_file(msg)

    cur.execute("INSERT INTO files VALUES (?, ?, ?, ?)",
                (code, file_id, caption, file_type))
    conn.commit()

    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    await update.message.reply_text(f"Stored!\nLink: {link}")


# -------------------- FILE EXTRACTOR --------------------
def extract_file(msg):
    if msg.document:
        return msg.document.file_id, msg.caption, "document"
    if msg.photo:
        return msg.photo[-1].file_id, msg.caption, "photo"
    if msg.video:
        return msg.video.file_id, msg.caption, "video"
    if msg.audio:
        return msg.audio.file_id, msg.caption, "audio"
    if msg.voice:
        return msg.voice.file_id, msg.caption, "voice"
    if msg.sticker:
        return msg.sticker.file_id, None, "sticker"
    return None, None, None


# -------------------- HANDLER --------------------
async def deep_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return

    code = context.args[0]

    cur.execute("SELECT file_id, caption, file_type FROM files WHERE code=?", (code,))
    row = cur.fetchone()

    if not row:
        return await update.message.reply_text("Invalid or expired link!")

    file_id, caption, file_type = row

    if file_type == "photo":
        await update.message.reply_photo(file_id, caption=caption)
    elif file_type == "document":
        await update.message.reply_document(file_id, caption=caption)
    elif file_type == "video":
        await update.message.reply_video(file_id, caption=caption)
    elif file_type == "audio":
        await update.message.reply_audio(file_id, caption=caption)
    elif file_type == "voice":
        await update.message.reply_voice(file_id, caption=caption)
    elif file_type == "sticker":
        await update.message.reply_sticker(file_id)


# -------------------- MAIN --------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("filestore", filestore))
    app.add_handler(MessageHandler(filters.Regex("^/start ") & filters.TEXT, deep_link))

    app.run_polling()


if __name__ == "__main__":
    main()