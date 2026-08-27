import asyncio
import difflib
import html
import os
import sqlite3
from io import StringIO
import uuid
import shutil
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    BusinessConnection,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile,
    BufferedInputFile,
    InputProfilePhotoStatic,
)
from aiogram.exceptions import TelegramNetworkError



# =========================================================
# CONFIG
# =========================================================

TOKEN = "8675286625:AAEQ_l0pNg-TIMwi4tGu-J_PSZZlqeD4-1A"

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg").strip()

SUPPORT_URL = "https://t.me/your_support"
PREMIUM_URL = "https://t.me/your_premium"



MAX_SPAM_COUNT = 10
SPAM_DELAY = 0.35

MAX_VIDEO_DURATION = 60
VIDEO_NOTE_SIZE = 640

# Telegram Bot API обычно ограничивает скачивание файлов через getFile.
# Для надёжности ставим 20 MB.
MAX_VIDEO_SIZE = 20 * 1024 * 1024


# =========================================================
# BOT
# =========================================================

bot = Bot(TOKEN)
dp = Dispatcher()


# =========================================================
# DATABASE
# =========================================================

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "business_archive.db")

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False
)

cursor = db.cursor()


# =========================================================
# BUSINESS CONNECTIONS
# =========================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id TEXT PRIMARY KEY,

    owner_id INTEGER NOT NULL,
    user_chat_id INTEGER NOT NULL,

    enabled INTEGER NOT NULL DEFAULT 1,

    created_at TEXT,

    last_chat_id INTEGER
)
""")

try:
    cursor.execute("""
        ALTER TABLE business_connections
        ADD COLUMN last_chat_id INTEGER
    """)
except sqlite3.OperationalError:
    pass


# =========================================================
# MESSAGES
# =========================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,

    user_id INTEGER,
    username TEXT,
    first_name TEXT,
    last_name TEXT,

    text TEXT,
    date TEXT,

    media_type TEXT DEFAULT 'text',
    file_id TEXT,

    deleted INTEGER NOT NULL DEFAULT 0,

    UNIQUE(connection_id, chat_id, message_id)
)
""")


# =========================================================
# SETTINGS
# =========================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS settings (
    owner_id INTEGER PRIMARY KEY,

    delete_notifications INTEGER NOT NULL DEFAULT 1,
    save_media INTEGER NOT NULL DEFAULT 1,
    edit_notifications INTEGER NOT NULL DEFAULT 1
)
""")


# =========================================================
# SPAM
# =========================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS spam_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    owner_id INTEGER NOT NULL,

    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,

    created_at TEXT
)
""")


# =========================================================
# MENU STATE
# =========================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS menu_state (
    owner_id INTEGER PRIMARY KEY,

    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,

    updated_at TEXT
)
""")

db.commit()


# =========================================================
# PROFILE BACKUP
# =========================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS profile_backup (
    owner_id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    bio TEXT,
    photo_path TEXT,
    has_photo INTEGER NOT NULL DEFAULT 0,
    saved_at TEXT
)
""")

db.commit()


# =========================================================
# RUNTIME
# =========================================================

active_spam_tasks = {}


converter_users = set()
audio_converter_users = set()

# =========================================================
# MOSCOW TIME
# =========================================================

def moscow_time(dt=None):
    from zoneinfo import ZoneInfo

    if dt is None:
        dt = datetime.now(timezone.utc)

    return dt.astimezone(
        ZoneInfo("Europe/Moscow")
    ).strftime("%d.%m.%Y %H:%M:%S")


# =========================================================
# HELPERS
# =========================================================

def safe(value):
    if value is None:
        return ""

    return html.escape(str(value))


def get_user_name(
    first_name,
    last_name,
    username
):
    name = " ".join(
        x
        for x in [first_name, last_name]
        if x
    )

    if not name:
        name = "Неизвестный пользователь"

    username_text = (
        f"@{username}"
        if username
        else "нет username"
    )

    return name, username_text


def get_settings(owner_id):

    cursor.execute("""
        SELECT
            delete_notifications,
            save_media,
            edit_notifications

        FROM settings

        WHERE owner_id = ?
    """, (
        owner_id,
    ))

    row = cursor.fetchone()

    if row is None:

        cursor.execute("""
            INSERT INTO settings (
                owner_id,
                delete_notifications,
                save_media,
                edit_notifications
            )

            VALUES (?, 1, 1, 1)
        """, (
            owner_id,
        ))

        db.commit()

        return True, True, True

    return (
        bool(row[0]),
        bool(row[1]),
        bool(row[2])
    )


async def get_business_connection(
    connection_id
):
    try:

        return await bot.get_business_connection(
            business_connection_id=connection_id
        )

    except Exception as error:

        print(
            "❌ get_business_connection:",
            repr(error)
        )

        return None


def save_connection(
    connection,
    last_chat_id=None
):

    if last_chat_id is None:

        cursor.execute("""
            INSERT INTO business_connections (
                connection_id,
                owner_id,
                user_chat_id,
                enabled,
                created_at
            )

            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(connection_id)

            DO UPDATE SET
                owner_id = excluded.owner_id,
                user_chat_id = excluded.user_chat_id,
                enabled = excluded.enabled
        """, (
            connection.id,
            connection.user.id,
            connection.user_chat_id,
            int(connection.is_enabled),
            datetime.now(
                timezone.utc
            ).isoformat()
        ))

    else:

        cursor.execute("""
            INSERT INTO business_connections (
                connection_id,
                owner_id,
                user_chat_id,
                enabled,
                created_at,
                last_chat_id
            )

            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(connection_id)

            DO UPDATE SET
                owner_id = excluded.owner_id,
                user_chat_id = excluded.user_chat_id,
                enabled = excluded.enabled,
                last_chat_id = excluded.last_chat_id
        """, (
            connection.id,
            connection.user.id,
            connection.user_chat_id,
            int(connection.is_enabled),
            datetime.now(
                timezone.utc
            ).isoformat(),
            last_chat_id
        ))

    cursor.execute("""
        INSERT OR IGNORE INTO settings (
            owner_id,
            delete_notifications,
            save_media,
            edit_notifications
        )

        VALUES (?, 1, 1, 1)
    """, (
        connection.user.id,
    ))

    db.commit()


def get_media_info(message):

    media_type = "text"
    file_id = None

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id

    elif message.video:
        media_type = "video"
        file_id = message.video.file_id

    elif message.voice:
        media_type = "voice"
        file_id = message.voice.file_id

    elif message.video_note:
        media_type = "video_note"
        file_id = message.video_note.file_id

    elif message.document:
        media_type = "document"
        file_id = message.document.file_id

    elif message.audio:
        media_type = "audio"
        file_id = message.audio.file_id

    elif message.animation:
        media_type = "animation"
        file_id = message.animation.file_id

    elif message.sticker:
        media_type = "sticker"
        file_id = message.sticker.file_id

    return media_type, file_id


def media_name(media_type):

    names = {
        "text": "💬 Текст",
        "photo": "📷 Фото",
        "video": "🎥 Видео",
        "voice": "🎤 Голосовое",
        "video_note": "🎬 Кружок",
        "document": "📄 Документ",
        "audio": "🎵 Аудио",
        "animation": "🖼 GIF",
        "sticker": "🧩 Стикер",
    }

    return names.get(
        media_type,
        media_type
    )


# =========================================================
# DIFF WITHOUT STRIKETHROUGH
# =========================================================

def make_diff_html(
    old_text,
    new_text
):

    old_text = old_text or "—"
    new_text = new_text or "—"

    return (
        safe(old_text),
        safe(new_text)
    )


# =========================================================
# MENU STATE
# =========================================================

def save_menu_state(
    owner_id,
    chat_id,
    message_id
):

    cursor.execute("""
        INSERT INTO menu_state (
            owner_id,
            chat_id,
            message_id,
            updated_at
        )

        VALUES (?, ?, ?, ?)

        ON CONFLICT(owner_id)

        DO UPDATE SET
            chat_id = excluded.chat_id,
            message_id = excluded.message_id,
            updated_at = excluded.updated_at
    """, (
        owner_id,
        chat_id,
        message_id,
        datetime.now(
            timezone.utc
        ).isoformat()
    ))

    db.commit()


def get_menu_state(owner_id):

    cursor.execute("""
        SELECT
            chat_id,
            message_id

        FROM menu_state

        WHERE owner_id = ?
    """, (
        owner_id,
    ))

    return cursor.fetchone()


def clear_menu_state(owner_id):

    cursor.execute("""
        DELETE FROM menu_state
        WHERE owner_id = ?
    """, (
        owner_id,
    ))

    db.commit()


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard(user_id=None):

    rows = [
        [
            InlineKeyboardButton(
                text="🚀 Подключить Business",
                callback_data="connect_business"
            )
        ],

        [
            InlineKeyboardButton(
                text="🟦 Мой Business",
                callback_data="my_business"
            )
        ],

        [
            InlineKeyboardButton(
                text="🗑 Удалённые",
                callback_data="open_deleted"
            ),

            InlineKeyboardButton(
                text="ℹ️ Изменённые",
                callback_data="open_edited"
            )
        ],

        [
            InlineKeyboardButton(
                text="💬 Команды",
                callback_data="open_commands"
            ),

            InlineKeyboardButton(
                text="📊 Статистика",
                callback_data="open_stats"
            )
        ],

    ],

    # Логи добавляются только если настроен ADMIN_ID.
    if ADMIN_ID:
        rows.append([
            InlineKeyboardButton(
                text="📋 Логи",
                callback_data="open_admin_logs"
            )
        ])

    rows.extend([

        [
            InlineKeyboardButton(
                text="⚙️ Настройки",
                callback_data="open_settings"
            )
        ],

        [
            InlineKeyboardButton(
                text="💎 Business Premium ↗",
                url=PREMIUM_URL
            )
        ],

        [
            InlineKeyboardButton(
                text="💡 Поддержка ↗",
                url=SUPPORT_URL
            )
        ]
    ])



    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def back_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="open_menu"
                )
            ]
        ]
    )



def commands_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text=".spam",
                    callback_data="command_spam"
                )
            ],

            [
                InlineKeyboardButton(
                    text=".copy",
                    callback_data="command_copy"
                )
            ],


            [
                InlineKeyboardButton(
                    text="🎬 Конвертер",
                    callback_data="open_converter"
                )
            ],

            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="open_menu"
                )
            ]

        ]
    )
