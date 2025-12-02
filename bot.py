#!/usr/bin/env python3
"""
Render-friendly, PTB v20 filestore bot (final full).
Features:
- /filestore (store next message only)
- /batch  (admin silent batch)
- /batchdone (finish batch -> single deep link)
- deep-link restore via /start CODE
- admin system (owner add/remove admins)
- auto-delete DB cleanup (optional)
- preserves original file (forward-on-group) and caption
- safe forwarding + ordered batch + 1.5s restore delay
"""
import os
import asyncio
import logging
import sqlite3
import random
import string
from time import time
from datetime import datetime
from typing import Optional, List

from telegram import Update, BotCommand, Message
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIG (ENV) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "YourBotUsername")  # no @
GROUP_ID = int(os.getenv("GROUP_ID", "0"))  # -100... channel/group id where files will be stored
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
BACKUP_GROUP_ID = int(os.getenv("BACKUP_GROUP_ID") or 0) or None
AUTO_DELETE = int(os.getenv("AUTO_DELETE") or 0)  # seconds; 0 => disabled

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable not set")

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- Database ----------------
DB_FILE = "filestore.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

# create tables
cur.execute("""
CREATE TABLE IF NOT EXISTS files (
    code TEXT PRIMARY KEY,
    msg_id INTEGER,
    owner INTEGER,
    created_at INTEGER,
    caption TEXT,
    file_type TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS batches (
    code TEXT PRIMARY KEY,
    owner INTEGER,
    created_at INTEGER,
    item_count INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS items (
    code TEXT,
    msg_id INTEGER,
    owner INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
)
""")
conn.commit()

# ensure owner admin
if OWNER_ID:
    cur.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)", (OWNER_ID,))
    conn.commit()

# ---------------- In-memory modes ----------------
filestore_mode = {}   # user_id -> True (only next message)
batch_mode = {}       # user_id -> [group_msg_id, ...] (silent accumulate)

# ---------------- Utilities ----------------
def gen_code(length: int = 8) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def is_admin(uid: int) -> bool:
    r = cur.execute("SELECT 1 FROM admins WHERE id=?", (uid,)).fetchone()
    return bool(r)

def detect_file_type(msg: Message) -> str:
    if msg.document:
        return "document"
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.sticker:
        return "sticker"
    if msg.animation:
        return "animation"
    return "text"

async def try_forward(msg: Message, target: int, retries: int = 3, delay: float = 0.6) -> Optional[int]:
    """Forward incoming message to target chat (GROUP_ID). Return forwarded message_id or None."""
    for attempt in range(1, retries + 1):
        try:
            fwd = await msg.forward(int(target))
            return fwd.message_id
        except Exception as e:
            logger.warning("Forward attempt %d failed: %s", attempt, e)
            await asyncio.sleep(delay)
    return None

async def forward_message_by_id(app, from_chat_id: int, message_id: int, to_chat_id: int):
    """Forward a message that's already in from_chat_id to to_chat_id."""
    try:
        res = await app.bot.forward_message(chat_id=int(to_chat_id),
                                            from_chat_id=int(from_chat_id),
                                            message_id=int(message_id))
        return res
    except Exception as e:
        logger.exception("forward_message_by_id failed: %s", e)
        return None

# ---------------- Command Handlers ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start [CODE] -> if CODE present restore; else show help"""
    args = context.args or []
    if args:
        code = args[0].strip()
        await handle_restore_request(update, context, code)
        return
    await update.message.reply_text(
        "Welcome — Filestore Bot.\nUse /help to see commands.\n"
        "Use /filestore to store next message (one file). Admins: /batch and /batchdone."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "filestore – store the next message you send (one file)\n"
        "myfiles – list your stored files\n"
        "setcode NEWCODE – rename your last stored file\n\n"
        "batch – start silent batch (admin only)\n"
        "batchdone – finish batch and get link\n\n"
        "stats – admin only\n"
        "adminlist – admin only\n"
        "addadmin USERID – owner only\n"
        "removeadmin USERID – owner only\n"
    )

