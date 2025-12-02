import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
GROUP_ID = int(os.getenv("GROUP_ID"))
OWNER_ID = int(os.getenv("OWNER_ID"))
AUTO_DELETE = int(os.getenv("AUTO_DELETE", "0"))   # optional

DB = "filestore.db"

# -------------------------------------------------------------------
# LOGGING
# -------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s"
)

# -------------------------------------------------------------------
# DATABASE INIT
# -------------------------------------------------------------------

con = sqlite3.connect(DB, check_same_thread=False)
cur = con.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS files (
    code TEXT PRIMARY KEY,
    user_id INTEGER,
    msg_id INTEGER,
    caption TEXT,
    stored_at INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS batches (
    user_id INTEGER,
    code TEXT,
    msg_id INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY
)
""")

# add owner as default admin
cur.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (OWNER_ID,))
con.commit()

# -------------------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------------------

def gen_code():
    import random, string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


def is_admin(uid: int):
    row = cur.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone()
    return bool(row)


async def forward_safely(app, chat_id, msg):
    """
    Forwards EXACT original file with caption.
    """
    await asyncio.sleep(1.5)   # flood control delay

    if msg.photo:
        return await app.bot.send_photo(
            chat_id,
            msg.photo[-1].file_id,
            caption=msg.caption or ""
        )
    elif msg.video:
        return await app.bot.send_video(
            chat_id,
            msg.video.file_id,
            caption=msg.caption or ""
        )
    elif msg.document:
        return await app.bot.send_document(
            chat_id,
            msg.document.file_id,
            caption=msg.caption or ""
        )
    elif msg.animation:
        return await app.bot.send_animation(
            chat_id,
            msg.animation.file_id,
            caption=msg.caption or ""
        )
    elif msg.sticker:
        return await app.bot.send_sticker(
            chat_id,
            msg.sticker.file_id
        )
    else:
        # text
        return await app.bot.send_message(chat_id, msg.text)


# -------------------------------------------------------------------
# COMMANDS
# -------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if args:
        # deep link restore
        code = args[0]
        row = cur.execute("SELECT msg_id FROM files WHERE code=?", (code,)).fetchone()
        if not row:
            return await update.message.reply_text("❌ Invalid or expired link.")

        msg_id = row[0]
        try:
            original = await context.bot.forward_message(
                chat_id=update.effective_chat.id,
                from_chat_id=GROUP_ID,
                message_id=msg_id
            )
        except:
            return await update.message.reply_text("❌ File not found in storage.")

        return

    await update.message.reply_text(
        "👋 Welcome!\nSend /filestore to store the next file.\nAdmins can use batch mode."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "**Available Commands:**\n\n"
        "filestore – Store next file only\n"
        "myfiles – Show stored files\n"
        "setcode – Rename last stored file\n\n"
        "**Admin Commands:**\n"
        "batch – Start silent batch\n"
        "batchdone – Finish batch\n"
        "adminlist – Show admins\n"
        "addadmin – Add admin (owner only)\n"
        "removeadmin – Remove admin (owner only)",
        parse_mode="Markdown"
    )


# ===================================================================
# FILERESTORE (Single file)
# ===================================================================

pending_single = {}   # user_id → True
last_stored = {}      # user_id → code


async def filestore(update: Update, context):
    uid = update.effective_user.id
    pending_single[uid] = True
    await update.message.reply_text("📥 Send the file you want to store.")


async def single_file_handler(update: Update, context):
    uid = update.effective_user.id

    if uid not in pending_single:
        return

    msg = update.message
    code = gen_code()

    # forward to GROUP
    forwarded = await forward_safely(context.application, GROUP_ID, msg)

    cur.execute(
        "INSERT INTO files(code, user_id, msg_id, caption, stored_at) VALUES(?,?,?,?,?)",
        (code, uid, forwarded.message_id, msg.caption or "", int(datetime.now().timestamp()))
    )
    con.commit()

    del pending_single[uid]
    last_stored[uid] = code

    link = f"https://t.me/{BOT_USERNAME}?start={code}"

    await update.message.reply_text(f"✅ File stored!\n🔗 {link}")


async def myfiles(update, context):
    uid = update.effective_user.id
    rows = cur.execute(
        "SELECT code FROM files WHERE user_id=?", (uid,)
    ).fetchall()

    if not rows:
        return await update.message.reply_text("❌ You have no stored files.")

    txt = "📦 **Your Files:**\n\n" + "\n".join([row[0] for row in rows])
    await update.message.reply_text(txt, parse_mode="Markdown")


async def setcode(update, context):
    uid = update.effective_user.id
    if uid not in last_stored:
        return await update.message.reply_text("❌ No recent stored file.")

    if not context.args:
        return await update.message.reply_text("❌ Provide a new code.")

    new = context.args[0]
    old = last_stored[uid]

    cur.execute("UPDATE files SET code=? WHERE code=?", (new, old))
    con.commit()

    last_stored[uid] = new

    await update.message.reply_text(f"✅ Code updated: `{new}`", parse_mode="Markdown")


# ===================================================================
# BATCH MODE
# ===================================================================

batch_mode = {}   # uid → True
batch_files = {}  # uid → list of message_ids


async def batch(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("❌ Admin only.")

    batch_mode[uid] = True
    batch_files[uid] = []
    await update.message.reply_text("📦 Batch mode started.\nSend files silently.")


async def batch_handler(update: Update, context):
    uid = update.effective_user.id
    if uid not in batch_mode:
        return

    msg = update.message
    forwarded = await forward_safely(context.application, GROUP_ID, msg)

    batch_files[uid].append(forwarded.message_id)


async def batchdone(update, context):
    uid = update.effective_user.id
    if uid not in batch_mode:
        return await update.message.reply_text("❌ You are not in batch mode.")

    msgs = batch_files.get(uid, [])
    if not msgs:
        return await update.message.reply_text("❌ Batch empty.")

    code = gen_code()
    # we store only FIRST message id (entry point)
    cur.execute(
        "INSERT INTO files(code, user_id, msg_id, caption, stored_at) VALUES(?,?,?,?,?)",
        (code, uid, msgs[0], "", int(datetime.now().timestamp()))
    )
    con.commit()

    del batch_mode[uid]
    del batch_files[uid]

    link = f"https://t.me/{BOT_USERNAME}?start={code}"
    await update.message.reply_text(f"✅ Batch stored!\n🔗 {link}")


# ===================================================================
# ADMIN SYSTEM
# ===================================================================

async def adminlist(update, context):
    rows = cur.execute("SELECT user_id FROM admins").fetchall()
    txt = "**Admins:**\n" + "\n".join([str(r[0]) for r in rows])
    await update.message.reply_text(txt, parse_mode="Markdown")


async def addadmin(update, context):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return await update.message.reply_text("❌ Owner only.")

    if not context.args:
        return await update.message.reply_text("Send user ID.")

    new = int(context.args[0])
    cur.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (new,))
    con.commit()

    await update.message.reply_text("✅ Admin added.")


async def removeadmin(update, context):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return await update.message.reply_text("❌ Owner only.")

    if not context.args:
        return await update.message.reply_text("Send user ID.")

    rem = int(context.args[0])
    cur.execute("DELETE FROM admins WHERE user_id=?", (rem,))
    con.commit()

    await update.message.reply_text("❌ Admin removed.")


# ===================================================================
# MESSAGE ROUTER
# ===================================================================

async def message_handler(update: Update, context):
    uid = update.effective_user.id

    # single store
    if uid in pending_single:
        return await single_file_handler(update, context)

    # batch
    if uid in batch_mode:
        return await batch_handler(update, context)


# ===================================================================
# MAIN
# ===================================================================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("filestore", filestore))
    app.add_handler(CommandHandler("myfiles", myfiles))
    app.add_handler(CommandHandler("setcode", setcode))

    app.add_handler(CommandHandler("batch", batch))
    app.add_handler(CommandHandler("batchdone", batchdone))

    app.add_handler(CommandHandler("adminlist", adminlist))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CommandHandler("removeadmin", removeadmin))

    app.add_handler(MessageHandler(filters.ALL, message_handler))

    app.run_polling()

if __name__ == "__main__":
    main()