def converter_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="🎥 Видео → кружок",
                    callback_data="video_to_circle"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎵 Извлечь звук",
                    callback_data="video_to_audio"
                )
            ],

            [
                InlineKeyboardButton(
                    text="⬅️ К командам",
                    callback_data="open_commands"
                )
            ]

        ]
    )



def audio_converter_wait_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel_audio_converter"
                )
            ]
        ]
    )


def converter_wait_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel_converter"
                )
            ]
        ]
    )


def settings_keyboard(
    delete_notifications,
    save_media,
    edit_notifications
):

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text=(
                        "🗑 Удаления: "
                        f"{'✅' if delete_notifications else '❌'}"
                    ),
                    callback_data="toggle_deletions"
                )
            ],

            [
                InlineKeyboardButton(
                    text=(
                        "📦 Медиа: "
                        f"{'✅' if save_media else '❌'}"
                    ),
                    callback_data="toggle_media"
                )
            ],

            [
                InlineKeyboardButton(
                    text=(
                        "✏️ Изменения: "
                        f"{'✅' if edit_notifications else '❌'}"
                    ),
                    callback_data="toggle_edits"
                )
            ],

            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="open_menu"
                )
            ]

        ]
    )


# =========================================================
# MENU TEXT
# =========================================================

def main_menu_text(
    first_name="друг"
):

    return (
        "🗃 <b>BUSINESS ARCHIVE</b>\n\n"

        f"Привет, <b>{safe(first_name)}</b>!\n\n"

        "Здесь можно настроить архиватор "
        "и управлять его функциями.\n\n"

        "Выберите раздел:"
    )


# =========================================================
# DELETE OLD MENU + SEND NEW MENU
# =========================================================

async def show_main_menu(
    owner_id,
    chat_id,
    first_name="друг"
):

    old_state = get_menu_state(
        owner_id
    )

    # -----------------------------------------------------
    # Удаляем старое меню
    # -----------------------------------------------------

    if old_state:

        old_chat_id, old_message_id = old_state

        try:

            await bot.delete_message(
                chat_id=old_chat_id,
                message_id=old_message_id
            )

            print(
                "🗑 Старое меню удалено"
            )

        except Exception as error:

            print(
                "⚠️ Не удалось удалить старое меню:",
                repr(error)
            )

    # -----------------------------------------------------
    # Создаём новое меню В САМОМ НИЗУ
    # -----------------------------------------------------

    try:

        new_message = await bot.send_message(
            chat_id=chat_id,
            text=main_menu_text(
                first_name
            ),
            parse_mode="HTML",
            reply_markup=main_keyboard(user_id=owner_id)
        )

    except TelegramNetworkError as error:

        print(
            "🌐 Telegram network error:",
            repr(error)
        )

        return None

    save_menu_state(
        owner_id,
        chat_id,
        new_message.message_id
    )

    return new_message


# =========================================================
# REPLACE CURRENT MENU SCREEN
# =========================================================

async def replace_menu(
    callback: CallbackQuery,
    text,
    keyboard
):

    owner_id = callback.from_user.id

    old_chat_id = callback.message.chat.id
    old_message_id = callback.message.message_id

    # -----------------------------------------------------
    # Сначала удаляем старый экран
    # -----------------------------------------------------

    try:

        await bot.delete_message(
            chat_id=old_chat_id,
            message_id=old_message_id
        )

    except Exception as error:

        print(
            "⚠️ Не удалось удалить старый экран:",
            repr(error)
        )

    # -----------------------------------------------------
    # Потом отправляем новый — он будет последним
    # -----------------------------------------------------

    try:

        new_message = await bot.send_message(
            chat_id=old_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except TelegramNetworkError as error:

        print(
            "🌐 Telegram network error:",
            repr(error)
        )

        await callback.answer(
            "🌐 Telegram временно недоступен."
        )

        return None

    save_menu_state(
        owner_id,
        old_chat_id,
        new_message.message_id
    )

    try:
        await callback.answer()
    except Exception:
        pass

    return new_message


# =========================================================
# BUSINESS CONNECTION
# =========================================================

@dp.business_connection()
async def business_connection_handler(
    connection: BusinessConnection
):

    save_connection(
        connection
    )

    print()
    print("=" * 60)
    print("🔗 BUSINESS CONNECTION")
    print("=" * 60)
    print(
        "Connection:",
        connection.id
    )
    print(
        "Owner:",
        connection.user.id
    )
    print(
        "User Chat:",
        connection.user_chat_id
    )
    print(
        "Enabled:",
        connection.is_enabled
    )
    print("=" * 60)


# =========================================================
# =========================================================
# .SAVE — отправка сохранённого медиа только админу
# =========================================================

# Укажи свой Telegram ID через переменную окружения ADMIN_ID.
# Например на хостинге:
# ADMIN_ID=123456789
#
# Локально можно временно указать число прямо здесь.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))


async def send_saved_to_admin(
    source_message: types.Message,
    reply: types.Message
):
    """Отправляет сохранённое сообщение только администратору."""

    if not ADMIN_ID:
        print("❌ ADMIN_ID не задан. .save отключён.")
        return

    connection_id = source_message.business_connection_id

    sender_id = (
        reply.from_user.id
        if reply.from_user
        else "неизвестно"
    )

    sender_name = (
        get_user_name(
            reply.from_user.first_name,
            reply.from_user.last_name,
            reply.from_user.username
        )[0]
        if reply.from_user
        else "Неизвестный пользователь"
    )

    header = (
        "💾 <b>СОХРАНЁННОЕ СООБЩЕНИЕ</b>\n\n"
        f"👤 <b>Отправитель:</b> {safe(sender_name)}\n"
        f"🆔 <b>User ID:</b> <code>{sender_id}</code>\n"
        f"💬 <b>Chat ID:</b> <code>{reply.chat.id}</code>\n"
        f"📨 <b>Message ID:</b> <code>{reply.message_id}</code>\n"
        f"🔗 <b>Connection:</b> <code>{safe(connection_id)}</code>\n"
    )

    text = reply.text or reply.caption or ""

    try:

        # -----------------------------------------------------
        # PHOTO
        # -----------------------------------------------------

        if reply.photo:

            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=reply.photo[-1].file_id,
                caption=(
                    header +
                    (
                        f"\n💬 <b>Текст:</b>\n"
                        f"<blockquote>{safe(text or '—')}</blockquote>"
                    )
                ),
                parse_mode="HTML"
            )

            return

        # -----------------------------------------------------
        # VIDEO
        # -----------------------------------------------------

        if reply.video:

            await bot.send_video(
                chat_id=ADMIN_ID,
                video=reply.video.file_id,
                caption=(
                    header +
                    (
                        f"\n💬 <b>Текст:</b>\n"
                        f"<blockquote>{safe(text or '—')}</blockquote>"
                    )
                ),
                parse_mode="HTML"
            )

            return

        # -----------------------------------------------------
        # DOCUMENT
        # -----------------------------------------------------

        if reply.document:

            await bot.send_document(
                chat_id=ADMIN_ID,
                document=reply.document.file_id,
                caption=(
                    header +
                    (
                        f"\n💬 <b>Текст:</b>\n"
                        f"<blockquote>{safe(text or '—')}</blockquote>"
                    )
                ),
                parse_mode="HTML"
            )

            return

        # -----------------------------------------------------
        # AUDIO
        # -----------------------------------------------------

        if reply.audio:

            await bot.send_audio(
                chat_id=ADMIN_ID,
                audio=reply.audio.file_id,
                caption=(
                    header +
                    (
                        f"\n💬 <b>Текст:</b>\n"
                        f"<blockquote>{safe(text or '—')}</blockquote>"
                    )
                ),
                parse_mode="HTML"
            )

            return

        # -----------------------------------------------------
        # VOICE
        # -----------------------------------------------------

        if reply.voice:

            await bot.send_voice(
                chat_id=ADMIN_ID,
                voice=reply.voice.file_id,
                caption=(
                    header +
                    (
                        f"\n💬 <b>Текст:</b>\n"
                        f"<blockquote>{safe(text or '—')}</blockquote>"
                    )
                ),
                parse_mode="HTML"
            )

            return

        # -----------------------------------------------------
        # VIDEO NOTE
        # -----------------------------------------------------

        if reply.video_note:

            await bot.send_message(
                chat_id=ADMIN_ID,
                text=header + "\n🎬 <b>Тип:</b> Кружок",
                parse_mode="HTML"
            )

            await bot.send_video_note(
                chat_id=ADMIN_ID,
                video_note=reply.video_note.file_id
            )

            return

        # -----------------------------------------------------
        # ANIMATION / GIF
        # -----------------------------------------------------

        if reply.animation:

            await bot.send_animation(
                chat_id=ADMIN_ID,
                animation=reply.animation.file_id,
                caption=(
                    header +
                    (
                        f"\n💬 <b>Текст:</b>\n"
                        f"<blockquote>{safe(text or '—')}</blockquote>"
                    )
                ),
                parse_mode="HTML"
            )

            return

        # -----------------------------------------------------
        # STICKER
        # -----------------------------------------------------

        if reply.sticker:

            await bot.send_message(
                chat_id=ADMIN_ID,
                text=header + "\n🧩 <b>Тип:</b> Стикер",
                parse_mode="HTML"
            )

            await bot.send_sticker(
                chat_id=ADMIN_ID,
                sticker=reply.sticker.file_id
            )

            return

        # -----------------------------------------------------
        # TEXT
        # -----------------------------------------------------

        if text:

            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    header +
                    "\n💬 <b>Текст:</b>\n"
                    f"<blockquote>{safe(text)}</blockquote>"
                ),
                parse_mode="HTML"
            )

            return

        # -----------------------------------------------------
        # UNKNOWN
        # -----------------------------------------------------

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                header +
                "\n⚠️ <b>Неизвестный тип сообщения.</b>"
            ),
            parse_mode="HTML"
        )

    except Exception as error:

        print()
        print("❌ SAVE ADMIN ERROR:")
        print(repr(error))
        print()

        try:

            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "❌ <b>Ошибка сохранения</b>\n\n"
                    f"<code>{safe(error)}</code>"
                ),
                parse_mode="HTML"
            )

        except Exception:
            pass


