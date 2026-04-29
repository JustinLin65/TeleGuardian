# TeleGuardian v1.1.0
import logging
import sqlite3
import re
import os
import asyncio
import datetime
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from telegram import Update, MessageEntity, MessageOriginChannel, ChatPermissions, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, Application, CommandHandler, CallbackQueryHandler

# 加載 .env 檔案
load_dotenv()

# 設定日誌
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

DB_PATH = 'manage.db'
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
FIRST_ADMIN_ID = os.getenv('FIRST_ADMIN_ID')

if not TOKEN:
    print("錯誤：找不到 TELEGRAM_BOT_TOKEN！請在 .env 檔案中設定。")
    exit(1)

# --- 資料庫操作 ---
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS whitelist (identifier TEXT PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS channel_whitelist (channel_id INTEGER PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS banned_words (word TEXT PRIMARY KEY)')
    cursor.execute('CREATE TABLE IF NOT EXISTS violations (user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0)')
    conn.commit()
    conn.close()

def check_in_db(table, column, value):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute(f'SELECT 1 FROM {table} WHERE {column} = ?', (value,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def is_bot_admin(user_id):
    if FIRST_ADMIN_ID and str(user_id) == str(FIRST_ADMIN_ID): return True
    return check_in_db('admins', 'user_id', user_id)

def add_violation(user_id):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('INSERT INTO violations (user_id, count) VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET count = count + 1', (user_id,))
    cursor.execute('SELECT count FROM violations WHERE user_id = ?', (user_id,))
    count = cursor.fetchone()[0]
    conn.commit(); conn.close()
    return count

def remove_violation(user_id):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('DELETE FROM violations WHERE user_id = ?', (user_id,))
    conn.commit(); conn.close()

def decrement_violation(user_id):
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('SELECT count FROM violations WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    if row:
        new_count = max(0, row[0] - 1)
        if new_count == 0:
            cursor.execute('DELETE FROM violations WHERE user_id = ?', (user_id,))
        else:
            cursor.execute('UPDATE violations SET count = ? WHERE user_id = ?', (new_count, user_id))
    conn.commit(); conn.close()

# --- 自動刪除邏輯 ---
async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    """延遲刪除任務"""
    job = context.job
    try: await context.bot.delete_message(chat_id=job.chat_id, message_id=job.data)
    except: pass

def schedule_delete(context: ContextTypes.DEFAULT_TYPE, message: Message):
    """將訊息排程於 180 秒後刪除"""
    if message:
        context.job_queue.run_once(delete_message_job, 180, data=message.message_id, chat_id=message.chat_id)

# --- 輔助函式 ---
def get_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.reply_to_message: return update.message.reply_to_message.from_user.id
    if context.args:
        try: return int(context.args[0])
        except ValueError: return None
    return None

# --- 管理指令 ---
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id): return
    target_id = get_target_user_id(update, context)
    if not target_id:
        msg = await update.message.reply_text("使用方式：\n1. 回覆訊息並輸入 /addadmin\n2. 輸入 /addadmin [ID]", message_thread_id=update.message.message_thread_id)
        schedule_delete(context, msg); return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO admins (user_id) VALUES (?)', (target_id,))
        conn.commit()
        msg = await update.message.reply_text(f"已成功添加管理員 {target_id}。", message_thread_id=update.message.message_thread_id)
    except:
        msg = await update.message.reply_text(f"用戶 {target_id} 已經是管理員。", message_thread_id=update.message.message_thread_id)
    finally: conn.close()
    schedule_delete(context, msg)

async def del_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id): return
    target_id = get_target_user_id(update, context)
    if not target_id or (FIRST_ADMIN_ID and str(target_id) == str(FIRST_ADMIN_ID)): return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('DELETE FROM admins WHERE user_id = ?', (target_id,))
    conn.commit(); conn.close()
    msg = await update.message.reply_text(f"已移除管理員 {target_id}。", message_thread_id=update.message.message_thread_id)
    schedule_delete(context, msg)

async def add_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id): return
    target_id = get_target_user_id(update, context)
    if not target_id: return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO whitelist (identifier) VALUES (?)', (str(target_id),))
        conn.commit()
        msg = await update.message.reply_text(f"已添加 {target_id} 至白名單。", message_thread_id=update.message.message_thread_id)
        schedule_delete(context, msg)
    except: pass
    finally: conn.close()

async def del_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id): return
    target_id = get_target_user_id(update, context)
    if not target_id: return
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('DELETE FROM whitelist WHERE identifier = ?', (str(target_id),))
    conn.commit(); conn.close()
    msg = await update.message.reply_text(f"已將 {target_id} 移出白名單。", message_thread_id=update.message.message_thread_id)
    schedule_delete(context, msg)

async def add_channel_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id): return
    if not context.args: return
    try:
        cid = int(context.args[0])
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute('INSERT INTO channel_whitelist (channel_id) VALUES (?)', (cid,))
        conn.commit(); conn.close()
        msg = await update.message.reply_text(f"已添加頻道 {cid} 至白名單。", message_thread_id=update.message.message_thread_id)
        schedule_delete(context, msg)
    except: pass

