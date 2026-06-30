# TeleGuardian v2.1.0
import asyncio
import datetime
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Set, Union

import aiosqlite
from dotenv import load_dotenv
from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    MessageOriginChannel,
    Update,
    User,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# 加載 .env 檔案
load_dotenv()

# 設定日誌
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

DB_PATH = "manage.db"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FIRST_ADMIN_ID_RAW = os.getenv("FIRST_ADMIN_ID")
FIRST_ADMIN_ID = (
    int(FIRST_ADMIN_ID_RAW)
    if FIRST_ADMIN_ID_RAW and FIRST_ADMIN_ID_RAW.isdigit()
    else None
)

if not TOKEN:
    print("錯誤：找不到 TELEGRAM_BOT_TOKEN！請在 .env 檔案中設定。")
    exit(1)


# --- 快取管理 ---
class BotCache:
    def __init__(self):
        self.admins: Set[int] = set()
        self.user_id_whitelist: Set[int] = set()
        self.username_whitelist: Set[str] = set()
        self.channel_whitelist: Set[int] = set()
        self.banned_words: Set[str] = set()


cache = BotCache()


# --- 資料庫操作 ---
class Database:
    @staticmethod
    async def init():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, username TEXT)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY, username TEXT)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS channel_whitelist (channel_id INTEGER PRIMARY KEY)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS banned_words (word TEXT PRIMARY KEY)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS violations (user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0)"
            )

            # 欄位遷移邏輯
            migration_tasks = [
                ("admins", "username", "TEXT"),
                ("whitelist", "username", "TEXT"),
            ]
            for table, col, col_type in migration_tasks:
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass  # 欄位已存在

            await db.commit()
        await Database.load_cache()

    @staticmethod
    async def load_cache():
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM admins") as cursor:
                cache.admins = {row[0] for row in await cursor.fetchall()}
                if FIRST_ADMIN_ID:
                    cache.admins.add(FIRST_ADMIN_ID)

            async with db.execute("SELECT user_id, username FROM whitelist") as cursor:
                rows = await cursor.fetchall()
                cache.user_id_whitelist = {row[0] for row in rows if row[0]}
                cache.username_whitelist = {row[1].lower() for row in rows if row[1]}

            async with db.execute("SELECT channel_id FROM channel_whitelist") as cursor:
                cache.channel_whitelist = {row[0] for row in await cursor.fetchall()}

            async with db.execute("SELECT word FROM banned_words") as cursor:
                cache.banned_words = {row[0].lower() for row in await cursor.fetchall()}
        logging.info("Cache loaded successfully.")

    @staticmethod
    async def execute(query, params=None):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(query, params or ())
            await db.commit()

    @staticmethod
    async def fetch_one(query, params=None):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(query, params or ()) as cursor:
                return await cursor.fetchone()


def is_whitelisted(val: Union[int, str]):
    """檢查 ID 或 Username 是否在白名單中"""
    if isinstance(val, int) or (isinstance(val, str) and val.isdigit()):
        return int(val) in cache.user_id_whitelist
    u_name = str(val).lstrip("@").lower()
    return u_name in cache.username_whitelist


def is_bot_admin(user_id: int):
    return user_id in cache.admins