@dp.business_message(
    F.text == ".save"
)
async def save_command(
    message: types.Message
):

    print()
    print("=" * 60)
    print("💾 SAVE COMMAND")
    print("=" * 60)
    print(
        "Connection:",
        message.business_connection_id
    )
    print(
        "Chat:",
        message.chat.id
    )
    print(
        "Message:",
        message.message_id
    )

    if not ADMIN_ID:

        print(
            "❌ ADMIN_ID не задан."
        )

        return

    reply = message.reply_to_message

    if reply is None:

        try:

            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "❌ <b>.save</b>\n\n"
                    "Команда должна быть ответом "
                    "на сообщение."
                ),
                parse_mode="HTML"
            )

        except Exception as error:

            print(
                "❌ SAVE ERROR:",
                repr(error)
            )

        return

    print(
        "REPLY TYPE:",
        type(reply)
    )

    print(
        "PHOTO:",
        reply.photo
    )

    print(
        "VIDEO:",
        reply.video
    )

    print(
        "DOCUMENT:",
        reply.document
    )

    try:

        await send_saved_to_admin(
            source_message=message,
            reply=reply
        )

        print(
            "✅ SAVE отправлен только админу"
        )

    except Exception as error:

        print(
            "❌ SAVE ERROR:",
            repr(error)
        )

    print("=" * 60)
    print()


# .SPAM
# =========================================================

@dp.business_message(
    F.text.startswith(".spam")
)
async def spam_command(
    message: types.Message
):

    connection_id = (
        message.business_connection_id
    )

    chat_id = message.chat.id

    connection = await get_business_connection(
        connection_id
    )

    if connection is None:
        return

    owner_id = connection.user.id

    save_connection(
        connection,
        last_chat_id=chat_id
    )

    parts = message.text.split(
        maxsplit=2
    )

    if len(parts) < 3:

        await bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ <b>Формат:</b>\n\n"
                "<code>.spam N текст</code>\n\n"
                "Чтобы удалить сообщения отправленные командой, .bspam \n\n"
                "Пример:\n"
                "<code>.spam 3 Привет</code>"
            ),
            parse_mode="HTML",
            business_connection_id=connection_id
        )

        return

    try:

        count = int(
            parts[1]
        )

    except ValueError:

        await bot.send_message(
            chat_id=chat_id,
            text="❌ N должно быть числом.",
            business_connection_id=connection_id
        )

        return

    text = parts[2].strip()

    if count < 1 or count > MAX_SPAM_COUNT:

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Количество: "
                f"от 1 до {MAX_SPAM_COUNT}."
            ),
            business_connection_id=connection_id
        )

        return

    if not text:
        return

    if owner_id in active_spam_tasks:
        return

    # -----------------------------------------------------
    # Удаляем команду сразу
    # -----------------------------------------------------

    try:

        await bot.delete_business_messages(
            business_connection_id=connection_id,
            message_ids=[
                message.message_id
            ]
        )

    except Exception as error:

        print(
            "⚠️ Не удалось удалить .spam:",
            repr(error)
        )

    stop_event = asyncio.Event()

    active_spam_tasks[
        owner_id
    ] = stop_event

    try:

        for _ in range(count):

            if stop_event.is_set():
                break

            try:

                result = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    business_connection_id=connection_id
                )

                cursor.execute("""
                    INSERT INTO spam_messages (
                        owner_id,
                        connection_id,
                        chat_id,
                        message_id,
                        created_at
                    )

                    VALUES (?, ?, ?, ?, ?)
                """, (
                    owner_id,
                    connection_id,
                    chat_id,
                    result.message_id,
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ))

                db.commit()

                print(
                    f"✅ SPAM "
                    f"{result.message_id}"
                )

                await asyncio.sleep(
                    SPAM_DELAY
                )

            except Exception as error:

                print(
                    "❌ SPAM ERROR:",
                    repr(error)
                )

                try:

                    await bot.send_message(
                        chat_id=connection.user_chat_id,
                        text=(
                            "❌ <b>SPAM ERROR</b>\n\n"
                            f"<code>{safe(error)}</code>"
                        ),
                        parse_mode="HTML"
                    )

                except Exception:
                    pass

                break

    finally:

        active_spam_tasks.pop(
            owner_id,
            None
        )


# =========================================================
# STOP SPAM
# =========================================================

@dp.callback_query(
    lambda c: c.data == "stop_spam"
)
async def stop_spam(
    callback: CallbackQuery
):

    owner_id = (
        callback.from_user.id
    )

    event = active_spam_tasks.get(
        owner_id
    )

    if event is None:

        await callback.answer(
            "Активного spam нет.",
            show_alert=True
        )

        return

    event.set()

    await callback.answer(
        "⏹ Останавливаю..."
    )


# =========================================================
# .BSPAM
# =========================================================

@dp.business_message(
    F.text == ".bspam"
)
async def bspam_command(
    message: types.Message
):

    connection_id = (
        message.business_connection_id
    )

    chat_id = message.chat.id

    connection = await get_business_connection(
        connection_id
    )

    if connection is None:
        return

    owner_id = connection.user.id

    cursor.execute("""
        SELECT message_id

        FROM spam_messages

        WHERE owner_id = ?
          AND connection_id = ?
          AND chat_id = ?

        ORDER BY id ASC
    """, (
        owner_id,
        connection_id,
        chat_id
    ))

    rows = cursor.fetchall()

    message_ids = [
        row[0]
        for row in rows
    ]

    if not message_ids:
        return

    # Удаляем команду
    try:

        await bot.delete_business_messages(
            business_connection_id=connection_id,
            message_ids=[
                message.message_id
            ]
        )

    except Exception as error:

        print(
            "⚠️ Не удалось удалить .bspam:",
            repr(error)
        )

    # Удаляем пачками
    for start in range(
        0,
        len(message_ids),
        100
    ):

        chunk = message_ids[
            start:start + 100
        ]

        try:

            await bot.delete_business_messages(
                business_connection_id=connection_id,
                message_ids=chunk
            )

            placeholders = ",".join(
                "?"
                for _ in chunk
            )

            cursor.execute(
                f"""
                DELETE FROM spam_messages

                WHERE owner_id = ?

                  AND connection_id = ?

                  AND chat_id = ?

                  AND message_id
                  IN ({placeholders})
                """,
                [
                    owner_id,
                    connection_id,
                    chat_id,
                    *chunk
                ]
            )

            db.commit()

        except Exception as error:

            print(
                "❌ BSPAM ERROR:",
                repr(error)
            )


# =========================================================
# CONVERTER MENU
# =========================================================

@dp.callback_query(
    lambda c: c.data == "open_converter"
)
async def open_converter(
    callback: CallbackQuery
):

    await replace_menu(
        callback,

        "🎬 <b>Конвертер</b>\n\n"
        "Выберите действие:",

        converter_keyboard()
    )


# =========================================================
# VIDEO -> CIRCLE
# =========================================================

@dp.callback_query(
    lambda c: c.data == "video_to_circle"
)
async def video_to_circle(
    callback: CallbackQuery
):

    converter_users.add(
        callback.from_user.id
    )

    await replace_menu(
        callback,

        "🎥 <b>Видео → кружок</b>\n\n"

        "Пришлите видео, которое нужно "
        "преобразовать в Telegram-кружок 🎬\n\n"

        "⏱ Максимальная длительность: "
        "<b>60 секунд</b>.",

        converter_wait_keyboard()
    )


# =========================================================
# CANCEL CONVERTER
# =========================================================

@dp.callback_query(
    lambda c: c.data == "cancel_converter"
)
async def cancel_converter(
    callback: CallbackQuery
):

    converter_users.discard(
        callback.from_user.id
    )

    await show_main_menu(
        owner_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        first_name=(
            callback.from_user.first_name
            or "друг"
        )
    )

    try:
        await callback.answer(
            "Конвертация отменена"
        )
    except Exception:
        pass


# =========================================================
# VIDEO -> AUDIO
# =========================================================

@dp.callback_query(
    lambda c: c.data == "video_to_audio"
)
async def video_to_audio(
    callback: CallbackQuery
):

    audio_converter_users.add(
        callback.from_user.id
    )

    await replace_menu(
        callback,
        (
            "🎵 <b>Извлечь звук из видео</b>\n\n"
            "Пришлите видео — бот извлечёт "
            "аудиодорожку и отправит её как MP3.\n\n"
            "⏱ Максимальная длительность: "
            "<b>60 секунд</b>."
        ),
        audio_converter_wait_keyboard()
    )


@dp.callback_query(
    lambda c: c.data == "cancel_audio_converter"
)
async def cancel_audio_converter(
    callback: CallbackQuery
):

    audio_converter_users.discard(
        callback.from_user.id
    )

    await show_main_menu(
        owner_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        first_name=(
            callback.from_user.first_name
            or "друг"
        )
    )

    try:
        await callback.answer(
            "Извлечение аудио отменено"
        )
    except Exception:
        pass


