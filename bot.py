import os
import sqlite3
import asyncio
import logging
from time import time
from datetime import datetime

from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN"
BOT_USERNAME = os.getenv("BOT_USERNAME") or "YOUR_BOT_USERNAME"

GROUP_ID = int(os.getenv("GROUP_ID") or -1001234567890)
OWNER_ID = int(os.getenv("OWNER_ID") or 123456789)

BACKUP_GROUP_ID = int(os.getenv("BACKUP_GROUP_ID", "0")) or None
AUTO_DELETE = int(os.getenv("AUTO_DELETE", "18000"))  # 5 hours

# Modes
filestore_mode = {}   # only next file
batch_mode = {}       # batch list

# Logging
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------

db = sqlite3.connect("filestore.db")
cur = db.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS files(
    code TEXT PRIMARY KEY,
    message_id INTEGER,
    owner INTEGER,
    created_at INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS batches(
    code TEXT PRIMARY KEY,
    owner INTEGER,
    created_at INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS items(
    code TEXT,
    message_id INTEGER,
    owner INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS admins(
    id INTEGER PRIMARY KEY
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS meta(
    k TEXT PRIMARY KEY,
    v TEXT
)
""")

db.commit()

cur.execute("INSERT OR IGNORE INTO admins VALUES (?)", (OWNER_ID,))
db.commit()

# ---------------------------------------------------------
# UTILITY FUNCTIONS
# ---------------------------------------------------------

def is_admin(uid):
    row = cur.execute("SELECT id FROM admins WHERE id=?", (uid,)).fetchone()
    return bool(row)


def gen_code(l=8):
    import random, string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=l))


async def safe_forward(msg, target, retries=3):
    for _ in range(retries):
        try:
            forwarded = await msg.forward(target)
            return forwarded.message_id
        except:
            await asyncio.sleep(0.5)
    return None


async def restore_message(context, chat_id, message_id):
    try:
        await context.bot.forward_message(chat_id, GROUP_ID, message_id)
        return True
    except:
        return False
# ---------------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    if " " in txt:
        code = txt.split(" ", 1)[1].strip()
        return await handle_restore_request(update, context, code)

    await update.message.reply_text(
        "Welcome!\n"
        "Use /help to see commands."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "filestore – store next file only\n"
        "myfiles – list stored files\n"
        "setcode – rename last code\n"
        "batch – start silent batch (admin)\n"
        "batchdone – finish batch\n"
        "stats – admin only\n"
        "adminlist – show admins\n"
        "addadmin – owner only\n"
        "removeadmin – owner only\n"
    )


async def cmd_filestore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    filestore_mode[uid] = True
    await update.message.reply_text("Send the file you want to store (one file).")


async def cmd_myfiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = cur.execute(
        "SELECT code, created_at FROM files WHERE owner=? ORDER BY created_at DESC LIMIT 100",
        (uid,)
    ).fetchall()

    if not rows:
        return await update.message.reply_text("You have no stored files.")

    out = []
    for code, ts in rows:
        dt = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"{code}\nhttps://t.me/{BOT_USERNAME}?start={code}")

    await update.message.reply_text("\n\n".join(out))


async def cmd_setcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Usage: setcode NEWCODE")

    new_code = context.args[0].strip()

    exists = cur.execute("SELECT 1 FROM files WHERE code=?", (new_code,)).fetchone()
    exists2 = cur.execute("SELECT 1 FROM batches WHERE code=?", (new_code,)).fetchone()

    if exists or exists2:
        return await update.message.reply_text("Code already exists.")

    row = cur.execute(
        "SELECT code FROM files WHERE owner=? ORDER BY created_at DESC LIMIT 1",
        (uid,)
    ).fetchone()

    if not row:
        return await update.message.reply_text("No recent file.")

    old = row[0]
    cur.execute("UPDATE files SET code=? WHERE code=?", (new_code, old))
    db.commit()

    await update.message.reply_text(
        f"Code updated:\nhttps://t.me/{BOT_USERNAME}?start={new_code}"
    )


async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Admins only.")
    batch_mode[uid] = []


async def cmd_batchdone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not is_admin(uid):
        return await update.message.reply_text("Admins only.")

    if uid not in batch_mode:
        return await update.message.reply_text("No active batch.")

    items = batch_mode.pop(uid)

    if not items:
        return await update.message.reply_text("Batch is empty.")

    code = gen_code()

    cur.execute("INSERT INTO batches VALUES (?,?,?)", (code, uid, int(time())))
    db.commit()

    for mid in items:
        cur.execute("INSERT INTO items VALUES (?,?,?)", (code, mid, uid))
    db.commit()

    await update.message.reply_text(
        f"Batch saved!\nhttps://t.me/{BOT_USERNAME}?start={code}"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Admins only.")

    f = cur.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    b = cur.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    i = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    await update.message.reply_text(
        f"Files: {f}\nBatches: {b}\nItems: {i}"
    )


async def cmd_adminlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = cur.execute("SELECT id FROM admins").fetchall()
    out = "Admins:\n" + "\n".join(str(r[0]) for r in rows)
    await update.message.reply_text(out)


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    uid = int(context.args[0])
    cur.execute("INSERT OR IGNORE INTO admins VALUES(?)", (uid,))
    db.commit()
    await update.message.reply_text("Admin added.")


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    uid = int(context.args[0])
    cur.execute("DELETE FROM admins WHERE id=?", (uid,))
    db.commit()
    await update.message.reply_text("Admin removed.")


# ---------------------------------------------------------
# MESSAGE HANDLER
# ---------------------------------------------------------

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    uid = msg.from_user.id

    if msg.text and msg.text.startswith("/"):
        return

    # filestore mode (store only next file)
    if uid in filestore_mode:
        filestore_mode.pop(uid, None)
        mid = await safe_forward(msg, GROUP_ID)

        if not mid:
            return await update.message.reply_text("Failed to store file.")

        code = gen_code()
        cur.execute("INSERT INTO files VALUES (?,?,?,?)",
                    (code, mid, uid, int(time())))
        db.commit()

        return await update.message.reply_text(
            f"Stored!\nhttps://t.me/{BOT_USERNAME}?start={code}"
        )

    # silent batch mode
    if uid in batch_mode:
        mid = await safe_forward(msg, GROUP_ID)
        if mid:
            batch_mode[uid].append(mid)
        return

    # normal mode = ignore
    return
# ---------------------------------------------------------
# RESTORE SYSTEM
# ---------------------------------------------------------

async def handle_restore_request(update: Update, context, code: str):
    chat_id = update.message.chat.id

    row = cur.execute("SELECT message_id FROM files WHERE code=?", (code,)).fetchone()
    if row:
        mid = row[0]
        ok = await restore_message(context, chat_id, mid)
        if ok:
            return await update.message.reply_text("Here is your file.")
        return await update.message.reply_text("Failed to restore.")
    
    items = cur.execute(
        "SELECT message_id FROM items WHERE code=? ORDER BY rowid ASC",
        (code,)
    ).fetchall()

    if items:
        await update.message.reply_text(f"Sending {len(items)} files…")
        for (mid,) in items:
            await restore_message(context, chat_id, mid)
            await asyncio.sleep(1.5)
        return

    await update.message.reply_text("Invalid or expired link.")


# ---------------------------------------------------------
# AUTO DELETE LOOP
# ---------------------------------------------------------

async def auto_delete_loop(app):
    while True:
        row = cur.execute("SELECT v FROM meta WHERE k='auto_delete_enabled'").fetchone()
        if not row or row[0] == "0":
            await asyncio.sleep(10)
            continue

        delay = int(cur.execute(
            "SELECT v FROM meta WHERE k='auto_delete_seconds'"
        ).fetchone()[0])

        now_ts = int(time())
        rows = cur.execute("SELECT code, created_at FROM files").fetchall()

        for code, ts in rows:
            if now_ts - ts > delay:
                cur.execute("DELETE FROM files WHERE code=?", (code,))
                db.commit()

        await asyncio.sleep(30)


# ---------------------------------------------------------
# POST INIT
# ---------------------------------------------------------

async def post_init(app):
    cur.execute("INSERT OR IGNORE INTO meta VALUES('auto_delete_enabled','1')")
    cur.execute("INSERT OR IGNORE INTO meta VALUES('auto_delete_seconds',?)",
                (AUTO_DELETE,))
    db.commit()

    asyncio.create_task(auto_delete_loop(app))
    logger.info("Auto delete loop running.")


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

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

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    logger.info("🔥 Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()