async def add_violation(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO violations (user_id, count) VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET count = count + 1",
            (user_id,),
        )
        await db.commit()
        async with db.execute(
            "SELECT count FROM violations WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def remove_violation(user_id: int):
    await Database.execute("DELETE FROM violations WHERE user_id = ?", (user_id,))


async def decrement_violation(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT count FROM violations WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                new_count = max(0, row[0] - 1)
                if new_count == 0:
                    await db.execute(
                        "DELETE FROM violations WHERE user_id = ?", (user_id,)
                    )
                else:
                    await db.execute(
                        "UPDATE violations SET count = ? WHERE user_id = ?",
                        (new_count, user_id),
                    )
                await db.commit()


# --- 自動刪除邏輯 ---
async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    """延遲刪除任務"""
    job = context.job
    try:
        await context.bot.delete_message(chat_id=job.chat_id, message_id=job.data)
    except:
        pass


def schedule_delete(context: ContextTypes.DEFAULT_TYPE, message: Message):
    """將訊息排程於 180 秒後刪除"""
    if message:
        context.job_queue.run_once(
            delete_message_job, 180, data=message.message_id, chat_id=message.chat_id
        )


# --- 輔助函式 ---
def get_batch_items(update: Update):
    """獲取批量輸入項目，結合訊息文本、提及實體 (TEXT_MENTION) 與回覆訊息"""
    items = []
    message = update.message
    if not message:
        return []

    # 1. 提取 TEXT_MENTION (提及姓名，通常用於無 Username 的用戶)
    entities = message.entities or message.caption_entities or []
    for ent in entities:
        if ent.type == MessageEntity.TEXT_MENTION and ent.user:
            items.append(ent.user)

    # 2. 處理文本行 (ID 或 @Username)
    text = message.text or message.caption or ""
    parts = text.split(None, 1)
    if len(parts) >= 2:
        lines = [line.strip() for line in parts[1].split("\n") if line.strip()]
        items.extend(lines)

    # 3. 如果前兩者都沒有，且有回覆訊息，則獲取回覆者的 User 物件
    if not items and message.reply_to_message:
        items.append(message.reply_to_message.from_user)

    return items


# --- 管理指令 ---
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id):
        return
    items = get_batch_items(update)
    if not items:
        msg = await update.message.reply_text(
            "使用方式：\n1. 回覆訊息並輸入 /addadmin\n2. 輸入 /addadmin 並換行輸入多個 ID/Username",
            message_thread_id=update.message.message_thread_id,
        )
        schedule_delete(context, msg)
        return

    success, failed = [], []
    for item in items:
        u_id, u_name = None, None
        if isinstance(item, User):
            u_id, u_name = item.id, item.username
        else:
            item_str = str(item).strip()
            if item_str.startswith("@"):
                u_name = item_str.lstrip("@")
            else:
                try:
                    u_id = int(item_str)
                except:
                    u_name = item_str

        try:
            if u_id and not u_name:
                try:
                    member = await context.bot.get_chat_member(
                        update.effective_chat.id, u_id
                    )
                    u_name = member.user.username
                except:
                    try:
                        chat = await context.bot.get_chat(u_id)
                        u_name = chat.username
                    except:
                        pass
            elif not u_id and u_name:
                try:
                    chat = await context.bot.get_chat(f"@{u_name}")
                    u_id, u_name = chat.id, chat.username
                except:
                    pass

            if u_id:
                await Database.execute(
                    "INSERT INTO admins (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
                    (u_id, u_name),
                )
                cache.admins.add(u_id)
                success.append(f"{u_id}{f' (@{u_name})' if u_name else ''}")
            else:
                failed.append(f"{item} (無法獲取 ID)")
        except Exception as e:
            failed.append(f"{item} ({str(e)})")

    res = []
    if success:
        res.append(f"✅ 已添加管理員: {', '.join(success)}")
    if failed:
        res.append(f"❌ 失敗: {', '.join(failed)}")
    msg = await update.message.reply_text(
        "\n".join(res), message_thread_id=update.message.message_thread_id
    )
    schedule_delete(context, msg)


async def del_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id):
        return
    items = get_batch_items(update)
    if not items:
        return

    removed = []
    for item in items:
        try:
            target_id = item.id if isinstance(item, User) else int(item)
            if FIRST_ADMIN_ID and target_id == FIRST_ADMIN_ID:
                continue
            await Database.execute("DELETE FROM admins WHERE user_id = ?", (target_id,))
            cache.admins.discard(target_id)
            removed.append(str(target_id))
        except:
            pass

    if removed:
        msg = await update.message.reply_text(
            f"已移除管理員: {', '.join(removed)}",
            message_thread_id=update.message.message_thread_id,
        )
        schedule_delete(context, msg)


async def add_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id):
        return
    items = get_batch_items(update)
    if not items:
        msg = await update.message.reply_text(
            "使用方式：\n1. 回覆訊息並輸入 /addwl\n2. 輸入 /addwl 並換行輸入多個 ID/Username",
            message_thread_id=update.message.message_thread_id,
        )
        schedule_delete(context, msg)
        return

    success, failed = [], []
    for item in items:
        u_id, u_name = None, None
        if isinstance(item, User):
            u_id, u_name = item.id, item.username
        else:
            item_str = str(item).strip()
            if item_str.startswith("@"):
                u_name = item_str.lstrip("@")
            else:
                try:
                    u_id = int(item_str)
                except:
                    u_name = item_str

        try:
            if u_id and not u_name:
                try:
                    member = await context.bot.get_chat_member(
                        update.effective_chat.id, u_id
                    )
                    u_name = member.user.username
                except:
                    try:
                        chat = await context.bot.get_chat(u_id)
                        u_name = chat.username
                    except:
                        pass
            elif not u_id and u_name:
                try:
                    chat = await context.bot.get_chat(f"@{u_name}")
                    u_id, u_name = chat.id, chat.username
                except:
                    pass

            if u_id:
                await Database.execute(
                    "INSERT INTO whitelist (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
                    (u_id, u_name),
                )
                cache.user_id_whitelist.add(u_id)
                if u_name:
                    cache.username_whitelist.add(u_name.lower())
                success.append(f"{u_id}{f' (@{u_name})' if u_name else ''}")
            else:
                failed.append(f"{item} (無法獲取 ID)")
        except Exception as e:
            failed.append(f"{item} ({str(e)})")

    res = []
    if success:
        res.append(f"✅ 已添加至白名單: {', '.join(success)}")
    if failed:
        res.append(f"❌ 失敗: {', '.join(failed)}")
    msg = await update.message.reply_text(
        "\n".join(res), message_thread_id=update.message.message_thread_id
    )
    schedule_delete(context, msg)