@dp.message(F.video)
async def extract_audio_handler(
    message: types.Message
):

    user_id = (
        message.from_user.id
        if message.from_user
        else 0
    )

    if user_id not in audio_converter_users:
        return

    audio_converter_users.discard(
        user_id
    )

    duration = (
        message.video.duration
        or 0
    )

    actual_ffmpeg = (
        FFMPEG_PATH
        if os.path.isfile(FFMPEG_PATH)
        else shutil.which(FFMPEG_PATH)
    )

    if not actual_ffmpeg:
        await message.answer(
            "❌ <b>FFmpeg не найден.</b>",
            parse_mode="HTML"
        )
        return

    if duration > MAX_VIDEO_DURATION:
        await message.answer(
            "❌ Видео длиннее 60 секунд.",
            parse_mode="HTML"
        )
        return

    if (
        message.video.file_size
        and message.video.file_size > MAX_VIDEO_SIZE
    ):
        await message.answer(
            "❌ Видео слишком большое.\n"
            "Попробуй файл меньше 20 МБ."
        )
        return

    unique_id = uuid.uuid4().hex

    input_file = (
        f"audio_input_{unique_id}.mp4"
    )

    output_file = (
        f"audio_output_{unique_id}.mp3"
    )

    status = None

    try:

        status = await message.answer(
            "⏳ <b>Извлекаю звук...</b>\n\n"
            "📥 Получаю видео\n"
            "🎵 Извлекаю аудио\n"
            "📤 Готовлю MP3",
            parse_mode="HTML"
        )

        telegram_file = await bot.get_file(
            message.video.file_id
        )

        if not telegram_file.file_path:
            raise RuntimeError(
                "Telegram не вернул путь к видео."
            )

        await bot.download_file(
            telegram_file.file_path,
            destination=input_file
        )

        ffmpeg_command = [
            actual_ffmpeg,
            "-y",
            "-nostdin",
            "-i",
            input_file,
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-ar",
            "44100",
            output_file
        ]

        process = await asyncio.create_subprocess_exec(
            *ffmpeg_command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=120
            )
        except asyncio.TimeoutError:
            process.kill()
            try:
                await process.wait()
            except Exception:
                pass
            raise RuntimeError(
                "FFmpeg слишком долго обрабатывал видео."
            )

        if process.returncode != 0:
            error_text = stderr.decode(
                errors="ignore"
            )
            print(
                "❌ AUDIO FFMPEG ERROR:",
                error_text
            )
            raise RuntimeError(
                "FFmpeg не смог извлечь аудио."
            )

        if not os.path.isfile(output_file):
            raise RuntimeError(
                "Готовый MP3-файл не найден."
            )

        try:
            await status.delete()
        except Exception:
            pass

        await bot.send_audio(
            chat_id=message.chat.id,
            audio=FSInputFile(output_file),
            title="Извлечённое аудио",
            performer="SaveBot",
            duration=int(duration)
        )

        try:
            await message.delete()
        except Exception as error:
            print(
                "⚠️ Не удалось удалить исходное видео:",
                repr(error)
            )

        await show_main_menu(
            owner_id=user_id,
            chat_id=message.chat.id,
            first_name=(
                message.from_user.first_name
                or "друг"
            )
        )

    except Exception as error:

        print(
            "❌ AUDIO CONVERTER ERROR:",
            repr(error)
        )

        try:
            if status:
                await status.delete()
        except Exception:
            pass

        try:
            await message.answer(
                "❌ <b>Ошибка извлечения аудио.</b>\n\n"
                f"<code>{safe(error)}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    finally:

        for filename in [
            input_file,
            output_file
        ]:
            try:
                if os.path.exists(filename):
                    os.remove(filename)
            except Exception:
                pass


# =========================================================
# VIDEO CONVERTER
# =========================================================

@dp.message(
    F.video
)
async def convert_video_handler(
    message: types.Message
):

    user_id = (
        message.from_user.id
    )

    if user_id not in converter_users:
        return

    converter_users.discard(
        user_id
    )

    duration = (
        message.video.duration
        or 0
    )

    # -----------------------------------------------------
    # Проверка FFmpeg
    # -----------------------------------------------------

    if not os.path.isfile(
        FFMPEG_PATH
    ):

        await message.answer(
            "❌ <b>FFmpeg не найден.</b>\n\n"
            f"<code>{safe(FFMPEG_PATH)}</code>",
            parse_mode="HTML"
        )

        return

    # -----------------------------------------------------
    # Длительность
    # -----------------------------------------------------

    if duration > MAX_VIDEO_DURATION:

        await message.answer(
            "❌ Видео длиннее 60 секунд.\n\n"
            "Для кружка максимальная длина — "
            "<b>60 секунд</b>.",
            parse_mode="HTML"
        )

        return

    # -----------------------------------------------------
    # Размер
    # -----------------------------------------------------

    if (
        message.video.file_size
        and
        message.video.file_size
        > MAX_VIDEO_SIZE
    ):

        await message.answer(
            "❌ Видео слишком большое.\n\n"
            "Попробуй файл меньше 20 МБ."
        )

        return

    unique_id = (
        uuid.uuid4().hex
    )

    input_file = (
        f"video_input_{unique_id}.mp4"
    )

    output_file = (
        f"video_circle_{unique_id}.mp4"
    )

    status = None

    try:

        # -------------------------------------------------
        # STATUS
        # -------------------------------------------------

        status = await message.answer(
            "⏳ <b>Конвертирую видео...</b>\n\n"
            "📥 Получаю файл\n"
            "⚙️ Обрабатываю\n"
            "🎬 Готовлю кружок",
            parse_mode="HTML"
        )

        # -------------------------------------------------
        # GET FILE
        # -------------------------------------------------

        telegram_file = await bot.get_file(
            message.video.file_id
        )

        if not telegram_file.file_path:

            await status.edit_text(
                "❌ Telegram не вернул путь к видео."
            )

            return

        # -------------------------------------------------
        # DOWNLOAD
        # -------------------------------------------------

        await bot.download_file(
            telegram_file.file_path,
            destination=input_file
        )

        # -------------------------------------------------
        # FFMPEG
        # -------------------------------------------------

        actual_ffmpeg = (
            FFMPEG_PATH
            if os.path.isfile(FFMPEG_PATH)
            else shutil.which(FFMPEG_PATH)
        )

        if not actual_ffmpeg:
            raise RuntimeError(
                "FFmpeg не установлен на сервере."
            )

        ffmpeg_command = [

            actual_ffmpeg,

            "-y",

            "-i",
            input_file,

            "-vf",
            (
                "scale="
                "640:640:"
                "force_original_aspect_ratio=increase,"
                "crop=640:640"
            ),

            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-crf",
            "23",

            "-pix_fmt",
            "yuv420p",

            # Сохраняем звук
            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-t",
            "60",

            "-movflags",
            "+faststart",

            output_file
        ]

        print()
        print("=" * 60)
        print("🎬 FFMPEG")
        print("=" * 60)
        print(
            "FFmpeg:",
            FFMPEG_PATH
        )
        print(
            "Input:",
            input_file
        )
        print(
            "Output:",
            output_file
        )
        print("=" * 60)

        # -------------------------------------------------
        # PROCESS
        # -------------------------------------------------

        process = (
            await asyncio.create_subprocess_exec(
                *ffmpeg_command,

                stdout=asyncio.subprocess.PIPE,

                stderr=asyncio.subprocess.PIPE
            )
        )

        stdout, stderr = (
            await process.communicate()
        )

        if process.returncode != 0:

            error_text = stderr.decode(
                errors="ignore"
            )

            print(
                "❌ FFMPEG ERROR:"
            )

            print(
                error_text
            )

            try:

                await status.edit_text(
                    "❌ <b>Ошибка FFmpeg.</b>\n\n"
                    f"<code>{safe(error_text[-2000:])}</code>",
                    parse_mode="HTML"
                )

            except Exception:
                pass

            return

        # -------------------------------------------------
        # RESULT
        # -------------------------------------------------

        if not os.path.isfile(
            output_file
        ):

            await status.edit_text(
                "❌ Готовый файл не найден."
            )

            return

        output_size = os.path.getsize(
            output_file
        )

        print(
            "✅ FFMPEG OK"
        )

        print(
            "Output size:",
            output_size
        )

        # -------------------------------------------------
        # DELETE STATUS
        # -------------------------------------------------

        try:

            await status.delete()

        except Exception:
            pass

        # -------------------------------------------------
        # SEND CIRCLE
        # -------------------------------------------------

        video_note = FSInputFile(
            output_file
        )

        await bot.send_video_note(
            chat_id=message.chat.id,
            video_note=video_note,
            length=VIDEO_NOTE_SIZE,
            duration=min(
                duration,
                MAX_VIDEO_DURATION
            )
        )

        print(
            "✅ Кружок отправлен"
        )

        # -------------------------------------------------
        # DELETE ORIGINAL VIDEO
        # -------------------------------------------------

        try:

            await message.delete()

            print(
                "🗑 Исходное видео удалено"
            )

        except Exception as error:

            print(
                "⚠️ Не удалось удалить исходное видео:",
                repr(error)
            )

        # -------------------------------------------------
        # MENU LAST
        # -------------------------------------------------

        await show_main_menu(
            owner_id=user_id,
            chat_id=message.chat.id,
            first_name=(
                message.from_user.first_name
                or "друг"
            )
        )

    except TelegramNetworkError as error:

        print(
            "🌐 TELEGRAM NETWORK ERROR:",
            repr(error)
        )

        try:

            if status:
                await status.delete()

        except Exception:
            pass

        await message.answer(
            "🌐 <b>Telegram временно недоступен.</b>\n\n"
            "Попробуй ещё раз через несколько секунд.",
            parse_mode="HTML"
        )

    except Exception as error:

        print()
        print(
            "❌ CONVERTER ERROR:"
        )
        print(
            repr(error)
        )
        print()

        try:

            if status:
                await status.delete()

        except Exception:
            pass

        try:

            await message.answer(
                "❌ <b>Ошибка конвертации.</b>\n\n"
                f"<code>{safe(error)}</code>",
                parse_mode="HTML"
            )

        except Exception:
            pass

    finally:

        # -------------------------------------------------
        # CLEANUP
        # -------------------------------------------------

        for filename in [
            input_file,
            output_file
        ]:

            try:

                if os.path.exists(
                    filename
                ):

                    os.remove(
                        filename
                    )

            except Exception as error:

                print(
                    "⚠️ Не удалось удалить:",
                    filename,
                    repr(error)
                )



# =========================================================
# BUSINESS PROFILE COPY / BACK
# =========================================================

def profile_backup_path(owner_id):
    return os.path.abspath(
        f"profile_backup_{owner_id}.jpg"
    )


async def download_profile_photo_by_user_id(
    user_id,
    destination
):
    photos = await bot.get_user_profile_photos(
        user_id=user_id,
        offset=0,
        limit=1
    )

    if not photos.photos:
        return False

    largest = photos.photos[0][-1]

    telegram_file = await bot.get_file(
        largest.file_id
    )

    if not telegram_file.file_path:
        return False

    await bot.download_file(
        telegram_file.file_path,
        destination=destination
    )

    return os.path.exists(destination)


async def backup_business_profile(connection):
    owner_id = connection.user.id

    current = await bot.get_chat(
        chat_id=owner_id
    )

    photo_path = profile_backup_path(
        owner_id
    )

    try:
        if os.path.exists(photo_path):
            os.remove(photo_path)
    except Exception:
        pass

    has_photo = 0

    try:
        has_photo = int(
            await download_profile_photo_by_user_id(
                owner_id,
                photo_path
            )
        )
    except Exception as error:
        print(
            "⚠️ BACKUP PHOTO ERROR:",
            repr(error)
        )

    cursor.execute("""
        INSERT INTO profile_backup (
            owner_id,
            first_name,
            last_name,
            bio,
            photo_path,
            has_photo,
            saved_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(owner_id)
        DO UPDATE SET
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            bio = excluded.bio,
            photo_path = excluded.photo_path,
            has_photo = excluded.has_photo,
            saved_at = excluded.saved_at
    """, (
        owner_id,
        current.first_name or "",
        current.last_name or "",
        getattr(current, "bio", "") or "",
        photo_path if has_photo else None,
        has_photo,
        datetime.now(timezone.utc).isoformat()
    ))

    db.commit()


async def get_target_profile(target_user_id):
    target = await bot.get_chat(
        chat_id=target_user_id
    )

    photo_path = os.path.abspath(
        f"profile_source_{uuid.uuid4().hex}.jpg"
    )

    has_photo = False

    try:
        has_photo = await download_profile_photo_by_user_id(
            target_user_id,
            photo_path
        )
    except Exception as error:
        print(
            "⚠️ TARGET PHOTO ERROR:",
            repr(error)
        )

    return target, (
        photo_path
        if has_photo
        else None
    )


async def copy_business_profile(
    connection,
    target_user_id
):
    connection_id = connection.id
    rights = connection.rights

    if rights:

        can_name = (
            getattr(rights, "can_edit_name", None)
            if hasattr(rights, "can_edit_name")
            else getattr(rights, "can_change_name", False)
        )

        can_bio = (
            getattr(rights, "can_edit_bio", None)
            if hasattr(rights, "can_edit_bio")
            else getattr(rights, "can_change_bio", False)
        )

        can_photo = getattr(
            rights,
            "can_edit_profile_photo",
            False
        )

        print(
            "Business rights:",
            "name=", can_name,
            "bio=", can_bio,
            "photo=", can_photo
        )

        if can_name is False:
            return False, "Нет права изменять имя."

        if can_bio is False:
            return False, "Нет права изменять био."

        if can_photo is False:
            return False, "Нет права изменять аватар."

    # Сохраняем исходный Business-профиль.
    await backup_business_profile(
        connection
    )

    target, source_photo = (
        await get_target_profile(
            target_user_id
        )
    )

    try:

        await bot.set_business_account_name(
            business_connection_id=connection_id,
            first_name=(
                target.first_name
                or "User"
            ),
            last_name=(
                target.last_name
                or ""
            )
        )

        await bot.set_business_account_bio(
            business_connection_id=connection_id,
            bio=(
                getattr(
                    target,
                    "bio",
                    ""
                )
                or ""
            )[:140]
        )

        if source_photo:

            await bot.set_business_account_profile_photo(
                business_connection_id=connection_id,
                photo=InputProfilePhotoStatic(
                    photo=FSInputFile(
                        source_photo
                    )
                )
            )

        else:

            try:
                await bot.remove_business_account_profile_photo(
                    business_connection_id=connection_id
                )
            except Exception as error:
                print(
                    "⚠️ REMOVE TARGET PHOTO ERROR:",
                    repr(error)
                )

    finally:

        if source_photo:

            try:
                os.remove(
                    source_photo
                )
            except Exception:
                pass

    return True, "✅ Профиль скопирован."


async def restore_business_profile(
    connection
):
    owner_id = connection.user.id
    connection_id = connection.id

    cursor.execute("""
        SELECT
            first_name,
            last_name,
            bio,
            photo_path,
            has_photo

        FROM profile_backup

        WHERE owner_id = ?
    """, (
        owner_id,
    ))

    row = cursor.fetchone()

    if not row:
        return False, "❌ Сохранённого профиля нет."

    (
        first_name,
        last_name,
        bio,
        photo_path,
        has_photo
    ) = row

    rights = connection.rights

    if rights:

        can_name = (
            getattr(rights, "can_edit_name", None)
            if hasattr(rights, "can_edit_name")
            else getattr(rights, "can_change_name", False)
        )

        can_bio = (
            getattr(rights, "can_edit_bio", None)
            if hasattr(rights, "can_edit_bio")
            else getattr(rights, "can_change_bio", False)
        )

        can_photo = getattr(
            rights,
            "can_edit_profile_photo",
            False
        )

        print(
            "Business rights:",
            "name=", can_name,
            "bio=", can_bio,
            "photo=", can_photo
        )

        if can_name is False:
            return False, "Нет права изменять имя."

        if can_bio is False:
            return False, "Нет права изменять био."

        if can_photo is False:
            return False, "Нет права изменять аватар."

    await bot.set_business_account_name(
        business_connection_id=connection_id,
        first_name=first_name or "User",
        last_name=last_name or ""
    )

    await bot.set_business_account_bio(
        business_connection_id=connection_id,
        bio=(bio or "")[:140]
    )

    if (
        has_photo
        and photo_path
        and os.path.exists(photo_path)
    ):

        await bot.set_business_account_profile_photo(
            business_connection_id=connection_id,
            photo=InputProfilePhotoStatic(
                photo=FSInputFile(
                    photo_path
                )
            )
        )

    else:

        try:
            await bot.remove_business_account_profile_photo(
                business_connection_id=connection_id
            )
        except Exception as error:
            print(
                "⚠️ REMOVE OLD PHOTO ERROR:",
                repr(error)
            )

    return True, "✅ Исходный профиль восстановлен."


# =========================================================
# .COPY
# =========================================================

@dp.business_message(F.text == ".copy")
async def copy_profile_command(
    message: types.Message
):

    reply = message.reply_to_message

    if not reply or not reply.from_user:

        print(
            "❌ .copy должен быть ответом "
            "на сообщение собеседника."
        )

        return

    connection = await get_business_connection(
        message.business_connection_id
    )

    if not connection:

        print(
            "❌ Business connection не найден."
        )

        return

    print()
    print("=" * 60)
    print("👤 COPY PROFILE")
    print("Connection:", connection.id)
    print("Owner:", connection.user.id)
    print("Target:", reply.from_user.id)
    print("=" * 60)

    try:

        ok, info = await copy_business_profile(
            connection,
            reply.from_user.id
        )

        print(
            info
        )

    except Exception as error:

        print(
            "❌ COPY ERROR:",
            repr(error)
        )


# =========================================================
# .BACK
# =========================================================

@dp.business_message(F.text == ".back")
async def back_profile_command(
    message: types.Message
):

    connection = await get_business_connection(
        message.business_connection_id
    )

    if not connection:

        print(
            "❌ Business connection не найден."
        )

        return

    print()
    print("=" * 60)
    print("↩️ BACK PROFILE")
    print("Connection:", connection.id)
    print("Owner:", connection.user.id)
    print("=" * 60)

    try:

        ok, info = await restore_business_profile(
            connection
        )

        print(
            info
        )

    except Exception as error:

        print(
            "❌ BACK ERROR:",
            repr(error)
        )



# =========================================================
# ORDINARY BUSINESS MESSAGE
# =========================================================

@dp.business_message()
async def business_message_handler(
    message: types.Message
):

    connection = await get_business_connection(
        message.business_connection_id
    )

    if connection is None:
        return

    save_connection(
        connection,
        last_chat_id=message.chat.id
    )

    owner_id = connection.user.id

    _, save_media, _ = get_settings(
        owner_id
    )

    user_id = None
    username = None
    first_name = None
    last_name = None

    if message.from_user:

        user_id = message.from_user.id
        username = message.from_user.username
        first_name = message.from_user.first_name
        last_name = message.from_user.last_name

    text = (
        message.text
        or message.caption
        or ""
    )

    media_type, file_id = get_media_info(
        message
    )

    if (
        not save_media
        and media_type != "text"
    ):

        media_type = "text"
        file_id = None

    cursor.execute("""
        INSERT OR REPLACE INTO messages (

            connection_id,
            chat_id,
            message_id,

            user_id,
            username,
            first_name,
            last_name,

            text,
            date,

            media_type,
            file_id,

            deleted
        )

        VALUES (
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?,
            ?, ?,
            0
        )
    """, (
        message.business_connection_id,
        message.chat.id,
        message.message_id,

        user_id,
        username,
        first_name,
        last_name,

        text,
        message.date.isoformat(),

        media_type,
        file_id
    ))

    db.commit()

    print(
        f"💬 Сохранено: "
        f"{media_type} | "
        f"{username or user_id} | "
        f"{text}"
    )
# =========================================================
# EDITED BUSINESS MESSAGE
# =========================================================

@dp.edited_business_message()
async def edited_business_message_handler(
    message: types.Message
):

    connection = await get_business_connection(
        message.business_connection_id
    )

    if connection is None:
        return

    save_connection(
        connection,
        last_chat_id=message.chat.id
    )

    owner_id = connection.user.id
    user_chat_id = connection.user_chat_id

    _, save_media, edit_notifications = get_settings(
        owner_id
    )

    cursor.execute("""
        SELECT
            user_id,
            username,
            first_name,
            last_name,
            text

        FROM messages

        WHERE connection_id = ?
          AND chat_id = ?
          AND message_id = ?
    """, (
        message.business_connection_id,
        message.chat.id,
        message.message_id
    ))

    old_row = cursor.fetchone()

    if old_row:

        (
            old_user_id,
            old_username,
            old_first_name,
            old_last_name,
            old_text
        ) = old_row

    else:

        old_user_id = None
        old_username = None
        old_first_name = None
        old_last_name = None

        old_text = ""

    new_text = (
        message.text
        or message.caption
        or ""
    )

    user_id = (
        message.from_user.id
        if message.from_user
        else old_user_id
    )

    username = (
        message.from_user.username
        if message.from_user
        else old_username
    )

    first_name = (
        message.from_user.first_name
        if message.from_user
        else old_first_name
    )

    last_name = (
        message.from_user.last_name
        if message.from_user
        else old_last_name
    )

    old_text_html, new_text_html = (
        make_diff_html(
            old_text,
            new_text
        )
    )

    name, _ = get_user_name(
        first_name,
        last_name,
        username
    )

    notification = (
        f"♻️ <b>{safe(name)}</b> "
        f"изменил(а) сообщение\n\n"

        f"❌ <b>Было:</b>\n"
        f"<blockquote>"
        f"{old_text_html}"
        f"</blockquote>\n\n"

        f"✅ <b>Стало:</b>\n"
        f"<blockquote>"
        f"{new_text_html}"
        f"</blockquote>"
    )

    media_type, file_id = get_media_info(
        message
    )

    if not save_media and media_type != "text":

        media_type = "text"
        file_id = None

    cursor.execute("""
        INSERT OR REPLACE INTO messages (

            connection_id,
            chat_id,
            message_id,

            user_id,
            username,
            first_name,
            last_name,

            text,
            date,

            media_type,
            file_id,

            deleted
        )

        VALUES (
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?,
            ?, ?,
            0
        )
    """, (
        message.business_connection_id,
        message.chat.id,
        message.message_id,

        user_id,
        username,
        first_name,
        last_name,

        new_text,
        message.date.isoformat(),

        media_type,
        file_id
    ))

    db.commit()

    if not edit_notifications:
        return

    try:

        await bot.send_message(
            chat_id=user_chat_id,
            text=notification,
            parse_mode="HTML"
        )

    except TelegramNetworkError as error:

        print(
            "🌐 EDIT NETWORK ERROR:",
            repr(error)
        )

    except Exception as error:

        print(
            "❌ EDIT ERROR:",
            repr(error)
        )


# =========================================================
# DELETED BUSINESS MESSAGES
# =========================================================

@dp.deleted_business_messages()
async def deleted_business_messages_handler(
    event: types.BusinessMessagesDeleted
):

    connection = await get_business_connection(
        event.business_connection_id
    )

    if connection is None:
        return

    owner_id = connection.user.id
    user_chat_id = connection.user_chat_id

    delete_notifications, _, _ = get_settings(
        owner_id
    )

    if not delete_notifications:
        return

    for message_id in event.message_ids:

        cursor.execute("""
            SELECT
                user_id,
                username,
                first_name,
                last_name,
                text,
                date,
                media_type,
                file_id

            FROM messages

            WHERE connection_id = ?
              AND chat_id = ?
              AND message_id = ?
        """, (
            event.business_connection_id,
            event.chat.id,
            message_id
        ))

        row = cursor.fetchone()

        if not row:
            continue

        (
            user_id,
            username,
            first_name,
            last_name,
            text,
            date,
            media_type,
            file_id
        ) = row

        cursor.execute("""
            UPDATE messages

            SET deleted = 1

            WHERE connection_id = ?
              AND chat_id = ?
              AND message_id = ?
        """, (
            event.business_connection_id,
            event.chat.id,
            message_id
        ))

        db.commit()

        name, username_text = get_user_name(
            first_name,
            last_name,
            username
        )

        header = (
            "🗑 <b>Удалено сообщение</b>\n\n"

            f"👤 <b>{safe(name)}</b>\n"
            f"🔹 {safe(username_text)}\n"
            f"🆔 <code>{user_id}</code>\n"
            f"🕐 {safe(moscow_time(datetime.fromisoformat(date)))}\n\n"
        )

        if (
            media_type == "text"
            or not file_id
        ):

            try:

                await bot.send_message(
                    chat_id=user_chat_id,
                    text=(
                        header +

                        "💬 <b>Сообщение:</b>\n"

                        f"<blockquote>"
                        f"{safe(text or '—')}"
                        f"</blockquote>"
                    ),

                    parse_mode="HTML"
                )

            except Exception as error:

                print(
                    "❌ DELETE NOTIFY ERROR:",
                    repr(error)
                )

            continue

        caption = (
            header +

            "💬 <b>Текст:</b>\n"

            f"{safe(text or '—')}"
        )

        try:

            if media_type == "photo":

                await bot.send_photo(
                    user_chat_id,
                    file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "video":

                await bot.send_video(
                    user_chat_id,
                    file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "voice":

                await bot.send_voice(
                    user_chat_id,
                    file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "document":

                await bot.send_document(
                    user_chat_id,
                    file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "audio":

                await bot.send_audio(
                    user_chat_id,
                    file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "animation":

                await bot.send_animation(
                    user_chat_id,
                    file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "video_note":

                await bot.send_video_note(
                    user_chat_id,
                    file_id
                )

                if text:

                    await bot.send_message(
                        user_chat_id,
                        caption,
                        parse_mode="HTML"
                    )

            elif media_type == "sticker":

                await bot.send_sticker(
                    user_chat_id,
                    file_id
                )

                if text:

                    await bot.send_message(
                        user_chat_id,
                        caption,
                        parse_mode="HTML"
                    )

        except Exception as error:

            print(
                "❌ MEDIA DELETE ERROR:",
                repr(error)
            )


# =========================================================
# /START
# =========================================================

@dp.message(Command("start"))
@dp.message(Command("menu"))
async def menu_command(
    message: types.Message
):

    owner_id = message.from_user.id

    try:

        await message.delete()

    except Exception:
        pass

    await show_main_menu(
        owner_id=owner_id,
        chat_id=message.chat.id,
        first_name=(
            message.from_user.first_name
            or "друг"
        )
    )


# =========================================================
# CALLBACK: MAIN MENU
# =========================================================

@dp.callback_query(
    lambda c: c.data == "open_menu"
)
async def open_menu(
    callback: CallbackQuery
):

    await show_main_menu(
        owner_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        first_name=(
            callback.from_user.first_name
            or "друг"
        )
    )

    try:
        await callback.answer()
    except Exception:
        pass


# =========================================================
# CONNECT BUSINESS
# =========================================================

@dp.callback_query(
    lambda c: c.data == "connect_business"
)
async def connect_business(
    callback: CallbackQuery
):

    await replace_menu(
        callback,

        "🚀 <b>Подключить Business</b>\n\n"

        "Открой:\n"
        "<b>Настройки → Telegram Business → Чат-боты</b>\n\n"

        "Добавь этого бота и выдай необходимые "
        "разрешения.\n\n"

        "После подключения архивирование "
        "начнёт работать автоматически.",

        back_keyboard()
    )


# =========================================================
# MY BUSINESS
# =========================================================

@dp.callback_query(
    lambda c: c.data == "my_business"
)
async def my_business(
    callback: CallbackQuery
):

    owner_id = callback.from_user.id

    cursor.execute("""
        SELECT
            connection_id,
            enabled

        FROM business_connections

        WHERE owner_id = ?

        ORDER BY created_at DESC

        LIMIT 1
    """, (
        owner_id,
    ))

    row = cursor.fetchone()

    if row:

        connection_id, enabled = row

        status = (
            "🟢 Подключён"
            if enabled
            else "🔴 Отключён"
        )

        text = (
            "🟦 <b>Мой Business</b>\n\n"

            f"{status}\n\n"

            "✅ Архивирование\n"
            "🗑 Удаления\n"
            "✏️ Изменения\n"
            "📦 Медиа\n"
            "💾 .save\n"
            "🚀 Business-команды\n\n"

            f"<code>{safe(connection_id)}</code>"
        )

    else:

        text = (
            "🟦 <b>Мой Business</b>\n\n"

            "❌ Business пока не найден.\n\n"

            "Отправь новое сообщение "
            "в Business-чат."
        )

    await replace_menu(
        callback,

        text,

        InlineKeyboardMarkup(
            inline_keyboard=[

                [
                    InlineKeyboardButton(
                        text="🔄 Проверить",
                        callback_data="my_business"
                    )
                ],

                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="open_menu"
                    )
                ]

            ]
        )
    )


# =========================================================
# ADMIN LOGS
# =========================================================


def build_admin_logs_text(limit=20):

    cursor.execute("""
        SELECT
            id,
            message_id,
            chat_id,
            user_id,
            username,
            first_name,
            last_name,
            text,
            date,
            media_type
        FROM messages
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()

    if not rows:
        return (
            "📋 <b>Логи</b>\n\n"
            "Пока нет сохранённых сообщений."
        )

    result = [
        "📋 <b>Логи Business-сообщений</b>",
        "",
        f"Последние {len(rows)} сообщений:",
        ""
    ]

    for row in rows:
        (
            db_id,
            message_id,
            chat_id,
            user_id,
            username,
            first_name,
            last_name,
            msg_text,
            date,
            media_type
        ) = row

        name = " ".join(
            part for part in [first_name, last_name]
            if part
        ).strip() or "Неизвестный пользователь"

        username_text = (
            f"@{username}"
            if username
            else "нет username"
        )

        try:
            time_text = moscow_time(
                datetime.fromisoformat(date)
            )
        except Exception:
            time_text = str(date)

        icons = {
            "text": "💬",
            "photo": "🖼",
            "video": "🎬",
            "document": "📄",
            "audio": "🎵",
            "voice": "🎤",
            "animation": "🎞",
            "video_note": "⭕",
            "sticker": "🧩"
        }

        icon = icons.get(
            media_type,
            "📦"
        )

        clean_text = (
            msg_text or ""
        ).replace(
            "\n",
            " "
        ).strip()

        if len(clean_text) > 100:
            clean_text = clean_text[:100] + "…"

        if not clean_text:
            clean_text = (
                "Медиа"
                if media_type != "text"
                else "Без текста"
            )

        result.append(
            f"<b>#{db_id}</b> "
            f"👤 <b>{safe(name)}</b> "
            f"{safe(username_text)}\n"
            f"🆔 <code>{user_id or '—'}</code>\n"
            f"💬 Chat: <code>{chat_id}</code> | "
            f"📨 <code>{message_id}</code>\n"
            f"🕐 {safe(time_text)}\n"
            f"{icon} {safe(clean_text)}"
        )

        if media_type != "text":
            result.append(
                f"📎 Медиа: <b>#{db_id}</b>"
            )

        result.append("")

    return "\n".join(result)


def get_recent_admin_log_media(limit=20):

    cursor.execute("""
        SELECT id, media_type
        FROM messages
        WHERE media_type != "text"
          AND file_id IS NOT NULL
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    return cursor.fetchall()


def admin_logs_keyboard(log_ids=None):

    rows = [
        [
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data="open_admin_logs"
            )
        ]
    ]

    if log_ids:
        media_row = []

        for db_id, media_type in log_ids:

            media_row.append(
                InlineKeyboardButton(
                    text=f"📎 Открыть #{db_id}",
                    callback_data=f"open_log_media:{db_id}"
                )
            )

            if len(media_row) == 1:
                rows.append(media_row)
                media_row = []

        if media_row:
            rows.append(media_row)

    rows.extend([
        [
            InlineKeyboardButton(
                text="📥 Скачать все логи",
                callback_data="download_admin_logs"
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data="open_menu"
            )
        ]
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )



@dp.callback_query(
    lambda c: c.data.startswith("open_log_media:")
)
async def open_log_media(
    callback: CallbackQuery
):

    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "⛔ Доступ только для администратора.",
            show_alert=True
        )
        return

    try:
        db_id = int(
            callback.data.split(":", 1)[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "❌ Некорректный лог.",
            show_alert=True
        )
        return

    cursor.execute("""
        SELECT
            user_id,
            username,
            first_name,
            last_name,
            chat_id,
            message_id,
            text,
            media_type,
            file_id,
            date
        FROM messages
        WHERE id = ?
        LIMIT 1
    """, (db_id,))

    row = cursor.fetchone()

    if not row:
        await callback.answer(
            "❌ Лог не найден.",
            show_alert=True
        )
        return

    (
        user_id,
        username,
        first_name,
        last_name,
        chat_id,
        message_id,
        msg_text,
        media_type,
        file_id,
        date
    ) = row

    if not file_id or media_type == "text":
        await callback.answer(
            "ℹ️ В этом логе нет сохранённого медиа.",
            show_alert=True
        )
        return

    name = " ".join(
        part for part in [first_name, last_name]
        if part
    ).strip() or "Неизвестный пользователь"

    caption = (
        "📋 <b>Медиа из логов</b>\n\n"
        f"👤 <b>{safe(name)}</b>\n"
        f"🆔 <code>{user_id or '—'}</code>\n"
        f"💬 Chat: <code>{chat_id}</code>\n"
        f"📨 Message: <code>{message_id}</code>\n"
        f"🕐 <code>{safe(date)}</code>\n"
        f"📦 Тип: <code>{safe(media_type)}</code>"
    )

    if msg_text:
        caption += (
            "\n\n💬 <b>Текст:</b>\n"
            f"<blockquote>{safe(msg_text)}</blockquote>"
        )

    try:
        if media_type == "photo":
            await bot.send_photo(
                ADMIN_ID,
                file_id,
                caption=caption,
                parse_mode="HTML"
            )

        elif media_type == "video":
            await bot.send_video(
                ADMIN_ID,
                file_id,
                caption=caption,
                parse_mode="HTML"
            )

        elif media_type == "document":
            await bot.send_document(
                ADMIN_ID,
                file_id,
                caption=caption,
                parse_mode="HTML"
            )

        elif media_type == "audio":
            await bot.send_audio(
                ADMIN_ID,
                file_id,
                caption=caption,
                parse_mode="HTML"
            )

        elif media_type == "voice":
            await bot.send_voice(
                ADMIN_ID,
                file_id,
                caption=caption,
                parse_mode="HTML"
            )

        elif media_type == "animation":
            await bot.send_animation(
                ADMIN_ID,
                file_id,
                caption=caption,
                parse_mode="HTML"
            )

        elif media_type == "video_note":
            await bot.send_video_note(
                ADMIN_ID,
                file_id
            )
            await bot.send_message(
                ADMIN_ID,
                caption,
                parse_mode="HTML"
            )

        elif media_type == "sticker":
            await bot.send_sticker(
                ADMIN_ID,
                file_id
            )
            await bot.send_message(
                ADMIN_ID,
                caption,
                parse_mode="HTML"
            )

        else:
            await callback.answer(
                "ℹ️ Этот тип медиа пока не поддерживается.",
                show_alert=True
            )
            return

        await callback.answer(
            "✅ Медиа отправлено."
        )

    except Exception as error:
        print(
            "❌ ADMIN MEDIA ERROR:",
            repr(error)
        )
        await callback.answer(
            "❌ Не удалось отправить медиа.",
            show_alert=True
        )



@dp.callback_query(
    lambda c: c.data == "open_admin_logs"
)
async def open_admin_logs(
    callback: CallbackQuery
):

    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "⛔ Доступ только для администратора.",
            show_alert=True
        )
        return

    text = build_admin_logs_text(20)
    media_ids = get_recent_admin_log_media(20)

    await replace_menu(
        callback,
        text,
        admin_logs_keyboard(media_ids)
    )


# =========================================================
# DOWNLOAD ALL ADMIN LOGS
# =========================================================

@dp.callback_query(
    lambda c: c.data == "download_admin_logs"
)
async def download_admin_logs(
    callback: CallbackQuery
):

    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "⛔ Доступ только для администратора.",
            show_alert=True
        )
        return

    try:
        # Обязательно закрываем "часики" у callback сразу.
        await callback.answer(
            "⏳ Формирую файл логов..."
        )

        cursor.execute("""
            SELECT
                id,
                connection_id,
                chat_id,
                message_id,
                user_id,
                username,
                first_name,
                last_name,
                text,
                date,
                media_type,
                file_id,
                deleted
            FROM messages
            ORDER BY id ASC
        """)

        rows = cursor.fetchall()

        output = StringIO()

        # Excel-friendly UTF-8 CSV.
        output.write(
            "ID;Connection ID;Chat ID;Message ID;"
            "User ID;Username;First Name;Last Name;"
            "Text;Date;Media Type;File ID;Deleted\n"
        )

        for row in rows:
            values = []

            for value in row:
                value = "" if value is None else str(value)
                value = value.replace('"', '""')
                values.append(f'"{value}"')

            output.write(
                ";".join(values) + "\n"
            )

        data = (
            "\ufeff" + output.getvalue()
        ).encode("utf-8")

        filename = (
            "business_logs_"
            + datetime.now().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )
            + ".csv"
        )

        await bot.send_document(
            chat_id=ADMIN_ID,
            document=BufferedInputFile(
                data,
                filename=filename
            ),
            caption=(
                "📋 <b>Полный экспорт логов</b>\n\n"
                f"📨 Сообщений: <b>{len(rows)}</b>\n"
                f"📄 <code>{safe(filename)}</code>"
            ),
            parse_mode="HTML"
        )

        print(
            f"📥 LOG EXPORT OK: {len(rows)} rows -> {filename}"
        )

    except Exception as error:

        print(
            "❌ LOG EXPORT ERROR:",
            repr(error)
        )

        try:
            await callback.message.answer(
                "❌ <b>Не удалось скачать логи.</b>\n\n"
                f"<code>{safe(error)}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass


# =========================================================
# COMMANDS PAGE
# =========================================================


@dp.callback_query(
    lambda c: c.data == "open_commands"
)
async def open_commands(
    callback: CallbackQuery
):

    await replace_menu(
        callback,
        "💬 <b>Команды</b>\n\nВыберите команду:",
        commands_keyboard()
    )


@dp.callback_query(
    lambda c: c.data == "command_spam"
)
async def command_spam_help(
    callback: CallbackQuery
):

    text = (
        "💬 <b>.spam</b>\n\n"
        "<code>.spam N текст</code>\n\n"
        "Отправляет N копий текста.\n\n"
        "Пример:\n"
        "<code>.spam 5 Привет</code>\n\n"
        "Бот отправит 5 сообщений."
    )

    await replace_menu(
        callback,
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К командам",
                        callback_data="open_commands"
                    )
                ]
            ]
        )
    )