async def cmd_filestore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    filestore_mode[uid] = True
    await update.message.reply_text("Send the message (file / media / sticker / text) you want to store (single).")

async def cmd_myfiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = cur.execute("SELECT code, created_at FROM files WHERE owner=? ORDER BY created_at DESC", (uid,)).fetchall()
    if not rows:
        await update.message.reply_text("You have no stored files.")
        return
    out = []
    for code, ts in rows:
        dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"{code} — {dt}\nhttps://t.me/{BOT_USERNAME}?start={code}")
    await update.message.reply_text("\n\n".join(out))

async def cmd_setcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: setcode NEWCODE")
        return
    new_code = context.args[0].strip()
    # check collisions
    if cur.execute("SELECT 1 FROM files WHERE code=?", (new_code,)).fetchone() or \
       cur.execute("SELECT 1 FROM batches WHERE code=?", (new_code,)).fetchone():
        return await update.message.reply_text("Code already in use.")
    row = cur.execute("SELECT code FROM files WHERE owner=? ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()
    if not row:
        return await update.message.reply_text("No recent file to rename.")
    old = row[0]
    cur.execute("UPDATE files SET code=? WHERE code=?", (new_code, old))
    conn.commit()
    await update.message.reply_text(f"Code updated: https://t.me/{BOT_USERNAME}?start={new_code}")

# ---------------- Admin Commands ----------------
async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Admins only.")
    batch_mode[uid] = []
    # intentionally silent — do not reply

async def cmd_batchdone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Admins only.")
    if uid not in batch_mode:
        return await update.message.reply_text("No active batch.")
    items = batch_mode.pop(uid)
    if not items:
        return await update.message.reply_text("Batch is empty.")
    code = gen_code(8)
    now_ts = int(time())
    cur.execute("INSERT INTO batches VALUES(?,?,?,?)", (code, uid, now_ts, len(items)))
    for mid in items:
        cur.execute("INSERT INTO items VALUES(?,?,?)", (code, mid, uid))
    conn.commit()
    await update.message.reply_text(f"Batch saved!\nhttps://t.me/{BOT_USERNAME}?start={code}")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Admins only.")
    total_files = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_batches = cur.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    total_items = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    total_admins = cur.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    await update.message.reply_text(
        f"Files: {total_files}\nBatches: {total_batches}\nItems: {total_items}\nAdmins: {total_admins}"
    )

async def cmd_adminlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = cur.execute("SELECT id FROM admins").fetchall()
    await update.message.reply_text("Admins:\n" + "\n".join(str(r[0]) for r in rows))

async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        return await update.message.reply_text("Usage: addadmin USERID")
    uid = int(context.args[0])
    cur.execute("INSERT OR IGNORE INTO admins VALUES(?)", (uid,))
    conn.commit()
    await update.message.reply_text(f"Added admin {uid}")

async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        return await update.message.reply_text("Usage: removeadmin USERID")
    uid = int(context.args[0])
    cur.execute("DELETE FROM admins WHERE id=?", (uid,))
    conn.commit()
    await update.message.reply_text(f"Removed admin {uid}")

# ---------------- Message Handler (filestore single + batch silent) ----------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    uid = msg.from_user.id

    # ignore commands
    if msg.text and msg.text.startswith("/"):
        return

    # filestore mode: only next message
    if uid in filestore_mode:
        filestore_mode.pop(uid, None)
        mid = await try_forward(msg, GROUP_ID)
        if not mid:
            return await msg.reply_text("❌ Could not store the file (forward failed).")
        code = gen_code(8)
        created = int(time())
        caption = msg.caption or (msg.text if msg.text else "")
        ftype = detect_file_type(msg)
        cur.execute("INSERT INTO files VALUES(?,?,?,?,?,?)",
                    (code, mid, uid, created, caption, ftype))
        conn.commit()
        return await msg.reply_text(f"Stored!\nhttps://t.me/{BOT_USERNAME}?start={code}")

    # batch mode: silent forward and append
    if uid in batch_mode:
        mid = await try_forward(msg, GROUP_ID)
        if mid:
            batch_mode[uid].append(mid)
        return

    # normal: ignore
    return

# ---------------- Restore / deep-link handler ----------------
async def handle_restore_request(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    chat_id = update.effective_chat.id

    # single file?
    row = cur.execute("SELECT msg_id FROM files WHERE code=?", (code,)).fetchone()
    if row:
        mid = row[0]
        forwarded = await forward_message_by_id(context.application, GROUP_ID, mid, chat_id)
        if forwarded:
            return
        else:
            return await update.message.reply_text("❌ Failed to restore file.")

    # batch items?
    rows = cur.execute("SELECT msg_id FROM items WHERE code=? ORDER BY rowid ASC", (code,)).fetchall()
    if rows:
        await update.message.reply_text(f"Sending {len(rows)} files…")
        for (mid,) in rows:
            await forward_message_by_id(context.application, GROUP_ID, mid, chat_id)
            await asyncio.sleep(1.5)
        return

    return await update.message.reply_text("Invalid or expired link.")

# ---------------- Auto-delete loop ----------------
async def auto_delete_loop(app):
    while True:
        row = cur.execute("SELECT v FROM meta WHERE k='auto_delete_enabled'").fetchone()
        enabled = row and row[0] == "1"
        if not enabled:
            await asyncio.sleep(10)
            continue
        row2 = cur.execute("SELECT v FROM meta WHERE k='auto_delete_seconds'").fetchone()
        delay = int(row2[0]) if row2 and row2[0].isdigit() else AUTO_DELETE
        if not delay:
            await asyncio.sleep(10)
            continue
        now_ts = int(time())
        # delete outdated single files
        for code, created in cur.execute("SELECT code, created_at FROM files").fetchall():
            if now_ts - created > delay:
                cur.execute("DELETE FROM files WHERE code=?", (code,))
        # delete outdated batches + items
        for code, created, owner, count in cur.execute("SELECT code, created_at, owner, item_count FROM batches").fetchall():
            if now_ts - created > delay:
                cur.execute("DELETE FROM batches WHERE code=?", (code,))
                cur.execute("DELETE FROM items WHERE code=?", (code,))
        conn.commit()
        await asyncio.sleep(30)

# ---------------- Post init ----------------
async def post_init(app):
    # default meta
    cur.execute("INSERT OR IGNORE INTO meta VALUES('auto_delete_enabled','0')")
    cur.execute("INSERT OR IGNORE INTO meta VALUES('auto_delete_seconds',?)", (str(AUTO_DELETE),))
    conn.commit()

    # set bot commands (Bot menu)
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Start the bot / restore file"),
            BotCommand("help", "Show help"),
            BotCommand("filestore", "Store the next file you send"),
            BotCommand("myfiles", "List your stored files"),
            BotCommand("setcode", "Rename last stored file"),
            BotCommand("batch", "Start silent batch mode (admin)"),
            BotCommand("batchdone", "Finish batch and generate one link"),
            BotCommand("stats", "Show bot stats (admin)"),
            BotCommand("adminlist", "List admins"),
            BotCommand("addadmin", "Add an admin (owner only)"),
            BotCommand("removeadmin", "Remove an admin (owner only)"),
        ])
    except Exception as e:
        logger.warning("Could not set bot commands: %s", e)

    # start auto-delete loop
    asyncio.create_task(auto_delete_loop(app))
    logger.info("Post init complete.")

# ---------------- Main ----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("filestore", cmd_filestore))
    app.add_handler(CommandHandler("myfiles", cmd_myfiles))
    app.add_handler(CommandHandler("setcode", cmd_setcode))
    app.add_handler(CommandHandler("batch", cmd_batch))
    app.add_handler(CommandHandler("batchdone", cmd_batchdone))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("adminlist", cmd_adminlist))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))

    # messages
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    logger.info("🔥 Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()