async def del_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id):
        return
    items = get_batch_items(update)
    if not items:
        return

    removed = []
    for item in items:
        try:
            if isinstance(item, User):
                await Database.execute(
                    "DELETE FROM whitelist WHERE user_id = ?", (item.id,)
                )
                cache.user_id_whitelist.discard(item.id)
                if item.username:
                    cache.username_whitelist.discard(item.username.lower())
                removed.append(str(item.id))
            else:
                val = str(item).strip()
                if val.isdigit():
                    target_id = int(val)
                    await Database.execute(
                        "DELETE FROM whitelist WHERE user_id = ?", (target_id,)
                    )
                    cache.user_id_whitelist.discard(target_id)
                else:
                    target_name = val.lstrip("@").lower()
                    await Database.execute(
                        "DELETE FROM whitelist WHERE username = ?", (target_name,)
                    )
                    cache.username_whitelist.discard(target_name)
                removed.append(val)
        except:
            pass

    if removed:
        msg = await update.message.reply_text(
            f"已從白名單移除: {', '.join(removed)}",
            message_thread_id=update.message.message_thread_id,
        )
        schedule_delete(context, msg)


async def add_channel_whitelist_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_bot_admin(update.effective_user.id):
        return
    items = get_batch_items(update)
    if not items:
        return

    added = []
    for item in items:
        try:
            cid = int(item)
            await Database.execute(
                "INSERT INTO channel_whitelist (channel_id) VALUES (?)", (cid,)
            )
            cache.channel_whitelist.add(cid)
            added.append(str(cid))
        except:
            pass

    if added:
        msg = await update.message.reply_text(
            f"已添加頻道至白名單: {', '.join(added)}",
            message_thread_id=update.message.message_thread_id,
        )
        schedule_delete(context, msg)


async def del_channel_whitelist_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not is_bot_admin(update.effective_user.id):
        return
    items = get_batch_items(update)
    if not items:
        return

    removed = []
    for item in items:
        try:
            cid = int(item)
            await Database.execute(
                "DELETE FROM channel_whitelist WHERE channel_id = ?", (cid,)
            )
            cache.channel_whitelist.discard(cid)
            removed.append(str(cid))
        except:
            pass

    if removed:
        msg = await update.message.reply_text(
            f"已移除頻道白名單: {', '.join(removed)}",
            message_thread_id=update.message.message_thread_id,
        )
        schedule_delete(context, msg)


async def add_word_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id):
        return
    items = get_batch_items(update)
    if not items:
        return

    added = []
    for word in items:
        try:
            word_clean = word.strip().lower()
            await Database.execute(
                "INSERT INTO banned_words (word) VALUES (?)", (word_clean,)
            )
            cache.banned_words.add(word_clean)
            added.append(word_clean)
        except:
            pass

    if added:
        msg = await update.message.reply_text(
            f"已添加違禁詞: {', '.join(added)}",
            message_thread_id=update.message.message_thread_id,
        )
        schedule_delete(context, msg)


async def del_word_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_admin(update.effective_user.id):
        return
    items = get_batch_items(update)
    if not items:
        return

    removed = []
    for word in items:
        word_clean = word.strip().lower()
        await Database.execute("DELETE FROM banned_words WHERE word = ?", (word_clean,))
        cache.banned_words.discard(word_clean)
        removed.append(word_clean)

    if removed:
        msg = await update.message.reply_text(
            f"已移除違禁詞: {', '.join(removed)}",
            message_thread_id=update.message.message_thread_id,
        )
        schedule_delete(context, msg)


# --- 核心檢查邏輯 ---
async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type == "private":
        return False
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        return any(admin.user.id == update.effective_user.id for admin in admins)
    except:
        return False