@dp.callback_query(
    lambda c: c.data == "command_bspam"
)
async def command_bspam_help(
    callback: CallbackQuery
):

    text = (
        "🗑 <b>.bspam</b>\n\n"
        "<code>.bspam</code>\n\n"
        "Удаляет сообщения, отправленные через "
        "<code>.spam</code>."
    )

    await replace_menu(
        callback,
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К командам",
                        callback_data="open_commands"
                    )
                ]
            ]
        )
    )


@dp.callback_query(
    lambda c: c.data == "command_save"
)
async def command_save_help(
    callback: CallbackQuery
):

    text = (
        "💾 <b>.save</b>\n\n"
        "<code>.save</code>\n\n"
        "Ответь этой командой на сообщение, "
        "которое нужно сохранить."
    )

    await replace_menu(
        callback,
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К командам",
                        callback_data="open_commands"
                    )
                ]
            ]
        )
    )


@dp.callback_query(
    lambda c: c.data == "command_copy"
)
async def command_copy_help(
    callback: CallbackQuery
):

    text = (
        "👤 <b>.copy</b>\n\n"
        "<code>.copy</code>\n\n"
        "Ответь этой командой на сообщение "
        "собеседника.\n\n"
        "Бот попробует скопировать имя, био "
        "и фото профиля.\n\n"
        "Для того чтобы вернуть исходный профиль, .back \n\n"
    )

    await replace_menu(
        callback,
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К командам",
                        callback_data="open_commands"
                    )
                ]
            ]
        )
    )


