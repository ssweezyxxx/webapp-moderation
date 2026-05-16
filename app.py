import os
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from fastapi import FastAPI, Request, BackgroundTasks
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler   # <-- сюда добавлен ConversationHandler
)
from contextlib import asynccontextmanager

# ------------------ КОНФИГ ------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("Нет TELEGRAM_TOKEN")
OWNER_ID = 7868534958
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
if not RENDER_EXTERNAL_URL:
    raise ValueError("Нет RENDER_EXTERNAL_URL (Render сам даёт эту переменную)")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------ БД (SQLite) ------------------
DB_PATH = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS group_settings (
        chat_id INTEGER PRIMARY KEY,
        automod_enabled INTEGER DEFAULT 1,
        check_links INTEGER DEFAULT 1,
        check_keywords INTEGER DEFAULT 1,
        check_flood INTEGER DEFAULT 1,
        action_on_link TEXT DEFAULT 'delete',
        action_on_keyword TEXT DEFAULT 'delete',
        action_on_flood TEXT DEFAULT 'mute',
        flood_threshold INTEGER DEFAULT 5,
        warn_limit INTEGER DEFAULT 3,
        mute_duration INTEGER DEFAULT 10
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_words (
        chat_id INTEGER,
        word TEXT,
        PRIMARY KEY (chat_id, word)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_admins (
        chat_id INTEGER,
        user_id INTEGER,
        PRIMARY KEY (chat_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS warnings (
        chat_id INTEGER,
        user_id INTEGER,
        count INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    )''')
    conn.commit()
    conn.close()

def get_setting(chat_id: int, key: str, default=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return default
    cols = ["chat_id","automod_enabled","check_links","check_keywords","check_flood","action_on_link","action_on_keyword","action_on_flood","flood_threshold","warn_limit","mute_duration"]
    try:
        idx = cols.index(key)
        return row[idx]
    except:
        return default

def set_setting(chat_id: int, key: str, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO group_settings (chat_id) VALUES (?)", (chat_id,))
    c.execute(f"UPDATE group_settings SET {key} = ? WHERE chat_id = ?", (value, chat_id))
    conn.commit()
    conn.close()

def get_all_settings(chat_id: int) -> Dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        set_setting(chat_id, "automod_enabled", 1)
        return get_all_settings(chat_id)
    cols = ["chat_id","automod_enabled","check_links","check_keywords","check_flood","action_on_link","action_on_keyword","action_on_flood","flood_threshold","warn_limit","mute_duration"]
    return {cols[i]: row[i] for i in range(len(cols))}

def get_banned_words(chat_id: int) -> List[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT word FROM banned_words WHERE chat_id = ?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def add_banned_word(chat_id: int, word: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO banned_words (chat_id, word) VALUES (?, ?)", (chat_id, word.lower()))
    conn.commit()
    conn.close()

def remove_banned_word(chat_id: int, word: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM banned_words WHERE chat_id = ? AND word = ?", (chat_id, word.lower()))
    conn.commit()
    conn.close()

def get_bot_admins(chat_id: int) -> List[int]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM bot_admins WHERE chat_id = ?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def add_bot_admin(chat_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO bot_admins (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
    conn.commit()
    conn.close()

def remove_bot_admin(chat_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM bot_admins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()

def get_warnings(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def add_warning(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO warnings (chat_id, user_id, count) VALUES (?, ?, 1) ON CONFLICT(chat_id, user_id) DO UPDATE SET count = count + 1", (chat_id, user_id))
    c.execute("SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    new_count = c.fetchone()[0]
    conn.commit()
    conn.close()
    return new_count

def reset_warnings(chat_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()

# ------------------ Утилиты ------------------
async def is_admin_or_owner(bot, chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except:
        return False

def parse_time(time_str: str) -> Optional[timedelta]:
    if not time_str:
        return None
    import re
    match = re.match(r"(\d+)([дhмdhm])", time_str.lower())
    if not match:
        return None
    val = int(match.group(1))
    unit = match.group(2)
    if unit in ("д", "d"):
        return timedelta(days=val)
    elif unit == "h":
        return timedelta(hours=val)
    elif unit in ("м", "m"):
        return timedelta(minutes=val)
    return None

# ------------------ Модерация ------------------
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_admin_or_owner(context.bot, chat_id, user_id):
        await update.message.reply_text("⛔ Нет прав")
        return
    text = update.message.text
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("Используй: .бан @user 1д причина")
        return
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        mention = parts[1]
        if mention.startswith("@"):
            try:
                member = await context.bot.get_chat_member(chat_id, mention[1:])
                target = member.user
            except:
                pass
    if not target:
        await update.message.reply_text("Не найден пользователь")
        return
    time_delta = None
    reason = ""
    if len(parts) >= 3:
        time_delta = parse_time(parts[2])
        reason = " ".join(parts[3:]) if len(parts) > 3 else ""
    until = datetime.now() + time_delta if time_delta else None
    try:
        await context.bot.ban_chat_member(chat_id, target.id, until_date=until)
        dur = f"на {time_delta.days}д" if time_delta and time_delta.days else (f"на {time_delta.seconds//3600}ч" if time_delta and time_delta.seconds>=3600 else (f"на {time_delta.seconds//60}м" if time_delta else "навсегда"))
        await update.message.reply_text(f"✅ {target.full_name} забанен {dur}. Причина: {reason if reason else 'не указана'}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_admin_or_owner(context.bot, chat_id, user_id):
        await update.message.reply_text("⛔ Нет прав")
        return
    text = update.message.text
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text("Используй: .мут @user 1ч причина")
        return
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        mention = parts[1]
        if mention.startswith("@"):
            try:
                member = await context.bot.get_chat_member(chat_id, mention[1:])
                target = member.user
            except:
                pass
    if not target:
        await update.message.reply_text("Не найден пользователь")
        return
    time_delta = parse_time(parts[2]) if len(parts) >= 3 else timedelta(minutes=get_setting(chat_id, "mute_duration", 10))
    reason = " ".join(parts[3:]) if len(parts) > 3 else ""
    until = datetime.now() + time_delta
    perms = ChatPermissions(can_send_messages=False)
    try:
        await context.bot.restrict_chat_member(chat_id, target.id, perms, until_date=until)
        dur = f"{time_delta.days}д" if time_delta.days else f"{time_delta.seconds//3600}ч" if time_delta.seconds>=3600 else f"{time_delta.seconds//60}м"
        # ИСПРАВЛЕНА строка (была пропущена кавычка)
        await update.message.reply_text(f"🔇 {target.full_name} замучен на {dur}. Причина: {reason}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_admin_or_owner(context.bot, chat_id, user_id):
        await update.message.reply_text("Нет прав")
        return
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        parts = update.message.text.split()
        if len(parts) > 1:
            mention = parts[1]
            if mention.startswith("@"):
                try:
                    member = await context.bot.get_chat_member(chat_id, mention[1:])
                    target = member.user
                except:
                    pass
    if not target:
        await update.message.reply_text("Не найден")
        return
    perms = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
    try:
        await context.bot.restrict_chat_member(chat_id, target.id, perms)
        await update.message.reply_text(f"🔊 {target.full_name} размучен")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_admin_or_owner(context.bot, chat_id, user_id):
        await update.message.reply_text("Нет прав")
        return
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        parts = update.message.text.split()
        if len(parts) > 1:
            mention = parts[1]
            if mention.startswith("@"):
                try:
                    member = await context.bot.get_chat_member(chat_id, mention[1:])
                    target = member.user
                except:
                    pass
    if not target:
        await update.message.reply_text("Не найден")
        return
    try:
        await context.bot.unban_chat_member(chat_id, target.id)
        await update.message.reply_text(f"✅ {target.full_name} разбанен")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await is_admin_or_owner(context.bot, chat_id, user_id):
        await update.message.reply_text("Нет прав")
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text("Используй: .варн @user причина")
        return
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        mention = parts[1]
        if mention.startswith("@"):
            try:
                member = await context.bot.get_chat_member(chat_id, mention[1:])
                target = member.user
            except:
                pass
    if not target:
        await update.message.reply_text("Не найден")
        return
    reason = parts[2] if len(parts) > 2 else "не указана"
    new_count = add_warning(chat_id, target.id)
    warn_limit = get_setting(chat_id, "warn_limit", 3)
    await update.message.reply_text(f"⚠️ {target.full_name} получил предупреждение {new_count}/{warn_limit}. Причина: {reason}")
    if new_count >= warn_limit:
        try:
            await context.bot.ban_chat_member(chat_id, target.id)
            await update.message.reply_text(f"🚫 {target.full_name} забанен (лимит предупреждений)")
            reset_warnings(chat_id, target.id)
        except Exception as e:
            await update.message.reply_text(f"Не удалось забанить: {e}")

# ------------------ Автомодерация ------------------
flood_tracker = {}

async def automod_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if await is_admin_or_owner(context.bot, chat_id, user_id):
        return
    text = update.message.text
    settings = get_all_settings(chat_id)
    if not settings.get("automod_enabled"):
        return
    action = None
    reason = ""
    if settings.get("check_links"):
        import re
        if re.search(r"(https?://[^\s]+|www\.[^\s]+)", text, re.I):
            action = settings.get("action_on_link", "delete")
            reason = "ссылка"
    if not action and settings.get("check_keywords"):
        banned = get_banned_words(chat_id)
        for w in banned:
            if w in text.lower():
                action = settings.get("action_on_keyword", "delete")
                reason = f"слово: {w}"
                break
    if not action and settings.get("check_flood"):
        now = datetime.now()
        key = f"{chat_id}_{user_id}"
        if key not in flood_tracker:
            flood_tracker[key] = []
        flood_tracker[key] = [t for t in flood_tracker[key] if (now - t).total_seconds() < 5]
        flood_tracker[key].append(now)
        if len(flood_tracker[key]) > settings.get("flood_threshold", 5):
            action = settings.get("action_on_flood", "mute")
            reason = "флуд"
    if action:
        await apply_auto_action(update, context, action, reason, settings)

async def apply_auto_action(update, context, action, reason, settings):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    msg = update.message
    if action == "delete":
        await msg.delete()
        await context.bot.send_message(chat_id, f"🗑️ {msg.from_user.full_name}: удалено ({reason})", reply_to_message_id=msg.message_id)
    elif action == "warn":
        cnt = add_warning(chat_id, user_id)
        limit = settings.get("warn_limit", 3)
        await msg.reply_text(f"⚠️ Предупреждение {cnt}/{limit} за {reason}")
        if cnt >= limit:
            await context.bot.ban_chat_member(chat_id, user_id)
            await msg.reply_text(f"🚫 {msg.from_user.full_name} забанен")
            reset_warnings(chat_id, user_id)
    elif action == "mute":
        duration = settings.get("mute_duration", 10)
        until = datetime.now() + timedelta(minutes=duration)
        perms = ChatPermissions(can_send_messages=False)
        await context.bot.restrict_chat_member(chat_id, user_id, perms, until_date=until)
        await msg.reply_text(f"🔇 {msg.from_user.full_name} замучен на {duration} мин за {reason}")
    elif action == "ban":
        await context.bot.ban_chat_member(chat_id, user_id)
        await msg.reply_text(f"🚫 {msg.from_user.full_name} забанен за {reason}")

# ------------------ Панель (инлайн) ------------------
ADD_WORD, ADD_ADMIN = 10, 11

async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not (await is_admin_or_owner(context.bot, chat_id, user_id) or user_id == OWNER_ID):
        await update.message.reply_text("Нет прав")
        return
    keyboard = [
        [InlineKeyboardButton("🤖 Авто-модерация", callback_data="automod")],
        [InlineKeyboardButton("🚫 Запрещённые слова", callback_data="words")],
        [InlineKeyboardButton("👥 Админы бота", callback_data="admins")],
        [InlineKeyboardButton("⚙️ Лимит предупреждений", callback_data="warn_limit")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="close")]
    ]
    await update.message.reply_text("📋 Панель управления", reply_markup=InlineKeyboardMarkup(keyboard))

async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    if data == "automod":
        s = get_all_settings(chat_id)
        text = (f"Авто-мод: {'✅' if s['automod_enabled'] else '❌'}\n"
                f"Ссылки: {s['action_on_link']}\nСлова: {s['action_on_keyword']}\nФлуд: {s['action_on_flood']}\n"
                f"Порог флуда: {s['flood_threshold']}\nМут(мин): {s['mute_duration']}")
        kb = [[InlineKeyboardButton("🔁 Переключить ссылки", callback_data="toggle_links")],
              [InlineKeyboardButton("🔁 Переключить слова", callback_data="toggle_words")],
              [InlineKeyboardButton("🔁 Переключить флуд", callback_data="toggle_flood")],
              [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "toggle_links":
        current = get_setting(chat_id, "action_on_link", "delete")
        actions = ["delete","warn","mute","ban"]
        new = actions[(actions.index(current)+1)%len(actions)]
        set_setting(chat_id, "action_on_link", new)
        await query.answer(f"Действие на ссылки: {new}")
        await panel_callback(update, context)
    elif data == "toggle_words":
        current = get_setting(chat_id, "action_on_keyword", "delete")
        actions = ["delete","warn","mute","ban"]
        new = actions[(actions.index(current)+1)%len(actions)]
        set_setting(chat_id, "action_on_keyword", new)
        await query.answer(f"Действие на слова: {new}")
        await panel_callback(update, context)
    elif data == "toggle_flood":
        current = get_setting(chat_id, "action_on_flood", "mute")
        actions = ["delete","warn","mute","ban"]
        new = actions[(actions.index(current)+1)%len(actions)]
        set_setting(chat_id, "action_on_flood", new)
        await query.answer(f"Действие на флуд: {new}")
        await panel_callback(update, context)
    elif data == "words":
        words = get_banned_words(chat_id)
        txt = "🚫 Слова:\n" + "\n".join(words) if words else "Нет слов"
        kb = [[InlineKeyboardButton("➕ Добавить", callback_data="add_word")],
              [InlineKeyboardButton("➖ Удалить", callback_data="remove_word")],
              [InlineKeyboardButton("Назад", callback_data="back_main")]]
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "add_word":
        await query.edit_message_text("Введите слово:")
        return ADD_WORD
    elif data == "remove_word":
        words = get_banned_words(chat_id)
        if not words:
            await query.answer("Нет слов")
            return
        kb = [[InlineKeyboardButton(w, callback_data=f"del_{w}")] for w in words]
        kb.append([InlineKeyboardButton("Назад", callback_data="words")])
        await query.edit_message_text("Выберите слово:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("del_"):
        word = data[4:]
        remove_banned_word(chat_id, word)
        await query.answer(f"Удалено: {word}")
        await panel_callback(update, context)
    elif data == "admins":
        admins = get_bot_admins(chat_id)
        txt = "👥 Админы:\n" + "\n".join(str(a) for a in admins) if admins else "Нет админов"
        kb = [[InlineKeyboardButton("➕ Добавить", callback_data="add_admin")],
              [InlineKeyboardButton("➖ Удалить", callback_data="remove_admin")],
              [InlineKeyboardButton("Назад", callback_data="back_main")]]
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "add_admin":
        await query.edit_message_text("Введите ID пользователя (число):")
        return ADD_ADMIN
    elif data == "remove_admin":
        admins = get_bot_admins(chat_id)
        if not admins:
            await query.answer("Нет админов")
            return
        kb = [[InlineKeyboardButton(str(a), callback_data=f"rm_admin_{a}")] for a in admins]
        kb.append([InlineKeyboardButton("Назад", callback_data="admins")])
        await query.edit_message_text("Выберите ID:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("rm_admin_"):
        uid = int(data.split("_")[2])
        remove_bot_admin(chat_id, uid)
        await query.answer(f"Админ {uid} удалён")
        await panel_callback(update, context)
    elif data == "warn_limit":
        limit = get_setting(chat_id, "warn_limit", 3)
        kb = [[InlineKeyboardButton("➕ +1", callback_data="inc_warn")],
              [InlineKeyboardButton("➖ -1", callback_data="dec_warn")],
              [InlineKeyboardButton("Назад", callback_data="back_main")]]
        await query.edit_message_text(f"⚠️ Лимит предупреждений: {limit}", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "inc_warn":
        new = get_setting(chat_id, "warn_limit", 3) + 1
        set_setting(chat_id, "warn_limit", new)
        await query.answer(f"Лимит: {new}")
        await panel_callback(update, context)
    elif data == "dec_warn":
        new = max(1, get_setting(chat_id, "warn_limit", 3) - 1)
        set_setting(chat_id, "warn_limit", new)
        await query.answer(f"Лимит: {new}")
        await panel_callback(update, context)
    elif data == "back_main":
        kb = [[InlineKeyboardButton("🤖 Авто-модерация", callback_data="automod")],
              [InlineKeyboardButton("🚫 Запрещённые слова", callback_data="words")],
              [InlineKeyboardButton("👥 Админы бота", callback_data="admins")],
              [InlineKeyboardButton("⚙️ Лимит предупреждений", callback_data="warn_limit")],
              [InlineKeyboardButton("❌ Закрыть", callback_data="close")]]
        await query.edit_message_text("📋 Панель управления", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "close":
        await query.delete_message()
    return ConversationHandler.END

async def add_word_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = update.message.text.strip().lower()
    if word:
        add_banned_word(update.effective_chat.id, word)
        await update.message.reply_text(f"✅ Слово '{word}' добавлено")
    else:
        await update.message.reply_text("Пусто")
    await panel_callback(update, context)
    return ConversationHandler.END

async def add_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inp = update.message.text.strip()
    if not inp.isdigit():
        await update.message.reply_text("Нужен числовой ID")
        await panel_callback(update, context)
        return ConversationHandler.END
    uid = int(inp)
    chat_id = update.effective_chat.id
    add_bot_admin(chat_id, uid)
    await update.message.reply_text(f"✅ Админ {uid} добавлен")
    await panel_callback(update, context)
    return ConversationHandler.END

conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(panel_callback, pattern="^(add_word|add_admin)$")],
    states={
        ADD_WORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_word_text)],
        ADD_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_admin_text)],
    },
    fallbacks=[CallbackQueryHandler(panel_callback, pattern="^(back_main|close)$")],
    per_message=False,
)

# ------------------ Сборка бота ------------------
ptb_app = Application.builder().token(TELEGRAM_TOKEN).build()
ptb_app.add_handler(CommandHandler("panel", panel_command))
ptb_app.add_handler(CallbackQueryHandler(panel_callback, pattern="^(automod|words|admins|warn_limit|back_main|close|toggle_links|toggle_words|toggle_flood|add_word|remove_word|add_admin|remove_admin|inc_warn|dec_warn|del_.*|rm_admin_.*)$"))
ptb_app.add_handler(conv_handler)

async def dot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    if t.startswith(".бан"):
        await ban_command(update, context)
    elif t.startswith(".мут"):
        await mute_command(update, context)
    elif t.startswith(".анмут"):
        await unmute_command(update, context)
    elif t.startswith(".анбан"):
        await unban_command(update, context)
    elif t.startswith(".варн"):
        await warn_command(update, context)

ptb_app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^\.[банмутанварн]'), dot_handler))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, automod_check))

# ------------------ FastAPI ------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
        await ptb_app.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set to {webhook_url}")
    else:
        logger.warning("No RENDER_EXTERNAL_URL")
    yield
    await ptb_app.bot.delete_webhook()

fast_api = FastAPI(lifespan=lifespan)

@fast_api.post(WEBHOOK_PATH)
async def webhook(request: Request, bg: BackgroundTasks):
    body = await request.json()
    bg.add_task(ptb_app.process_update, Update.de_json(body, ptb_app.bot))
    return {"ok": True}

@fast_api.get("/health")
async def health():
    return {"status": "alive"}

@fast_api.get("/api/my_groups")
async def api_my_groups(user_id: int):
    # Пока тестовые данные, потом заменишь на реальные
    test_groups = [
        {
            "id": -1001234567890,
            "title": "Тестовая группа",
            "settings": get_all_settings(-1001234567890),
            "words": get_banned_words(-1001234567890),
            "admins": get_bot_admins(-1001234567890)
        }
    ]
    return {"groups": test_groups}

@fast_api.post("/api/settings")
async def api_save_settings(data: dict):
    chat_id = data.get("chat_id")
    action = data.get("action")
    if action == "set_setting":
        key = data.get("key")
        value = data.get("value")
        set_setting(chat_id, key, value)
    elif action == "add_word":
        word = data.get("word")
        add_banned_word(chat_id, word)
    elif action == "remove_word":
        word = data.get("word")
        remove_banned_word(chat_id, word)
    return {"status": "ok"}

# ------------------ Запуск ------------------
if __name__ == "__main__":
    import uvicorn
    init_db()
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(fast_api, host="0.0.0.0", port=port)