async def handle_violation_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, reason: str
):
    message = update.message or update.edited_message
    count = await add_violation(user_id)
    mention = message.from_user.mention_html()
    action_text = ""

    if count == 1:
        action_text = f"{mention} ⚠️ <b>第 1 次違規</b>\n原因：{reason}\n處置：警告"
    elif count == 2:
        action_text = f"{mention} 🔇 <b>第 2 次違規</b>\n原因：{reason}\n處置：<b>禁言 10 分鐘</b>"
        until = datetime.now(timezone.utc) + timedelta(minutes=10)
        await context.bot.restrict_chat_member(
            chat_id=message.chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
    elif count >= 3:
        action_text = f"{mention} 🚫 <b>第 3 次違規</b>\n原因：{reason}\n處置：<b>永久封鎖並移除資料</b>"
        await context.bot.ban_chat_member(chat_id=message.chat_id, user_id=user_id)
        await remove_violation(user_id)

    full_text = f"{action_text}\n<i>(此通知將在 3 分鐘後自動刪除)</i>"

    keyboard = [
        [
            InlineKeyboardButton("✅ 忽略", callback_data=f"ignore_{user_id}"),
            InlineKeyboardButton("🚫 永久封鎖", callback_data=f"ban_{user_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    notice_msg = await message.chat.send_message(
        text=full_text,
        parse_mode="HTML",
        message_thread_id=message.message_thread_id,
        reply_markup=reply_markup,
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
        await decrement_violation(target_id)
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
                    can_invite_users=True,
                ),
            )
        except Exception as e:
            logging.error(f"解除禁言失敗: {e}")

        try:
            await context.bot.unban_chat_member(
                chat_id=query.message.chat_id, user_id=target_id, only_if_banned=True
            )
        except:
            pass

        try:
            await query.message.delete()
        except:
            pass
    elif data.startswith("ban_"):
        target_id = int(data.split("_")[1])
        try:
            await context.bot.ban_chat_member(
                chat_id=query.message.chat_id, user_id=target_id
            )
            await remove_violation(target_id)
            await query.message.edit_text(
                f"🛡️ <b>管理員處置</b>\n用戶 <code>{target_id}</code> 已被永久封鎖。",
                parse_mode="HTML",
            )
        except Exception as e:
            await query.message.edit_text(f"❌ 封鎖失敗：{str(e)}")


async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.edited_message
    if not message or not message.from_user:
        return
    user_id = message.from_user.id
    if (
        is_bot_admin(user_id)
        or is_whitelisted(user_id)
        or await is_group_admin(update, context)
    ):
        return

    text, entities = (
        (message.text or message.caption or ""),
        (message.entities or message.caption_entities or []),
    )
    if any(e.type in [MessageEntity.URL, MessageEntity.TEXT_LINK] for e in entities):
        await message.delete()
        await handle_violation_handler(update, context, user_id, "發送連結")
        return

    if message.contact:
        await message.delete()
        await handle_violation_handler(update, context, user_id, "發送聯絡資訊")
        return

    is_forbidden_fwd = False
    if message.forward_origin:
        is_forbidden_fwd = True
        if isinstance(message.forward_origin, MessageOriginChannel):
            if message.forward_origin.chat.id in cache.channel_whitelist:
                is_forbidden_fwd = False
    elif message.external_reply:
        is_forbidden_fwd = True
        if message.external_reply.chat and message.external_reply.chat.type in [
            "channel",
            "supergroup",
        ]:
            if message.external_reply.chat.id in cache.channel_whitelist:
                is_forbidden_fwd = False
    elif message.reply_to_message:
        reply = message.reply_to_message
        if reply.forward_origin and isinstance(
            reply.forward_origin, MessageOriginChannel
        ):
            if reply.forward_origin.chat.id not in cache.channel_whitelist:
                is_forbidden_fwd = True

    if is_forbidden_fwd:
        await message.delete()
        await handle_violation_handler(
            update, context, user_id, "不當轉發/回覆外部訊息"
        )
        return

    for entity in entities:
        if entity.type == MessageEntity.MENTION:
            mention_text = text[entity.offset : entity.offset + entity.length]
            if not is_whitelisted(mention_text):
                await message.delete()
                await handle_violation_handler(
                    update, context, user_id, "標記非白名單用戶"
                )
                return
        elif entity.type == MessageEntity.TEXT_MENTION:
            if not is_whitelisted(entity.user.id):
                await message.delete()
                await handle_violation_handler(
                    update, context, user_id, "標記非白名單用戶"
                )
                return

    text_lower = text.lower()
    for word in cache.banned_words:
        if word in text_lower:
            await message.delete()
            await handle_violation_handler(update, context, user_id, "包含違禁詞")
            return


async def post_init(application: Application):
    await Database.init()


if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("deladmin", del_admin_command))
    app.add_handler(CommandHandler("addwl", add_whitelist_command))
    app.add_handler(CommandHandler("delwl", del_whitelist_command))
    app.add_handler(CommandHandler("addcwl", add_channel_whitelist_command))
    app.add_handler(CommandHandler("delcwl", del_channel_whitelist_command))
    app.add_handler(CommandHandler("addword", add_word_command))
    app.add_handler(CommandHandler("delword", del_word_command))
    app.add_handler(CallbackQueryHandler(button_callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, check_message))
    app.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE & ~filters.COMMAND, check_message
        )
    )
    print("機器人啟動...")
    app.run_polling()