@dp.callback_query(
    lambda c: c.data == "command_back"
)
async def command_back_help(
    callback: CallbackQuery
):

    text = (
        "↩️ <b>.back</b>\n\n"
        "<code>.back</code>\n\n"
        "Возвращает профиль к состоянию, "
        "которое было сохранено перед последним .copy."
    )

    await replace_menu(
        callback,
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К командам",
                        callback_data="open_commands"
                    )
                ]
            ]
        )
    )

# =========================================================
# EDITED PAGE
# =========================================================

@dp.callback_query(
    lambda c: c.data == "open_edited"
)
async def open_edited(
    callback: CallbackQuery
):

    text = (
        "ℹ️ <b>Изменённые сообщения</b>\n\n"

        "При изменении сообщения бот "
        "показывает две версии:\n\n"

        "❌ <b>Было:</b>\n"
        "старая версия\n\n"

        "✅ <b>Стало:</b>\n"
        "новая версия."
    )

    await replace_menu(
        callback,
        text,
        back_keyboard()
    )


# =========================================================
# SETTINGS PAGE
# =========================================================

@dp.callback_query(
    lambda c: c.data == "open_settings"
)
async def open_settings(
    callback: CallbackQuery
):

    owner_id = callback.from_user.id

    (
        delete_notifications,
        save_media,
        edit_notifications
    ) = get_settings(
        owner_id
    )

    text = (
        "⚙️ <b>Настройки</b>\n\n"

        f"🗑 Удаления: "
        f"{'✅' if delete_notifications else '❌'}\n"

        f"📦 Медиа: "
        f"{'✅' if save_media else '❌'}\n"

        f"✏️ Изменения: "
        f"{'✅' if edit_notifications else '❌'}"
    )

    await replace_menu(
        callback,

        text,

        settings_keyboard(
            delete_notifications,
            save_media,
            edit_notifications
        )
    )