async def del_channel_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id): return
    if not context.args: return
    try:
        cid = int(context.args[0])
        conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
        cursor.execute('DELETE FROM channel_whitelist WHERE channel_id = ?', (cid,))
        conn.commit(); conn.close()
        msg = await update.message.reply_text(f"已移除頻道 {cid}。", message_thread_id=update.message.message_thread_id)
        schedule_delete(context, msg)
    except: pass

async def add_word_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id): return
    if not context.args: return
    word = " ".join(context.args)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO banned_words (word) VALUES (?)', (word,))
        conn.commit(); conn.close()
        msg = await update.message.reply_text(f"已添加違禁詞：{word}", message_thread_id=update.message.message_thread_id)
        schedule_delete(context, msg)
    except: pass

async def del_word_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id): return
    if not context.args: return
    word = " ".join(context.args)
    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor()
    cursor.execute('DELETE FROM banned_words WHERE word = ?', (word,))
    conn.commit(); conn.close()
    msg = await update.message.reply_text(f"已移除違禁詞：{word}", message_thread_id=update.message.message_thread_id)
    schedule_delete(context, msg)

# --- 核心檢查邏輯 ---
async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type == 'private': return False
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        return any(admin.user.id == update.effective_user.id for admin in admins)
    except: return False

async def handle_violation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, reason: str):
    message = update.message or update.edited_message
    count = add_violation(user_id)
    mention = message.from_user.mention_html()
    action_text = ""

    if count == 1:
        action_text = f"{mention} ⚠️ <b>第 1 次違規</b>\n原因：{reason}\n處置：警告"
    elif count == 2:
        action_text = f"{mention} 🔇 <b>第 2 次違規</b>\n原因：{reason}\n處置：<b>禁言 10 分鐘</b>"
        until = datetime.now(timezone.utc) + timedelta(minutes=10)
        await context.bot.restrict_chat_member(chat_id=message.chat_id, user_id=user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
    elif count >= 3:
        action_text = f"{mention} 🚫 <b>第 3 次違規</b>\n原因：{reason}\n處置：<b>永久封鎖並移除資料</b>"
        await context.bot.ban_chat_member(chat_id=message.chat_id, user_id=user_id)
        remove_violation(user_id)

    full_text = f"{action_text}\n<i>(此通知將在 3 分鐘後自動刪除)</i>"
    
    keyboard = [
        [
            InlineKeyboardButton("✅ 忽略", callback_data=f"ignore_{user_id}"),
            InlineKeyboardButton("🚫 永久封鎖", callback_data=f"ban_{user_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    notice_msg = await message.chat.send_message(
        text=full_text, 
        parse_mode='HTML', 
        message_thread_id=message.message_thread_id,
        reply_markup=reply_markup
    )
    schedule_delete(context, notice_msg)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if not is_bot_admin(user_id) and not await is_group_admin(update, context):
        await query.answer("❌ 您沒有權限執行此操作。", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("ignore_"):
        target_id = int(data.split("_")[1])
        decrement_violation(target_id)
        try:
            # 使用細顆粒度權限恢復用戶權限
            await context.bot.restrict_chat_member(
                chat_id=query.message.chat_id,
                user_id=target_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_invite_users=True
                )
            )
        except Exception as e:
            logging.error(f"解除禁言失敗: {e}")
        
        try:
            await context.bot.unban_chat_member(chat_id=query.message.chat_id, user_id=target_id, only_if_banned=True)
        except: pass
        
        try: await query.message.delete()
        except: pass
    elif data.startswith("ban_"):
        target_id = int(data.split("_")[1])
        try:
            await context.bot.ban_chat_member(chat_id=query.message.chat_id, user_id=target_id)
            remove_violation(target_id)
            await query.message.edit_text(f"🛡️ <b>管理員處置</b>\n用戶 <code>{target_id}</code> 已被永久封鎖。", parse_mode='HTML')
        except Exception as e:
            await query.message.edit_text(f"❌ 封鎖失敗：{str(e)}")

async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.edited_message
    if not message or not message.from_user: return
    user_id = message.from_user.id
    if is_bot_admin(user_id) or check_in_db('whitelist', 'identifier', str(user_id)) or await is_group_admin(update, context): return

    text, entities = (message.text or message.caption or ""), (message.entities or message.caption_entities or [])
    if any(e.type in [MessageEntity.URL, MessageEntity.TEXT_LINK] for e in entities):
        await message.delete()
        await handle_violation_handler(update, context, user_id, "發送連結"); return

    is_forbidden_fwd = False
    if message.forward_origin:
        is_forbidden_fwd = True
        if isinstance(message.forward_origin, MessageOriginChannel):
            if check_in_db('channel_whitelist', 'channel_id', message.forward_origin.chat.id): is_forbidden_fwd = False
    elif message.external_reply:
        is_forbidden_fwd = True
        if message.external_reply.chat and message.external_reply.chat.type in ['channel', 'supergroup']:
            if check_in_db('channel_whitelist', 'channel_id', message.external_reply.chat.id): is_forbidden_fwd = False
    elif message.reply_to_message:
        reply = message.reply_to_message
        if reply.forward_origin and isinstance(reply.forward_origin, MessageOriginChannel):
            if not check_in_db('channel_whitelist', 'channel_id', reply.forward_origin.chat.id): is_forbidden_fwd = True
    
    if is_forbidden_fwd:
        await message.delete()
        await handle_violation_handler(update, context, user_id, "不當轉發/回覆外部訊息"); return

    for entity in entities:
        if entity.type == MessageEntity.MENTION:
            if not check_in_db('whitelist', 'identifier', text[entity.offset : entity.offset + entity.length]):
                await message.delete()
                await handle_violation_handler(update, context, user_id, "標記非白名單用戶"); return
        elif entity.type == MessageEntity.TEXT_MENTION:
            if not check_in_db('whitelist', 'identifier', str(entity.user.id)):
                await message.delete()
                await handle_violation_handler(update, context, user_id, "標記非白名單用戶"); return

    conn = sqlite3.connect(DB_PATH); cursor = conn.cursor(); cursor.execute('SELECT word FROM banned_words'); banned_words = [row[0] for row in cursor.fetchall()]; conn.close()
    for word in banned_words:
        if word.lower() in text.lower():
            await message.delete()
            await handle_violation_handler(update, context, user_id, "包含違禁詞"); return

if __name__ == '__main__':
    db_init()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler('addadmin', add_admin_command))
    app.add_handler(CommandHandler('deladmin', del_admin_command))
    app.add_handler(CommandHandler('addwl', add_whitelist_command))
    app.add_handler(CommandHandler('delwl', del_whitelist_command))
    app.add_handler(CommandHandler('addcwl', add_channel_whitelist_command))
    app.add_handler(CommandHandler('delcwl', del_channel_whitelist_command))
    app.add_handler(CommandHandler('addword', add_word_command))
    app.add_handler(CommandHandler('delword', del_word_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, check_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & ~filters.COMMAND, check_message))
    print("機器人啟動..."); app.run_polling()