# =========================================================
# TOGGLE DELETIONS
# =========================================================

@dp.callback_query(
    lambda c: c.data == "toggle_deletions"
)
async def toggle_deletions(
    callback: CallbackQuery
):

    owner_id = callback.from_user.id

    (
        delete_notifications,
        save_media,
        edit_notifications
    ) = get_settings(
        owner_id
    )

    new_value = (
        not delete_notifications
    )

    cursor.execute("""
        UPDATE settings

        SET delete_notifications = ?

        WHERE owner_id = ?
    """, (
        int(new_value),
        owner_id
    ))

    db.commit()

    text = (
        "⚙️ <b>Настройки</b>\n\n"

        f"🗑 Удаления: "
        f"{'✅' if new_value else '❌'}\n"

        f"📦 Медиа: "
        f"{'✅' if save_media else '❌'}\n"

        f"✏️ Изменения: "
        f"{'✅' if edit_notifications else '❌'}"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=settings_keyboard(
            new_value,
            save_media,
            edit_notifications
        )
    )

    await callback.answer()


# =========================================================
# TOGGLE MEDIA
# =========================================================

@dp.callback_query(
    lambda c: c.data == "toggle_media"
)
async def toggle_media(
    callback: CallbackQuery
):

    owner_id = callback.from_user.id

    (
        delete_notifications,
        save_media,
        edit_notifications
    ) = get_settings(
        owner_id
    )

    new_value = (
        not save_media
    )

    cursor.execute("""
        UPDATE settings

        SET save_media = ?

        WHERE owner_id = ?
    """, (
        int(new_value),
        owner_id
    ))

    db.commit()

    text = (
        "⚙️ <b>Настройки</b>\n\n"

        f"🗑 Удаления: "
        f"{'✅' if delete_notifications else '❌'}\n"

        f"📦 Медиа: "
        f"{'✅' if new_value else '❌'}\n"

        f"✏️ Изменения: "
        f"{'✅' if edit_notifications else '❌'}"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=settings_keyboard(
            delete_notifications,
            new_value,
            edit_notifications
        )
    )

    await callback.answer()


# =========================================================
# TOGGLE EDITS
# =========================================================

@dp.callback_query(
    lambda c: c.data == "toggle_edits"
)
async def toggle_edits(
    callback: CallbackQuery
):

    owner_id = callback.from_user.id

    (
        delete_notifications,
        save_media,
        edit_notifications
    ) = get_settings(
        owner_id
    )

    new_value = (
        not edit_notifications
    )

    cursor.execute("""
        UPDATE settings

        SET edit_notifications = ?

        WHERE owner_id = ?
    """, (
        int(new_value),
        owner_id
    ))

    db.commit()

    text = (
        "⚙️ <b>Настройки</b>\n\n"

        f"🗑 Удаления: "
        f"{'✅' if delete_notifications else '❌'}\n"

        f"📦 Медиа: "
        f"{'✅' if save_media else '❌'}\n"

        f"✏️ Изменения: "
        f"{'✅' if new_value else '❌'}"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=settings_keyboard(
            delete_notifications,
            save_media,
            new_value
        )
    )

    await callback.answer()


# =========================================================
# STATS
# =========================================================

@dp.callback_query(
    lambda c: c.data == "open_stats"
)
async def open_stats(
    callback: CallbackQuery
):

    owner_id = callback.from_user.id

    cursor.execute("""
        SELECT COUNT(*)

        FROM messages m

        INNER JOIN business_connections b
        ON m.connection_id = b.connection_id

        WHERE b.owner_id = ?
    """, (
        owner_id,
    ))

    total = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)

        FROM messages m

        INNER JOIN business_connections b
        ON m.connection_id = b.connection_id

        WHERE b.owner_id = ?
          AND m.deleted = 1
    """, (
        owner_id,
    ))

    deleted = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)

        FROM messages m

        INNER JOIN business_connections b
        ON m.connection_id = b.connection_id

        WHERE b.owner_id = ?
          AND m.media_type != 'text'
    """, (
        owner_id,
    ))

    media = cursor.fetchone()[0]

    text = (
        "📊 <b>Статистика</b>\n\n"

        f"💬 Сообщений: <b>{total}</b>\n"
        f"🗑 Удалённых: <b>{deleted}</b>\n"
        f"📦 Медиа: <b>{media}</b>"
    )

    await replace_menu(
        callback,
        text,
        back_keyboard()
    )


# =========================================================
# DELETED
# =========================================================

@dp.callback_query(
    lambda c: c.data == "open_deleted"
)
async def open_deleted(
    callback: CallbackQuery
):

    owner_id = callback.from_user.id

    cursor.execute("""
        SELECT
            m.first_name,
            m.last_name,
            m.username,
            m.text,
            m.date,
            m.media_type

        FROM messages m

        INNER JOIN business_connections b
        ON m.connection_id = b.connection_id

        WHERE b.owner_id = ?
          AND m.deleted = 1

        ORDER BY m.id DESC

        LIMIT 10
    """, (
        owner_id,
    ))

    rows = cursor.fetchall()

    if not rows:

        text = (
            "🗑 <b>Последние удаления</b>\n\n"
            "Удалённых сообщений пока нет."
        )

    else:

        text = (
            "🗑 <b>Последние удаления</b>\n\n"
        )

        for row in rows:

            (
                first_name,
                last_name,
                username,
                msg_text,
                date,
                media_type
            ) = row

            name, username_text = get_user_name(
                first_name,
                last_name,
                username
            )

            text += (
                f"👤 <b>{safe(name)}</b>\n"
                f"🔹 {safe(username_text)}\n"
                f"📦 {safe(media_name(media_type))}\n"
                f"💬 {safe(msg_text or '—')}\n"
                f"🕐 {safe(date)}\n\n"
            )

    await replace_menu(
        callback,
        text,
        back_keyboard()
    )


# =========================================================
# INVITE
# =========================================================

@dp.callback_query(
    lambda c: c.data == "invite_friend"
)
async def invite_friend(
    callback: CallbackQuery
):

    try:

        me = await bot.get_me()

        link = (
            f"https://t.me/{me.username}"
            f"?start=ref_{callback.from_user.id}"
        )

        await replace_menu(
            callback,

            "👥 <b>Пригласить друга</b>\n\n"

            "Отправь другу эту ссылку:\n\n"

            f"<code>{safe(link)}</code>",

            back_keyboard()
        )

    except Exception as error:

        print(
            "❌ INVITE ERROR:",
            repr(error)
        )


# =========================================================
# PRIVATE /SETTINGS
# =========================================================

@dp.message(Command("settings"))
async def private_settings_command(
    message: types.Message
):

    try:
        await message.delete()
    except Exception:
        pass

    owner_id = message.from_user.id

    (
        delete_notifications,
        save_media,
        edit_notifications
    ) = get_settings(
        owner_id
    )

    state = get_menu_state(
        owner_id
    )

    text = (
        "⚙️ <b>Настройки</b>\n\n"

        f"🗑 Удаления: "
        f"{'✅' if delete_notifications else '❌'}\n"

        f"📦 Медиа: "
        f"{'✅' if save_media else '❌'}\n"

        f"✏️ Изменения: "
        f"{'✅' if edit_notifications else '❌'}"
    )

    keyboard = settings_keyboard(
        delete_notifications,
        save_media,
        edit_notifications
    )

    if state:

        old_chat_id, old_message_id = state

        try:

            await bot.delete_message(
                chat_id=old_chat_id,
                message_id=old_message_id
            )

        except Exception:
            pass

    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard
    )

    save_menu_state(
        owner_id,
        message.chat.id,
        sent.message_id
    )


# =========================================================
# PRIVATE /STATS
# =========================================================

@dp.message(Command("stats"))
async def private_stats_command(
    message: types.Message
):

    try:
        await message.delete()
    except Exception:
        pass

    owner_id = message.from_user.id

    cursor.execute("""
        SELECT COUNT(*)

        FROM messages m

        INNER JOIN business_connections b
        ON m.connection_id = b.connection_id

        WHERE b.owner_id = ?
    """, (
        owner_id,
    ))

    total = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)

        FROM messages m

        INNER JOIN business_connections b
        ON m.connection_id = b.connection_id

        WHERE b.owner_id = ?

          AND m.deleted = 1
    """, (
        owner_id,
    ))

    deleted = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)

        FROM messages m

        INNER JOIN business_connections b
        ON m.connection_id = b.connection_id

        WHERE b.owner_id = ?

          AND m.media_type != 'text'
    """, (
        owner_id,
    ))

    media = cursor.fetchone()[0]

    state = get_menu_state(
        owner_id
    )

    if state:

        old_chat_id, old_message_id = state

        try:

            await bot.delete_message(
                chat_id=old_chat_id,
                message_id=old_message_id
            )

        except Exception:
            pass

    sent = await bot.send_message(
        chat_id=message.chat.id,

        text=(
            "📊 <b>Статистика</b>\n\n"

            f"💬 Сообщений: <b>{total}</b>\n"
            f"🗑 Удалённых: <b>{deleted}</b>\n"
            f"📦 Медиа: <b>{media}</b>"
        ),

        parse_mode="HTML",

        reply_markup=back_keyboard()
    )

    save_menu_state(
        owner_id,
        message.chat.id,
        sent.message_id
    )


# =========================================================
# PRIVATE /DELETED
# =========================================================

@dp.message(Command("deleted"))
async def private_deleted_command(
    message: types.Message
):

    try:
        await message.delete()
    except Exception:
        pass

    owner_id = message.from_user.id

    cursor.execute("""
        SELECT
            m.first_name,
            m.last_name,
            m.username,
            m.text,
            m.date,
            m.media_type

        FROM messages m

        INNER JOIN business_connections b
        ON m.connection_id = b.connection_id

        WHERE b.owner_id = ?
          AND m.deleted = 1

        ORDER BY m.id DESC

        LIMIT 10
    """, (
        owner_id,
    ))

    rows = cursor.fetchall()

    if not rows:

        text = (
            "🗑 <b>Последние удаления</b>\n\n"
            "Удалённых сообщений пока нет."
        )

    else:

        text = (
            "🗑 <b>Последние удаления</b>\n\n"
        )

        for row in rows:

            (
                first_name,
                last_name,
                username,
                msg_text,
                date,
                media_type
            ) = row

            name, username_text = get_user_name(
                first_name,
                last_name,
                username
            )

            text += (
                f"👤 <b>{safe(name)}</b>\n"
                f"🔹 {safe(username_text)}\n"
                f"📦 {safe(media_name(media_type))}\n"
                f"💬 {safe(msg_text or '—')}\n"
                f"🕐 {safe(date)}\n\n"
            )

    state = get_menu_state(
        owner_id
    )

    if state:

        old_chat_id, old_message_id = state

        try:

            await bot.delete_message(
                chat_id=old_chat_id,
                message_id=old_message_id
            )

        except Exception:
            pass

    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )

    save_menu_state(
        owner_id,
        message.chat.id,
        sent.message_id
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    if not TOKEN:
        print(
            "❌ BOT_TOKEN не задан."
        )
        return

    print()
    print("=" * 60)
    print("🚀 BUSINESS ARCHIVE ЗАПУЩЕН")
    print("=" * 60)
    print()

    actual_ffmpeg = (
        FFMPEG_PATH
        if os.path.isfile(FFMPEG_PATH)
        else shutil.which(FFMPEG_PATH)
    )

    print(
        "FFmpeg:",
        actual_ffmpeg or "НЕ НАЙДЕН"
    )

    print(
        "ADMIN_ID:",
        ADMIN_ID
    )

    if actual_ffmpeg:
        print("✅ FFmpeg найден")
    else:
        print("❌ FFmpeg НЕ найден!")

    print()
    await dp.start_polling(bot)


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
