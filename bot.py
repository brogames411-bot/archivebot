import asyncio
import base64
import difflib
import html
import logging
import math
import os
import random
import sqlite3
from io import BytesIO, StringIO
import uuid
import shutil
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from logging.handlers import RotatingFileHandler
import qrcode
from PIL import Image, ImageDraw
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
    LabeledPrice,
)
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError



# =========================================================
# CONFIG
# =========================================================


TOKEN = os.getenv("BOT_TOKEN", "").strip()
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg").strip()

SUPPORT_URL = "https://t.me/armgg077"



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


# =========================================================
# LOGGING
# =========================================================

def configure_logging():
    requested_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, requested_level, None)
    if not isinstance(level, int):
        level = logging.INFO

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        level=level,
        handlers=[console_handler],
        force=True
    )

    app_logger = logging.getLogger("archivebot")
    log_file = os.getenv(
        "LOG_FILE",
        os.path.join(DATA_DIR, "archivebot.log")
    ).strip()

    if log_file:
        try:
            max_bytes = max(1024, int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024))))
            backup_count = max(1, int(os.getenv("LOG_BACKUP_COUNT", "3")))
            log_directory = os.path.dirname(os.path.abspath(log_file))
            os.makedirs(log_directory, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            logging.getLogger().addHandler(file_handler)
        except (OSError, ValueError) as error:
            app_logger.warning("Не удалось включить лог-файл %s: %s", log_file, error)

    logging.getLogger("aiogram").setLevel(logging.WARNING)

    if not isinstance(getattr(logging, requested_level, None), int):
        app_logger.warning(
            "Неизвестный LOG_LEVEL=%r; используется INFO",
            requested_level
        )

    app_logger.info(
        "Логирование готово: уровень=%s, файл=%s",
        logging.getLevelName(level),
        log_file or "отключён"
    )
    return app_logger


logger = configure_logging()

SUBSCRIPTION_DAYS = 30
try:
    SUBSCRIPTION_PRICE_STARS = int(os.getenv("SUBSCRIPTION_PRICE_STARS", "100"))
except ValueError:
    SUBSCRIPTION_PRICE_STARS = 100
    logger.warning("Invalid SUBSCRIPTION_PRICE_STARS; using 100 Stars")
if not 1 <= SUBSCRIPTION_PRICE_STARS <= 10000:
    logger.warning("SUBSCRIPTION_PRICE_STARS must be 1..10000; using 100 Stars")
    SUBSCRIPTION_PRICE_STARS = 100

FREE_USER_IDS = set()
for _free_user_id in os.getenv("FREE_USER_IDS", "").split(","):
    _free_user_id = _free_user_id.strip()
    if not _free_user_id:
        continue
    try:
        parsed_free_user_id = int(_free_user_id)
        if parsed_free_user_id > 0:
            FREE_USER_IDS.add(parsed_free_user_id)
        else:
            logger.warning("Ignoring invalid FREE_USER_IDS entry: %r", _free_user_id)
    except ValueError:
        logger.warning("Ignoring invalid FREE_USER_IDS entry: %r", _free_user_id)

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

    last_chat_id INTEGER,
    owner_username TEXT,
    owner_first_name TEXT,
    owner_last_name TEXT
)
""")

try:
    cursor.execute("""
        ALTER TABLE business_connections
        ADD COLUMN last_chat_id INTEGER
    """)
except sqlite3.OperationalError:
    pass

for column in ("owner_username", "owner_first_name", "owner_last_name"):
    try:
        cursor.execute(
            f"ALTER TABLE business_connections ADD COLUMN {column} TEXT"
        )
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

    chat_username TEXT,
    chat_first_name TEXT,
    chat_last_name TEXT,

    text TEXT,
    date TEXT,

    media_type TEXT DEFAULT 'text',
    file_id TEXT,

    deleted INTEGER NOT NULL DEFAULT 0,

    UNIQUE(connection_id, chat_id, message_id)
)
""")

for column in ("chat_username", "chat_first_name", "chat_last_name"):
    try:
        cursor.execute(
            f"ALTER TABLE messages ADD COLUMN {column} TEXT"
        )
    except sqlite3.OperationalError:
        pass


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
# GROUP CHATS
# =========================================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS group_chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    username TEXT,
    added_by_user_id INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    last_seen_at TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS muted_business_chats (
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    muted_at TEXT NOT NULL,
    PRIMARY KEY(connection_id, chat_id)
)
""")

muted_delete_permission_warnings = set()

cursor.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    expires_at TEXT NOT NULL,
    last_charge_id TEXT,
    updated_at TEXT NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS free_users (
    user_id INTEGER PRIMARY KEY,
    granted_by INTEGER NOT NULL,
    granted_at TEXT NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS payment_orders (
    payload TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    charge_id TEXT,
    created_at TEXT NOT NULL,
    paid_at TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS archive_delivery_claims_v2 (
    connection_id TEXT NOT NULL,
    business_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    trigger_type TEXT NOT NULL,
    trigger_message_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(
        connection_id, business_chat_id, source_message_id,
        recipient_id, trigger_type, trigger_message_id
    )
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS subscription_reminders (
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    sent_at TEXT,
    PRIMARY KEY(user_id, expires_at)
)
""")

try:
    cursor.execute("""
        INSERT OR IGNORE INTO archive_delivery_claims_v2 (
            connection_id, business_chat_id, source_message_id, recipient_id,
            trigger_type, trigger_message_id, status, updated_at
        )
        SELECT
            legacy.connection_id,
            legacy.business_chat_id,
            legacy.source_message_id,
            CASE
                WHEN legacy.recipient_id = connections.owner_id
                THEN connections.user_chat_id
                ELSE legacy.recipient_id
            END,
            'legacy',
            legacy.source_message_id,
            legacy.status,
            legacy.updated_at
        FROM archive_delivery_claims AS legacy
        LEFT JOIN business_connections AS connections
          ON connections.connection_id = legacy.connection_id
    """)
    db.commit()
except sqlite3.OperationalError:
    # The legacy table exists only on installations that briefly used v1 dedupe.
    pass


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

def claim_archive_delivery(
    connection_id,
    business_chat_id,
    source_message_id,
    recipient_id,
    trigger_type,
    trigger_message_id
):
    """Reserve this trigger and suppress its paired delete/reply notification."""
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    key = (
        connection_id, business_chat_id, source_message_id, recipient_id,
        trigger_type, trigger_message_id
    )
    row = cursor.execute("""
        SELECT status, updated_at FROM archive_delivery_claims_v2
        WHERE connection_id = ?
          AND business_chat_id = ?
          AND source_message_id = ?
          AND recipient_id = ?
          AND trigger_type = ?
          AND trigger_message_id = ?
    """, key).fetchone()

    if row and row[0] == "sent":
        return False
    if row and row[0] == "sending":
        try:
            previous_update = datetime.fromisoformat(row[1])
            if previous_update.tzinfo is None:
                previous_update = previous_update.replace(tzinfo=timezone.utc)
            if now - previous_update < timedelta(minutes=10):
                return False
        except (TypeError, ValueError):
            return False

    cross_event = cursor.execute("""
        SELECT status, updated_at FROM archive_delivery_claims_v2
        WHERE connection_id = ?
          AND business_chat_id = ?
          AND source_message_id = ?
          AND recipient_id = ?
          AND trigger_type != ?
          AND status IN ('sending', 'sent')
        ORDER BY updated_at DESC
        LIMIT 1
    """, (connection_id, business_chat_id, source_message_id, recipient_id, trigger_type)).fetchone()
    if cross_event and cross_event[0] == "sent":
        return False
    if cross_event and cross_event[0] == "sending":
        try:
            cross_time = datetime.fromisoformat(cross_event[1])
            if cross_time.tzinfo is None:
                cross_time = cross_time.replace(tzinfo=timezone.utc)
            if now - cross_time < timedelta(minutes=10):
                return False
        except (TypeError, ValueError):
            return False

    if row:
        cursor.execute("""
            UPDATE archive_delivery_claims_v2
            SET status = 'sending', updated_at = ?
            WHERE connection_id = ?
              AND business_chat_id = ?
              AND source_message_id = ?
              AND recipient_id = ?
              AND trigger_type = ?
              AND trigger_message_id = ?
        """, (now_text, *key))
    else:
        cursor.execute("""
            INSERT INTO archive_delivery_claims_v2 (
                connection_id, business_chat_id, source_message_id,
                recipient_id, trigger_type, trigger_message_id, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'sending', ?)
        """, (*key, now_text))

    db.commit()
    return True


async def wait_for_archive_delivery(
    connection_id,
    business_chat_id,
    source_message_id,
    recipient_id,
    trigger_type,
    trigger_message_id
):
    """Wait for this or its paired event's in-flight send to finish."""
    for _ in range(300):
        row = cursor.execute("""
            SELECT status FROM archive_delivery_claims_v2
            WHERE connection_id = ?
              AND business_chat_id = ?
              AND source_message_id = ?
              AND recipient_id = ?
              AND trigger_type = ?
              AND trigger_message_id = ?
        """, (
            connection_id, business_chat_id, source_message_id, recipient_id,
            trigger_type, trigger_message_id
        )).fetchone()
        if row and row[0] == "sent":
            return True
        if row and row[0] == "failed":
            return False

        cross_event = cursor.execute("""
            SELECT status, updated_at FROM archive_delivery_claims_v2
            WHERE connection_id = ?
              AND business_chat_id = ?
              AND source_message_id = ?
              AND recipient_id = ?
              AND trigger_type != ?
              AND status IN ('sending', 'sent')
            ORDER BY updated_at DESC
            LIMIT 1
        """, (connection_id, business_chat_id, source_message_id, recipient_id, trigger_type)).fetchone()
        if cross_event:
            if cross_event[0] == "sent":
                return True
        elif not row:
            return False

        await asyncio.sleep(0.1)
    return None


def finish_archive_delivery(
    connection_id,
    business_chat_id,
    source_message_id,
    recipient_id,
    trigger_type,
    trigger_message_id,
    sent
):
    cursor.execute("""
        UPDATE archive_delivery_claims_v2
        SET status = ?, updated_at = ?
        WHERE connection_id = ?
          AND business_chat_id = ?
          AND source_message_id = ?
          AND recipient_id = ?
          AND trigger_type = ?
          AND trigger_message_id = ?
    """, (
        "sent" if sent else "failed",
        datetime.now(timezone.utc).isoformat(),
        connection_id,
        business_chat_id,
        source_message_id,
        recipient_id,
        trigger_type,
        trigger_message_id
    ))
    db.commit()

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
game_invites = {}
active_games = {}
subscription_notice_sent = set()


converter_users = set()
audio_converter_users = set()
qr_users = set()
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


def get_subscription_expiration(user_id):
    if not user_id:
        return None
    row = cursor.execute(
        "SELECT expires_at FROM subscriptions WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    if not row:
        return None
    try:
        expiration = datetime.fromisoformat(row[0])
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
        return expiration
    except (TypeError, ValueError):
        logger.exception("Invalid subscription expiry stored: user_id=%s", user_id)
        return None


def is_subscription_active(user_id):
    if not user_id:
        return False
    if is_free_user(user_id):
        return True
    expiration = get_subscription_expiration(user_id)
    return bool(expiration and expiration > datetime.now(timezone.utc))


def is_free_user(user_id):
    if not user_id:
        return False
    if (ADMIN_ID and user_id == ADMIN_ID) or user_id in FREE_USER_IDS:
        return True
    row = cursor.execute(
        "SELECT 1 FROM free_users WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    return bool(row)


def group_subscription_owner(chat_id):
    row = cursor.execute(
        "SELECT added_by_user_id FROM group_chats WHERE chat_id = ? AND enabled = 1",
        (chat_id,)
    ).fetchone()
    return row[0] if row else None


def subscription_status_text(user_id):
    if is_free_user(user_id):
        return "✅ Для этого аккаунта включён бесплатный доступ."
    expiration = get_subscription_expiration(user_id)
    if expiration and expiration > datetime.now(timezone.utc):
        return (
            "✅ Подписка активна до "
            f"<b>{safe(expiration.strftime('%d.%m.%Y %H:%M UTC'))}</b>."
        )
    return "⛔ Активной подписки нет. Архивирование приостановлено."


def subscription_page_text(user_id):
    if is_free_user(user_id):
        return (
            "⭐ <b>Подписка ArchiveBot</b>\n\n"
            "✅ Для этого аккаунта включён бесплатный доступ.\n"
            "Архивирование Business-переписки и подключённых групп активно.\n"
            "Поддержка: /paysupport · Условия: /terms."
        )
    return (
        "⭐ <b>Подписка ArchiveBot</b>\n\n"
        f"{subscription_status_text(user_id)}\n\n"
        f"Доступ на 30 дней — <b>{SUBSCRIPTION_PRICE_STARS} ⭐</b>.\n"
        "Продление вручную, автоматических списаний нет.\n"
        "Архивирование Business-переписки и подключённых групп включено при активном доступе.\n"
        "Поддержка платежей: /paysupport · Условия: /terms."
    )


def subscription_keyboard(user_id):
    rows = []
    if not is_free_user(user_id):
        label = "🔄 Продлить на 30 дней" if is_subscription_active(user_id) else "⭐ Купить доступ на 30 дней"
        rows.append([InlineKeyboardButton(text=label, callback_data="buy_subscription")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="open_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def russian_days_word(days):
    last_two = days % 100
    last = days % 10
    if 11 <= last_two <= 14:
        return "дней"
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дня"
    return "дней"


async def subscription_expiry_reminder_loop():
    """Sends one persistent reminder when a paid subscription has <= 3 days left."""
    while True:
        now = datetime.now(timezone.utc)
        subscriptions = cursor.execute(
            "SELECT user_id, expires_at FROM subscriptions"
        ).fetchall()

        for user_id, expires_at_text in subscriptions:
            if is_free_user(user_id):
                continue
            try:
                expires_at = datetime.fromisoformat(expires_at_text)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                logger.exception("Invalid subscription expiry stored: user_id=%s", user_id)
                continue

            remaining_seconds = (expires_at - now).total_seconds()
            if remaining_seconds <= 0 or remaining_seconds > timedelta(days=3).total_seconds():
                continue

            reminder = cursor.execute("""
                SELECT status, attempted_at FROM subscription_reminders
                WHERE user_id = ? AND expires_at = ?
            """, (user_id, expires_at_text)).fetchone()
            if reminder and reminder[0] == "sent":
                continue
            if reminder and reminder[0] == "sending":
                try:
                    attempted_at = datetime.fromisoformat(reminder[1])
                    if attempted_at.tzinfo is None:
                        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
                    if now - attempted_at < timedelta(minutes=10):
                        continue
                except (TypeError, ValueError):
                    pass

            attempted_at = now.isoformat()
            cursor.execute("""
                INSERT INTO subscription_reminders (
                    user_id, expires_at, status, attempted_at, sent_at
                ) VALUES (?, ?, 'sending', ?, NULL)
                ON CONFLICT(user_id, expires_at) DO UPDATE SET
                    status = 'sending',
                    attempted_at = excluded.attempted_at,
                    sent_at = NULL
            """, (user_id, expires_at_text, attempted_at))
            db.commit()

            days_left = max(1, int((remaining_seconds + 86399) // 86400))
            reminder_text = (
                "⏰ <b>Напоминание о подписке</b>\n\n"
                f"Подписка истечёт примерно через <b>{days_left} "
                f"{russian_days_word(days_left)}</b> — "
                f"{safe(expires_at.strftime('%d.%m.%Y %H:%M UTC'))}.\n"
                "Продли доступ кнопкой ниже или командой /subscription."
            )

            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=reminder_text,
                    parse_mode="HTML",
                    reply_markup=subscription_keyboard(user_id)
                )
            except Exception:
                cursor.execute("""
                    UPDATE subscription_reminders
                    SET status = 'failed'
                    WHERE user_id = ? AND expires_at = ?
                """, (user_id, expires_at_text))
                db.commit()
                logger.exception("Could not send subscription expiry reminder: user_id=%s", user_id)
            else:
                cursor.execute("""
                    UPDATE subscription_reminders
                    SET status = 'sent', sent_at = ?
                    WHERE user_id = ? AND expires_at = ?
                """, (datetime.now(timezone.utc).isoformat(), user_id, expires_at_text))
                db.commit()
                logger.info(
                    "Subscription expiry reminder sent: user_id=%s expires_at=%s",
                    user_id,
                    expires_at_text
                )

        await asyncio.sleep(60 * 60)


async def subscription_expiry_reminder_supervisor():
    while True:
        try:
            await subscription_expiry_reminder_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Subscription reminder loop stopped unexpectedly; restarting in 60 seconds")
            await asyncio.sleep(60)


async def notify_subscription_required(user_id):
    if not user_id or user_id in subscription_notice_sent:
        return
    subscription_notice_sent.add(user_id)
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🔒 Для архивирования переписки нужна активная подписка.\n\n"
                f"30 дней — {SUBSCRIPTION_PRICE_STARS} ⭐. Открой /subscription, чтобы оформить доступ."
            ),
            reply_markup=subscription_keyboard(user_id)
        )
    except Exception as error:
        logger.debug(
            "Could not send subscription notice: user_id=%s error=%s",
            user_id, error
        )


async def get_business_connection(
    connection_id
):
    try:

        return await bot.get_business_connection(
            business_connection_id=connection_id
        )

    except Exception as error:

        logger.error('%s %s', "❌ get_business_connection:", repr(error))

        return None


def save_connection(
    connection,
    last_chat_id=None
):
    owner = connection.user
    common = (
        connection.id,
        owner.id,
        connection.user_chat_id,
        int(connection.is_enabled),
        datetime.now(timezone.utc).isoformat(),
        getattr(owner, "username", None),
        getattr(owner, "first_name", None),
        getattr(owner, "last_name", None),
    )

    if last_chat_id is None:
        cursor.execute("""
            INSERT INTO business_connections (
                connection_id, owner_id, user_chat_id, enabled, created_at,
                owner_username, owner_first_name, owner_last_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                user_chat_id = excluded.user_chat_id,
                enabled = excluded.enabled,
                owner_username = excluded.owner_username,
                owner_first_name = excluded.owner_first_name,
                owner_last_name = excluded.owner_last_name
        """, common)
    else:
        cursor.execute("""
            INSERT INTO business_connections (
                connection_id, owner_id, user_chat_id, enabled, created_at,
                last_chat_id, owner_username, owner_first_name, owner_last_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                user_chat_id = excluded.user_chat_id,
                enabled = excluded.enabled,
                last_chat_id = excluded.last_chat_id,
                owner_username = excluded.owner_username,
                owner_first_name = excluded.owner_first_name,
                owner_last_name = excluded.owner_last_name
        """, common[:5] + (last_chat_id,) + common[5:])

    cursor.execute("""
        INSERT OR IGNORE INTO settings (
            owner_id, delete_notifications, save_media, edit_notifications
        ) VALUES (?, 1, 1, 1)
    """, (owner.id,))
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


def group_connection_id(chat_id):
    """Stable archive namespace for an ordinary group chat."""
    return f"group:{chat_id}"


def save_group_chat(chat, added_by_user_id=None, enabled=True):
    """Remember group metadata for archive labels and group status."""
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute("""
        INSERT INTO group_chats (
            chat_id, title, username, added_by_user_id, enabled,
            created_at, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title = excluded.title,
            username = excluded.username,
            added_by_user_id = COALESCE(
                group_chats.added_by_user_id, excluded.added_by_user_id
            ),
            enabled = excluded.enabled,
            last_seen_at = excluded.last_seen_at
    """, (
        chat.id,
        getattr(chat, "title", None),
        getattr(chat, "username", None),
        added_by_user_id,
        int(enabled),
        now,
        now
    ))
    db.commit()


async def is_group_admin(chat_id, user_id):
    if not user_id:
        return False
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception as error:
        logger.debug(
            "Could not verify group admin: chat_id=%s user_id=%s error=%s",
            chat_id,
            user_id,
            error
        )
        return False


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
                text="🚀 Подключить чат-бота",
                callback_data="connect_business"
            )
        ],

        [
            InlineKeyboardButton(
                text="👥 Подключить группу",
                callback_data="connect_group"
            )
        ],

        [
            InlineKeyboardButton(
                text="⭐ Подписка / доступ",
                callback_data="open_subscription"
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
        ]
    ]

    # =====================================================
    # 📋 ЛОГИ — ТОЛЬКО АДМИН
    # =====================================================

    if ADMIN_ID and user_id == ADMIN_ID:

        rows.append([
            InlineKeyboardButton(
                text="📋 Логи",
                callback_data="open_admin_logs"
            )
        ])

    # =====================================================
    # ОСТАЛЬНЫЕ КНОПКИ
    # =====================================================

    rows.extend([

        [
            InlineKeyboardButton(
                text="⚙️ Настройки",
                callback_data="open_settings"
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


def back_keyboard(callback_data="open_menu", text="⬅️ Назад"):
    """Build a one-button navigation keyboard for menu pages."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=text,
                callback_data=callback_data
            )
        ]]
    )


# =========================================================
# COMMANDS KEYBOARD
# =========================================================

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
                    text="🎲 .flip / .dice",
                    callback_data="command_flip_dice"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎮 Игры",
                    callback_data="command_games"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔇 .mute / .unmute",
                    callback_data="command_mute"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔳 QR-код",
                    callback_data="command_qr"
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

            logger.info('🗑 Старое меню удалено')

        except Exception as error:

            logger.warning('%s %s', "⚠️ Не удалось удалить старое меню:", repr(error))

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

        logger.warning('%s %s', "🌐 Telegram network error:", repr(error))

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

CONNECT_GUIDE_EMBEDDED_JPEG_BASE64 = (
    "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"
    "iOcZBkGQZBkHOOcZBkGQc440H6DiOYcw5hzDmHMOcc45xzjnHOOcc45xzjnHOOcc45xzjnHOOYcw4jiOcZBkGQZBkGQZBkGQ"
    "ZBkGQZBlGQZBkGQZBkF3oGdw1jySZEsgyDIMgyDIMoyjKMoyjKMoyjIMoyjKMgy1GSoyVHNXyOF02qMQSsrcjv6bcb3erbv+"
    "hCUrxp+34eXgOH8yYlFCzNuOYtZK836ZNbvhrd8Nbvhrd8Nbvhrd8Nbvhrd8Nbvhrd8Nbvhrd8Nbvhrd8Nbvhrd8Nbvhrd8N"
    "bPhrd8Nbvhrd8Nbvhrd8Nbvg5kJG5FrdgfB0bl/14Lcb3eqy2mybFi02pP0VO7Sw7tLDu0sO7Sw7srDuysO7Kw7srDuwqO7C"
    "o7sKjuwqO7Co7sKjuwqO7Co7sKjuwqO7Co7sKjuwqO66o7rqjuuqO7Co7rqjusqO6yo7rKjusqO6yo7rKjuuqO66o7rqjuuq"
    "O66o7rqjuuqO66o7rKjusqO6yo7rKjusqO6yo7rKjusqO6yo7rKjusqO6yo7rKjusqO6yo7rKjusqO6yo7rKjusqO6yo7rKj"
    "usqO6yo7rKjusqO66o7rKjusqO6yo7rKjusqO6yo7rKjusqO6yo7rKjusqO6yo7rKjusqO6yo7rKjusqO6yo7rKjusqO6yoV"
    "2ipLn7gNh3AbDuA2HcBsO4DYdwGw7gNh3AbDuA2HcBsO4DYdwGw7gNh3AbDuA2HcBsO4DYdwGw7gNh3AbDuA2HcBsO4DYdwG"
    "w7gNgltRqgR3tSeqh3tHknbHcb4+mnXiX+8mrwp6ha8taG5v7xWvAHNzV9WleAorUZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZ"
    "hmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZh"
    "mGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYZhmGYVVFa8f8A8XonWoxVGKoxVGKoxVGK"
    "oxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKoxVGKo"
    "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"
    "0PlatVHitKoxRTnMrXy0NUoXQocfD+aMmUwM1LUGbGoOU6YouagouGC7JBNd0d3Xy0/QKkosDUqStAn7/JHxxnpjuyN0+Na+"
    "px4BUtFqGLUldyvtS9ohfuW43u9VX6HyEIZQ5uEW39Fwhzl/h+H7YxKGHg6ZWZmJ6CpFEQV2oUFf0BXKZxSvHyHTzFr+lU/f"
    "vYtKvXDxwWhfXOlRalacpgp7UvaIX7nuN7vVV+h8kOlRBMx6qGDdud0rS11gtbTlMiSB1VaWu44HthcpTFqmYErymdJYVf4U"
    "xip0cXFHNQrfUemDbQUB3CIC7QUAlfUecN7ijnQIailPRrvrujW3UuXi/ULbmUCeTDq34pIprfqpV1HPI0xXihQV/QFcpmBa"
    "h8lw3Jn47uIRJ0Ed+w48A+S40CnwS9ohfue43u9Vb6HdUU/yq6/0W262qf6y6p1FYFY5ZBkSlJ16qc7uIWOSQni8spueEytP"
    "2lFaGMtXlpRTmUOfGCmof05K5mMaH99ulg5kXLw3nbSLloZhfbpAR1ysZP0K7qgxuUUPxq0p08dui2VXzqZk+Nd0S+61N8xK"
    "i4MxJUGYnoKkURHWK49xT8QyR6h2/UyL7mjJV8o8ZKsVE0zLHdQrpqj6ZC5UfhU/wT+AhPue43u9Vb6He0LzuXxuLrdbalCu"
    "VoF7RWIiXLZ0xdENOPIJ51MZCuk3kyrRaR3J0529P2blbgGfxd1/wbV4KLcMaSuM1P19CUm20QSXu13I1NXj6lK1pWJu13HC"
    "Lm2ssTy1BzclE1KrKuq8KNzcwc/4tt0D/g1NXibc0NUjm46f7jei3TXU3UEJT/cGrxNutb626vrrbjKELcX2yPjFZI9bR/xk"
    "I5WOVbWydyi6bVaOIuFUkiSkTWMN5Gvz3ReRwf4J/AQn3Pcb3eqt9FvYfWOvqNyCaiqqbCYoV4wlsRSmqcz+QaU6uQekrThv"
    "a/qnT9mrX/NNSpDHVqqKV4VO4Mcob/ql5fhS4LxK2Czg7hT10Vzt1LfvArkfHyvK/wCJT8hllaq1TUxmcG5091uq0yO0KtnO"
    "6JbVdPZ5eiz7e0r/ALin615KU3Q/v/53Wt9bIx1ZCYmpGiSk99nZTK7FOPi31XV0PSqGZrdPFXM2yVYULGNbt+Z5Gv1D/wCs"
    "P8E/gIT7nuN7vVW+i3tj8i7r9F91tUpnWeLnVgna3XPFOkllllrhcN3ysEc58h9yX+DOnw/ZOC8FKFrWm6hampQJF5SeStaE"
    "pc111cCv7KlRbV1Gb1pWhy73ROYhUzHqalR+or/kz3JqmRPxbz6K0C9RM3t92vVgizbEkWKrFffH04uC+7dEn4ODfobda31s"
    "q/LHtiKVWdz32eLhEZJs0jZRu8urHRtX8egliP2KzvPcF2fMa22ZwzMXlNuYF5nLs/O5N8E/gIT7nuN7vVW+h38f1qbMhuto"
    "9KOF4p2RaFjnCbxyWj2Xet1IFdjHmmwoTGpukzdPHfs1kslGoq3/AMG6PPShcKLZCta08n/F23LmN+1tW5MJvjvqMHIoonTl"
    "STpjamyN99K8Koy7xCi8m6cApzJnZu0Zxs/YKMFtxa9NHl925upicL0/y3NXirI7t8s9MU1SmXl3TlJByq1PqR9yrrqOT+Lu"
    "sCax0Qi4UQVdyC76qUw6Qb1/XehXp2fHjU3wJ8BCfc9xvd6q30Xkj1h8NxD1TMW4HxaLTT1ctK8KmOY4KoYm9k3zrSbvrHlP"
    "2nAVCZOSnDj5run+iRrXj+2pXlFpTtHiIr5WyuJQ9OFfMQ9UzvZNZ+UJJGWVm3BeYvx3tlc6H7AhKqnm16EKDfAnwEJ9z3G9"
    "3qr/AEXkpXgZFei5fQSTMspKOis0f3so/JGs3jo71x+3ZOjs3EY/JIs91fIgtzF9D/lVQsO2rWpjF929JWqJ6HKoX1zHLENT"
    "HqoYG+BPgIT7nuN7vVX+i8pTVTqguVxStOWvlbM1HIdyaTAnGpv316S3Uuv3NmyvTOq+dFehqVLUvmSROuZRdCHIssdwoC+7"
    "yIr1RMmYq1Ph6iSJ1zKKIQ9HDlR2puN8CfAQn3Pcb3eqv9F5vgEZIyYTWarCjSph4eqOhoQGfR7MPZhw9p++lnlGEeurVdX9"
    "yirVBWKd0fMKiu+oruRdnRBHLZUUbUOOgVFI41KGMwahxOnMXjxruJ7vKU1SVTlAVdoqKIEMOhMOgUHQKjoFR0Co6FQdFwFW"
    "6JBV5HIBxPLHLX/Ku83wJ8BCfc9xvd6q/wBF6P60HMYfH+Bvt9wL+7sd7xJXyV8vwHMYV/UcOHlJ7vQ4UH6jjUcxhzGHMYca"
    "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"
    "4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+"
    "IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPiBo+IGj4gaPi"
    "Bo+IGj4gaPiBo+IFbViaV0tFDS0UNMRhRNQsanB7je71bZ/zhN5fj5uA4DlHKOA4f2I3u3zjQzyL3G93q2r+P7y/H+2m929z"
    "8jcb3erav4/vL8f2XGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40"
    "HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40H"
    "Gg40HGg40HGg40HGg40HGg40HGg40HGg40HGg40HHym929z8jcb3erav4/vL8f2Ji0MMJBhKMJRhKMJRhKMJRhKMJRhKMJRh"
    "KMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhK"
    "MJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhKMJRhIKFoXym"
    "929z8jcb3erav4/vL8fVdv27AurooatiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JGrYka"
    "tiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JGr"
    "YkatiRq2JGrYkatiRq2JGrYkatiRq2JGrYkatiRq2JDKaYyFf4U3u3ufkbje71bV/H95fj6lyzXg7GOiHtzOu347fjt+O347"
    "fjt+O347fjt+O347fjt+O347fjt+O347fjt+O347fjt+O347fjt+O347fjt+O347fjt+O347fjt+O347fjt+O347fjt+O347"
    "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"
    "3u9W1fx/f/2kv6U8nEcRxHEcfPxHEcfLx9PjuktoXh9y0rxpWoV2huyXbSvoXDMeBRdm3Zqtl5DHoQrSWZvj/ulrJZuFT7Py"
    "86thO6KW6zeRbf8AbG+F0/fd7n5G43u9W1fx/fT8q8jlbp0Ge1xitRjtkbKvLg2psYpS0dorS6FrguJnbjPvH+tu3OyuZpc+"
    "0hlb7lrtdIVx4k3qxdbXEzOba2ksp5zc93sbXQ7y1oLeuNncjO676Y2qIra60drQG0FGdmrqvhhaxUNsRaKRsm3lmm5VUiCc"
    "nteaou7f2ps5Z5dt1ktZk92vsm7RK8WhbfU2ylqe1r2Y3SS6liN9ob/bCg1Wte7Wl0tVL2aFuue2gEgJlJYqqDDaEnJXBc+0"
    "djbqzDa8gdy3cJukd20L8WsS7mVrW9G7Ymjh0R0kq2mdrTNk6g9q7KRd3zdBYOOsq66W3JRL/wATYfyl1ff97n5G43u9W1fx"
    "/fT8r8kp9u2WxTeRuLbDFtkWez+EZEths2JH7TtpsjRe7SbRLeIx2evsF3yhXNoXmpeNqXanNQRFbPs27krNEWpaV0yt4Nbb"
    "RXlNodtOY3YytWjlNClybSJO3491G7OEuhu9aYbGvibv6Al4zY28U3yTdu5ZN7ls21VLrnWE1PbTzVNamza2mCcFtUWyzMLA"
    "smUSx2eNo2fvNoV7tAdW+w8D2U/6Fyr/APs/a7D1Wj4u4y1sHZwzMmxtmdj2U5eF4wlwxeyh6q4gP+RtC/FtkVvtXSW1S3Wl"
    "YZpOrl2Z7JYNt4XtdhG6LS3XHidj7J0U1Z4haFL/ACl1/f8Ae5+RuN7vVtX8f30/K/JK/btj9f8Azu2b7TYf4opX/wC1dpsa"
    "rF3A3vy1zR9oXUS45FbaS08Y2hr2y4aWy8fW7YcZfVvXGi7RYu70vevTXvMXjbaMTsZrzP7ujHto3YptVPLtNmiahbvfl0Xf"
    "L+/bYasrImjTrbdtFK4PbGz2btyNj7xm2s3O7TfxHZ5X/wCLbWoZYq8LtQh6xNtXzKXPcNyV/wDsl19v2YVpq5xX/wCzp6PL"
    "KRVZRdpH2/BUZ2rAPUbKuSZ2hW6xQtV6aRjBxG0L8W2c3gnbSe0G/mk80aWisbZ5s9vRrbqW0G70LqUhYY8ZaNiXC1tefYvE"
    "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"
    "6mSuC3KwkfYEt4tbm0u65C3Ve60ueNt+9nDO1Vto1yTTqA2pSjF/tgXK5ZW5eLqDhYjahNM3963M4YW5YF1StxJNtok03nrx"
    "uA8Nb9h3hLTLud2oychII7QrlgHUDNoz8a/+iQonW5roTtakFsxjHLq4r4vklqoa7up9SydqCsg82qTEoRxbkrJRTm3HLh5D"
    "/wANdf3/AHufkbje71bV/H95fyvftEmeumFFKqGLSpjbPKEaT20FqsYxnjM5bVcrqW2/e45mRS5VeIauDEFrS1JmF37Y/ssT"
    "D+KWGneVC2I8iPDNn9jqnRsi2XD5tL3ArdlyNLCtqSpFeFXBZUg02rr5Jtgle9qMq3FY7mJ2rFWc3bd8REtEtoM4+bQJznuf"
    "aicxp7Z0ybEte8kEW1038qctmWXJTkcnPxly3E6sdFy2t1yvRs3mZWj+4ruvePuCG2OS+N1tp+dsiim3hW2atU2mxxsl4fM2"
    "LDzLva21TZR2xxol4dtFRIhdF1/rs62L/N2nRfhVx3zPdbbUTHHidnNoupFpJXHqm5ktlca/jGL/AOi6Xrp9/sd5GNkXm8gZ"
    "PacuZe5rLZIEtaQTo0uq+Dc9kbIP1mvh+/5qcd1TUp5br+/73PyNxvd6tq/j+8v5XukF+mZSjmq5+IjVMLiLmPDJBhOkmGpk"
    "2pEaXAzWaOzY3DitTDiEDcqmyZ9+m/bEWpobZQ2z28pbapbr2pNOktfZSyo8t59FylgTz7anOy9IJrcRraTv+44B/NP3N7S3"
    "gUxC2cx2lzcSt0zy9JzaNajtklA3vNUjINFaly7RbIXnkYNG746s7HLRcxI25W5LJg7hlrAdkva6LokYpFw3YbR5Wsbbuzm2"
    "Ep6WfbNYGrSDVVt+59sBuprsjLUtu7QbZNcURA3LK2M4SNNX3N7VGJmcXsdpUsPtKTMa6LqLWuzvYyWpVtrEN18LHtnMm9Ug"
    "yL29hltnk482m3BOK2g3lEYt/wDRLKKs5p3tSnHjWyrNkJmX2oWW4dmitoUtBxlm2m+uOZu+IPI25EyMpaEjb0grJxH75dMx"
    "DIL0VKqrRIqZDOFPJdf3/e5+RuN7vVtX8f3l/K913KYrfd13M/fzCGipCTSlFpRpQqhiVMoZZR0XlS/4L8dlqnC4qbzpEVoR"
    "IidMKfOdMqlCJETCiJFqJR7ZCoWZoOAizQb7lWDZeqLZJvQxKHom1RSHSpUrtAi5xWjS+7oi0Iq0Zm7Zlo2K0bLM0HARaot9"
    "x0iKgiKae6rVGtToJnBEyp0CzFu4CTdJvQ6JFQRIiVDNkj1qiSpU0U0gYlD0K1RLUKoJr0RYNm+7gOjRqOjQFC0LQHjWpzEI"
    "UlAZuketC0LT99ULp4TpU6k9KcKeS6/v+9z8jcb3erav4/vp+V7rqSzQLv4BuvhNVxStUHhkDPZF0/HNxBKlIVRwZbcX47K0"
    "uaf/AGlUSGHDh/QVVaJ0TIZwdZCqVUF6K08l1/f97n5G43u9W1fx/fT8r3Oks7eZZ1ZuodFNzJ1s+O8cZw0WVGFYxLg8bExy"
    "Um0gE3FyuLbbahXjWT5K7mrBiRsXipsmj6psfI5nkk1PHHCYZSSD8sxMeFUjnxZBsoeiZI+4uvefspF9SPbRE5SUr5U5dY0w"
    "HhlSt4VV4qh55iZ8LGqFaUi5tGT8hZdAzv8AgDNqnUpThQGbVor5Lr+/73PyNxvd6tq/j+//ALVT4btqEHVo/oZRi4ScP1WE"
    "c9d0bs3Tlmq3dvCPjOpGpTuXvKs6dyDmalXLkRrBR0rCRhYmM3z7o6aCiyVvtlLiXbpv0+kVur/VJbx6s3VxuqotIVv0szNT"
    "CjVRVaaYptpUrhgg8lJg0W9fUcO5V47flcSzBxKv1mqBqzaaUNKVkGzuaeO3ruSk41BpJmLEpOZeUpEzCxnO+41eodGS8HmV"
    "z8rdrMyLqqbtzHx6DiYk6REwsdyusdCedu5hiPEarxMQ+cvY+Bk1nR5iTXSeyswoyKopNNU4uQpItt1114LpzDEjaFa1Xk5q"
    "ZMyMc822SZSR5RimV74oxouVD+GqLq+/b3PyNxvd6tq/j+//ALSnXjTdLxaMwwnoFaEf+JqIhKTUSKm55q0TdJua1dFQRenS"
    "WSfVTX/xUrs5tEzInknv8Hk7/oSdwyCDpB+5Tds7lLyElyVaKUPSZmW/5NIpq1nVIuUUJExnSNPAnbU0bMuaPJtBWHlUrslE"
    "q66fVDGQk5ltbFK9NbhypSF0KkKxW46ajGcio0RhXFH+5Q3ISi7heUllX7krNz1UPafuuulasoU1Kx1Fm9XCVONy3F9sY/jt"
    "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"
    "CNfRSTJsxoachHkXVtLGjPCYyAuNuyZvHqbyIgjmNb0Q0QeNLWXcKEcQx3DiAcnbyc9C9MhAwudF5I0YXExn2UioLvPyPHdy"
    "ODM7bQSSj1oUyyttOlCSCah9VSyhy3FdChyPLkUMVW4pM5G9pnMeMf8AHorScc7WMWqe4pSLXeu0VPCJeYg6OqwMR4oW5Vqs"
    "nbW52Dg1xqmpKU9n805+RuN7vVtX8f8AQk21XjCAnEYxtF0NMT6LjT05Mvyz7q7i44lpc6DWPt9Fc5Ik8YLbqSs+5PWCuGfl"
    "0pctxpqtIx4aHrHWp+sPK/lEz9uhJaPbRMQicsRAfjcL4R0sAovzRp47JBVTrcFzfaba+1O3SLW5XK6MrNi7j0Teq3ZH0bW+"
    "ivRrFnjOFtVJWcWepMbmkZdqtOTkq2kXl3KYw1YmTiLR+2V/Wi9otFloNuVrPnO38aVM18aefSWd8i61CpyE2/ZyRZopk3kp"
    "MJxKSC1HCP8AMufkbje71ba/04X0VmTdwCEIkVVBNcqLRFtS6Gaz5mgwSM1oWhCmZtznogkU6iRFios0G9TFoehWLZOhEyol"
    "M3SMc1KHp0DXjUhalIimQnh7UEIVOhmbc5yt0inOmVQpCFTKdm3UqmgmjuUbpLjoGpRT9KVZNzKFQTTOdm3UN4e1HQNSg6CS"
    "takKYqaREqbqIJFUVaouB0yI4UrRNFNEKN0lwm1RRqdFNWqqCa9C04fzVwODt4ncb3erG7RotpFNNpkYofuHCjuHCjuHCjuH"
    "CjuHCjuHCjuHCjuHCjuJCjuJCjuHCjuHCjuJDDuHCjuHCjuHCjuJCjuHCjuHCjuHCjuJCjuHCjuHCjuHCjuHCjuJCjuJCjuJ"
    "CjuHCjuJCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuJCjuHCjuHCjuHCjuHCjuHCjuJCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuH"
    "CjuHCjuHCjuHCjuHCjuJCjuHCjuHCjuJCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuH"
    "CjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuH"
    "CjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCjuHCh3tLi0RI7RYx1Fbje70yV4l/vNa8KeoQ3LX"
    "+8qH4+sU/KKK0GQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQo"
    "yFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoy"
    "FGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyFGQoyF"
    "GQoyFGQoyFGQoyFGQoqpQGPzf/izZmZzSlGiNOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOq"
    "oOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOq"
    "oOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOq"
    "oOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOqoOq"
    "oOqoOLVUOGNUk/VZNs5l16rm/u6KxkDvm9OHqKFog0/vLMtFq+pI/Wf3lt9S8+r9OR+s3QrdN3KPqwDB2uzincD6sJEeKrnf"
    "wzannZNDvnTgkNDqu1iOHP8AW231Lz6v05H6zdbn3u5Pvkd+GRREoqBiJLUh4Fii2bvblcSDa31zx0Fcbg7u36x7yKZXSxrR"
    "iJesv1E1hpcs9AKyMtabNVhO22cqENDTppZ3DQyFJdpdSjl63YEjrzfXGaNlblYpoyjM8l1M62I0l7OpSs2rdCrSQlYNGtxy"
    "Vwnins3yNR48601EuHz146SVfQf9WbfUvPq/TkfrN0K4TaSk25TdyrKRbpWzEyzbw5u/i4EkHMptCuW8KmSUkWpI2TkW7iBN"
    "KMJhpKVZ03S7qKll5OabOZB6rDvZRrcbU9wwUwSOVauYaGViZ6rKRR8BZuYl+aTux8SGVlXFxkWn6u4dOWnXKbyWjXxo16se"
    "Bdu39wHczLtWEllp6XJIDxFvpaEkWhIxvIRjKL/qzb6l59X6cj9Z+2in3hj9856x5/YW31Lz6v05H6z+8tvqXn1fpyP1m6Di"
    "6TEivBNzsP5SlONXUUwiv6A2+pefV+nI/WbrK5fH1pdm2YRRFCSNpzFUlEFF4qC++xr+VZN3in1Sf1R2aRWN5q1WuGHcrQ0C"
    "6TKreLGSczsbIyDmAaRFwSNLeTQUu1g1mlFlXsm6lLOs6hTSl0VOzFsESUj3BW7S0jlISTgTGk2N1cY6kk3YLRZykrfMTDoJ"
    "v4zGqyjUfDbbtt6nHzsZE0YJwRl49WGkXTB9Gu3vTGVowuaDYJRNyP8A65ygnHS9x/kKEmVkwjmSXiVxVdqyczIquGbJgxfK"
    "Rkq6nA7bpTUe9ZqyE1MKti2rZ1CmlG6DJG2jOW8IyXdIsozqevYUlX6t52+2RkQk6XqFHi01bNvu141xBxppW3pp2i7ZTrFK"
    "VuBRbrbriYdBN+zwsY65HzR+css8i7TzqOrpiJJzIzEQo4iozgnGTs4Q8y1yVeNP41t9S8+r9OR+s3M3q0euKXZK0q1uSRZt"
    "46XUZW8/lHUmda5pJwg2umSaIubpknaLmTcu2rt2q+XcXY4K3kbkWeqvp59Ios7gfsW5H66aLBekJDoPVmyXWrdE0erMVKyL"
    "irBB6s2STdLHbSzqjOFPJuVG9Jt7Rdw9WdJknnxJFa45Bc5pFwZJedfOVw2erMwjc0k3RJJuU12M29jUlZx8ssWbekKc9VDu"
    "5Z2+WcOFHbhC6JFshSSclojcUg3Km+XSQUkXCgeXHIv0BqCQz0dq0ZtHqzFRJ6sg3aXC/ZNmk09ZLv5NzJqamk+nq+XqypPP"
    "6PvH3Dp6vesmZ7JTp1V+uXwmmnpiJTz5F2tccgudpNvWKr1+vIqnerKNICTMpcDu4pBdNtPv2bVO5JBN28mnr5Ra6JNcrl6s"
    "8p/GNvqXn1fpyP1n95bfUvPq/TkfrP7y2+pefV+nI/WbkUqrrO1knAjoVSRI0hjuk1k6oqso5R8TwBWlaxypSRkTnc1iKndP"
    "GR2RqR6pk5BieNd+ErdeyizPqeH1o7UjFU3q0GsQykOoWlI1QzokSuq48GUoZ80MwdEiV1XHgh6BOHOdv4O461zGHboKwCid"
    "VGh0mqceZRFaMWbJN2SjlHwZbq3MYdug+iV2C7mJO0f0hVTOUWh126MKouzSTqsrSNUM68JW69KJVcU8KVonSNUM6ShlDpro"
    "nbLEhK1LSDV4OI07ZZWJOQrmFO3IlDKHTXRO2W8JW69lFmfU8PrR2pGKpvVIFYqikPyJGt89HAZsFHtSRJ1FXMYqgDwSpQyb"
    "dLFu491kTiFlHLSOO6SViVkaIRKriiUYstHnglSE/jG31Lz6v05H6zdbqSRFYxo2OVo/bMnUfLJsG7w9FXZFW7Vq4dJnZKPm"
    "rl3V81amavmzU0q76gycoi2hpZcjl6k6oWBTRbtGKyqDZ8vJNDyDCXRaJrSJE6VnCKPFpVBNktLpoCbcpvJRaVQTZST5BduZ"
    "FBaD8UbGdqKt2Uc4lmhjsJIqca8dtHqUtIISbeElaRSSThi3cqKt2UcrLtTmpINTSVJhvRy0kOvap0aVeocG72s4RR4k6oWB"
    "h37eJq5ft361ZwijxY7OSpKPKP5By8SUWWq3esCPmSTleTSqykpJsoksdnJUlHlH8gk6oWBTRbtGKyqDZ8vJNDyDR6hikJaj"
    "+NSkW5ZqSfFVjo5wh0jc7RJCkgjHJprMmLwijZ7HmftXZUV2Cb5Fckeh4zQhW8wgc6soiqgrNJBciZP4xt9S8+r9OR+s3UUM"
    "UiahkT+m4erO6emdwooh+wcPVndPN1CnTeVw9Wd0/n231Lz6v05H6z+8tvqXn1fpyP1n95bfUvPq/Tf151/7yzJzulz0VX9N"
    "A1XbL+80NVo09RFUyCvKR6K04V/u2ErcrpxVyt6tK1LUsopy9e3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx1"
    "7cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3H"
    "Xtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17cde3HXtx17c"
    "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"
    "pqU1KalNSmpTUpqU1OUU5RTlFKalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmU1KalNSmpTUpqU1KalNSmpTUpq"
    "U1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqMS+UumaDt4qJ96mE5Z"
    "XNe8phOWVzXvKYTllc17ymE5ZXNe8phOWVzXvKYTllc17ymE5ZXNe8phOWVzXvKYTllc17ymE5ZXNe8phOWVzXvKYTllc17y"
    "mE5ZXNe8phOWVzXvKYTllc17ymE5ZXNe8phOWVzXvKYTllc17ymE5ZXNe8phOWVzXvKYTllc17ymE5ZXNe8phOWVzXvKYTll"
    "c17ymE5ZXNe8o2/ePWqbxYqU1Ka5v/B+BAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECB"
    "AgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIf9MxIkSJEiRIkSJEiRIkSJ"
    "EiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIk"
    "SJEiRIkSJEiRIkSJEiRIkf8AppppGUi0o1OLhnNlGZxcNZ8gy0jSRZX/AKDeNo7ZVtrsJRKW5Q1FrN5uOTyluTtRZzeYdto8"
    "ZRtnt41SAiR8dDJERIipD0unR55Lv2/23icydhtmm8booSmSrJ4LGKL28U1vPKd+3+2capERmB25RIKJAhnIQTKMoikMsBpE"
    "QVMuQVIJxQ7SAuY8kaziIijKIKmWA0iInFAzek86MryjLXq4pPJ25Q1RZJa+YVGXDrKjPFNbK8o016uNoZziZxI9oiRIxiL4"
    "WUTyT1jWZBcmU/x4oi+c7CMVGs4ke0TtPWL5JCB6hV9J5dJ+XdxTOnE5lMnZk/IvEX1wH6yZU/4UWPr4pDJ+QdxXOvSj4+Pp"
    "dKZAy9WmxkUakj9jOyMyR+3mZJNIGXS028q/+BkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkerxIkSJEiR61EiRIkSJEiR"
    "IkSJEiRIkSJEiRIkSJEiRIkSPogn2ynUV+3U+2Uz9RX7YgQ+w5NNsrleVy7VU8+ZO9chgCcqq1m8xfnKqtZvMX5yqrWbzF+c"
    "qq1m8wBOVVazeYAnKqtZvMATlVWs3mAJyqrWbzAE5VVrN5gCcqq1m8wBOVVazeYAnKqtZvMATlVWs3mAJyqrWbzF+cqq1m8x"
    "fnKqtZvMX5yqrWbzAE5VVrN5gCcqq1m8wBOVVazeYAnKqtZvHkxzi7ZpK67oLYijTKsKrLSQVONM/UV9B2WVbVGWUiqj+ZJe"
    "4zu4p6svwy2DTKsKrLSQVOpTBM7t+xzqUsxT/H2Z1X++fJmJw4QuJE8VywzTaTP5u/zmNuo2v4mNmo2v4mNmo2v4mNmo2v4m"
    "Nmo2v4mNmo2v4mNmo2v4mNuo2v4mNuo2v4mNuo2v4mNuo2v4mNuo2v4mNuo2v4mNuo2v4mNuo2v4mNuo2v4mNuo2v4mNmo2v"
    "4mNmo2v4mNmo2v4mNmo2v4jvhYwrX/I5gnqWP6J8SWyGSzzJ+VYyrDwWr/7ky9o8dtOm1dt50ycSZ+or0l9AZG/Zkz9h80zS"
    "o5YZvuJPwnkjyCPWVZXvS+wZlUgnFEZpMtx7FuXKP+D0gfZmaK+pb4pYP+CraZXD3v8AN96R+A/mSXuM7uKerL8Mtg0yrCqy"
    "0mVPHTi0s3zY0jlfJRESxO/rHBV+2j544/xhH25E/X4HCZ0juX0k/wAkRf0/TiTP1FfQqTzhK5LDknioidnZ3ZiT8J5W7gj1"
    "lGk7lusJPwnkjyCPWVZXvS+wZlUgnFEZpMtx7FuXKP8Ag9IH2ZmivqW+KWD/AIKtplcPO/zfekfgP5kl7jO7inqy/DLYNMqw"
    "qstJBU8VP/0a99n/ALJ1jgt89a/CvxQ4U/PWfwp8V4kz9RX0OcThK5LDknioidnZ3ZhxwolbuCPmUaTuW6wk/CiSPII+ZVle"
    "9L7BiVTfOKIzSZbj2LcuUf8AB6QPszNFfUt8UsH/AAUbTK4e9/m+9I/AfzJL3Gd3FPVl+GWwaZVhVZaSCp0p/wDo177P/ZOs"
    "cFvnrX4V+KHCn56z+FPivEmfqK+JgQIFEh9kwIECiUSiUSBAh1OTy+VyWHJPFRE7OzuzDjhNKncEeojVi3WEn4TyR5BHqKza"
    "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"
    "O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O"
    "1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1"
    "uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1uMadTtbjGnU7W4xp1O1u"
    "MadTtbjGnU7W4xp1O1uJRwklDzI5ZRm1brBppWlpNZ/slc3UWM3iGZS+Yd8iy2qM+b7/AEuXN1FjN6bLm6ixm9NlzdRYzemy"
    "5uopk9NmsvUaalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmp"
    "TUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU"
    "1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUpqU1KalNSmpTUprm/8H4ECBAgQIECBAgQI"
    "ECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAgQIECBAg"
    "QIECBAgQIECBAgQIECBAgQIECBAh/wBMxIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiR"
    "IkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkf+mmmkZSLSjU4uGc2UZnFw1n"
    "yDLSNJFlf+g3jaO2Vba7CUSluUNRazebjk8pbk7UWc3mHbaPGUbZ7eOiKkOvqmTi7OkiR9F50eeS79v9t4nbtp62jDPaSuTc"
    "1eUIx4preeU79v8AbONUiNZEgZIRIJCJDJFSCQignrFRIEMgjOTiREgQRcwiIoiRUyLkETziw7CCIZIiQURMp4IqZYHg5hUh"
    "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"
    "Vlxi1I9Jqy4xakek1ZcYtSPSasuMWpHpNWXGLUj0mrLjFqR6TVlxi1I9Jqy4xakek1ZcYtSPSasuMWpHpNWXGLUj0mrLjFqR"
    "6TVlxi1I9Jqy4xakek1ZcYtSPSasuMWpHpNWXGLUj0mrLjFqR6TVlxi1I9Jqy4xakek1ZcYtSPSasuMWpHpNWXGLUj0mrLjF"
    "qR6TVlxi1I9Jqy4xakek1ZcYtSPSasuMWpHpNWXGLUj0mrLjFqR6TVlxi1I9Jqy4xakek1ZcYtSPSasuMWpHpNWXGLUj0mrL"
    "jFqR6TVlxi1I9Jqy4xakek1ZcYtSPSasuMWpHpNWXGLUj0mrLjFqR6TVlxi1I9Jqy4xakek1ZcYtSPSasuMWpHpNWXGLUj0m"
    "rLjFqR6TVlxi1I9Jqy4xakek1ZcYtSPSasuMWpHpNWXGLUj0mrLjFqR6TVlxi1I9Jqy4xakek1ZcSabJFNn/ADWtLm+CE8zu"
    "ktg5cRodvr3f2GToLm6ixm6LDxt01TdrBTn8srmveU5/LK5r3lOfyyua95Tn8srmveU5/LK5r3lOfyyua95Tn8srmveU5/LK"
    "5r3lOfyyua95Tn8srmveU5/LK5r3lOfyyua95RmWy1paLL1qP3qSKbZzfwblD5phPvWO7+5Bhl3I3XhN5PO0v6qSzhI7Y8GS"
    "pS9a5u7P8PaPp1lr9umrxU+7Ic/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WV"
    "zXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8p"
    "z+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZX"
    "Ne8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17yn"
    "P5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc"
    "17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc"
    "/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVz"
    "XvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz+WVzXvKc/llc17ynP5ZXNe8pz"
    "+WVzXvKPHzx8tJ60qr6+iubqLGbqDDtt61QdpFSR8G3jfhSpaPqTP35vj7BzJJHNzKtsIjPr3qSzhI7Y8GSpS9a5u7P8PaSi"
    "Vv5W1SftR9IlzdRYzeOduXj5aLplVX1Ej4NvG/ClS0fUmfvzfH2DLEjmt1FIMJ/fapLeEmdiSJ7V/RL+4fP3spb5R81FfSRc"
    "3UWM3jZsmFh+wy/ftxRexP1WxUtGm5HNbqCwYT++1SV8JYpRkrMPWt39+4fP3spb5R81FfSZc3UWM3jZPPMpksmSTuu/++3P"
    "HONvG3rVN4sV9KFzdRYzemy5uosZvTZc3UWfTZrqNNIFMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplM"
    "plMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplMplM"
    "plMplMplMplMplMplMplMplMplMpJD/wfh6bQ/6XePUd5O0VXrWdYFBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL"
    "3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8o"
    "LpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJ"
    "e8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3l"
    "BdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLp"
    "L3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8"
    "oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBdJe8oLpL3lBd"
    "Je8oLpL3lBdJe8oLpL3lBdJe8/5WfJa7xh8jS0VyL4568oJBM6jDFD04aZRtIKOW/wD+NrOnjUWm8aaX7vTp4tGDadnjXPya"
    "enTfkKOvk2fu8Y5+TTjftKw7Vpkd86eMo0jf97hlt+w/ZdvGvHSh9yKZM6iO5Q15TcPENto7ZVpRlZQ/Ski0UGGVZZgqx9HG"
    "/IUdfJs/d4xz8mnHKvkWiS/Isj352x/fOPlafP8AkYwQfOuaojx0pKHjTTTLpjJEdyVh20jTKkpZR6+YdqSVlGH7bKHKO3za"
    "q+aydhJHnhtO0WKdnE45vRXlc44pc3bjmyknlKOnUFRSWNsvHKNM+clSK09dsj+T8izyrtcqD9+1yTNHO0NyNGGFaZXKNPFe"
    "yRWl/uUdyVHrpFbUkrxVdtI2uYbRzRixFV85J2lbdMtKS75ESSI2wiquUcyhrm7TS50HUmR8xTeLlUcRbpuHmWBzdjnHJ9g+"
    "ZdMMIw0sEGFR2+Z5KMF9F2/IUdfJs/d4xz8mnG/ZVt2rLI4ZVh2jLQ26bWUMtomQfOW+UR86zjTt9KII8SCEocK3Bt3nQZal"
    "Kr4TI5dNq8V69zjp02y/bbVMinJPXDS8mkUUdcplV4kOJyw/coqUI+0dOG2WG452h2kodu+TRi0bkraOEYTKsSUOFeois50G"
    "2ZQ/Sg0kEH0n5R2jLPYLzptmgqe0fO0dSVWE/uUdrKEdIjCRGZKrLhXccqlCUK65KECTsKw6Rloeu0esKwoykqYZ5NE9o7ky"
    "Muldr2jCSlylBEiSdyruLTedTkm+c8pDIP3TxXjL13lgNO3zx4w20nou35Cjr5Nn7vGOfk06u+d8qwrA7ZoMoz6RN+Qo6+TZ"
    "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"
    "b7w73hczjsT1VLadsw4V4xIPcUV2w1IQ0cIs/e8d5ySQgqZZCJl2QncjshPYx3YTtjux3Y7IIFVNFFckZDEy7ITtjsgjtjtg"
    "jsjtjthCO2PNUIpZT/8AXxP9iEkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk/wCyCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCP5NNar1hqDPRnUO1y/vgf6M"
    "6humf98jmqxYcn8g6dNar0Y35nT9Kzpmw3Xft6jpWdS2Ha7lSmtJ6sd8u1FkVY9tOcC5CLPslWFEWfMfomn71T6f3+3Z1XW0"
    "ulydrsUal7TbU37PS1P3an0/v9+1Fgc6UM4yFVWjp1QV2kEqq5DlVCcpGqq6jVyzEVVXsn5EyTDhXIqCe6LaTMV0JJ6w6UM9"
    "RFcoi/JSVdoIucKIualqVJzEVVEXNS1Kk5wIs+XfRLkunN57OvamHe75wdH/APNT/ROz0s5Lpree1nzH6CrkgsfIVYIiBPVW"
    "BfeE/CN1UTPI/i7IgblkJ7w7Q0aLEC6IWkH6H8I1URBM1kasZKJm6T5waKgqS4avyU+cGioLmJms+Xeg6i4qQ7RezqaS1qLq"
    "bfmdPTWlSaxfknZ1/UX9SG6J3oQj20J2R2LoN0809L6QdRSxUzb+4zrOnfo//r+o/rOnZq//AL/odV6QdWSxTyb+/wDsKVVk"
    "txoW1LxS2pbcMba1FpogqwWlJUQgggggslksi5CvLaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1Lalt"
    "S2peKXiltS2pbUtqW1LaltS2pbUtqW1LxS8UvFLxS2pbUtqW1Lal4paU6fpHVyn6CpKnruX+/of5F0/4nft/4L6D6f8AE79v"
    "/Cp6CpNSWuX+/odT0r+nWD1yrXfTF9IVtkKfWVXlXq3M0G9c9wvUOTMxDnJkU+rqOdCi1XC9XURTHP4E6uq7RDEV/wAJiK/4"
    "TE1/wmJr/hMTX/CYmv8AhMTX/CYmv+ExNf8ACYmv+ExNf8Jia/4TE1/wmJr/AITE1/wmJr/hMTX/AAmJr/hMTX/CYmv+ExNf"
    "8Jia/wCExNf8Jia/4TF1EXNBla8SRFWdfAvyy7iCIMHuF7E7JJJJJJ7FUcvYiEfePT07x0HRUEpt7E7eu6VKjTpaKVFVjj0j"
    "6PVqKqDkjIpQxtpT4ii07CiKjkEVKSKUXIrpHVVtqiEZSe8p07YQnxD2I9IUorYfCjNfAv17iDE7HL2J2ySSSSJ2KL2IKL9z"
    "T2T30OhfTp1EWosIUFRzEVunZAuQiFXNpQ9Xq1g6ig2rSPSPSLSq5DrUQU4auZWfKiOVELSqIqtGZrI6ShSzESE8V9r9Ruvg"
    "X692mKovdXup2L2opa+8EKtCp1EWBrut9Gulss/osfsp03+Kepp5V2o/9l/8/Y6b/EPQdRlasr+bL99P3Jns6h1huZ0SXvUO"
    "cOb6p6Val6PoouhhRelkwxhZMIOoLSSSlRvEkayyKIgviPtfqN18C/XuIMHL2J3F7qdiioIQR4uPDMdZWToOsZUbd1PmdR6A"
    "6DqvWRtlfy5ftp+x1f8AhfqqWfTrb/Zf3WP3+hT6jrvRjrKK5nzhf+NFyOm/xVWbCdSyeUyXn9f2Oo9OUOrbFNY/XL//AA6D"
    "pbpiKdQ6w06+pe1RNuyCCOzqfcOm9zxn2v1G6+Bfr3EGju6veTtVBOxRfvBp0TVvEgxLaTMz/N6CfM6j0n09Vth6ShV9H0us"
    "f/oMs/3/AHpBU/w31qJNKHfsv7/+lPqOu9FustVzPnC/8aLkL/iHqKrbNZJ/v+9jEsesyLuST3Op9w6b3BPF/bfUbr4F+vdY"
    "O7q95O5BJPjJ8NRq2NCr1T35Ko6pBRrNTUoekqVP5Cem2J8h/UUuspK12aHWdDTR62ch3TObpmI97MhvU/iQbVY7Re51PuHT"
    "e54z7b6jdfAv17rV7F+/p7YNDUiRG2ReBryn1L2aDqy1NexyTqL07V0HUHpoI97MhvU/iQbVY7RTqfcOm9zsVYz8V9t9Ruvg"
    "X691qiDvYL7Bfu5EEQcIi6iipAip8zJRJRR0FolO6qIuo7p2roOoPTQlYslKqjEhRtRrtFOodDYOnqfwL4n7b6jdfAv17qDV"
    "HewXtkkknxkeCjsQVRNTpKLHtzQSijq1k6zpmshEG9A52YnSOtWRPRtSJMG61BV6ZWrClPpVeg+nYWBV73U+6U6N42R1F7RZ"
    "+fZRqW0hdfEfbfUbr4F+veRYJkVO+v3tPb0nU3S5lLqKT6kxmdaluoiIUaVhURVOu9RUsjHLh1U6FVdV9YqUqdV6tgoUkpo5"
    "Dqk/1D597qfcOm9wcths9iNcuiDKNRFnxH2v1G6+Bfr32qLmKkCd+CCCPvOSYKVaysjurVVkb6QchX6papjFRlkodQtNZQX0"
    "i4b1rsyrUtOk173U+4dN7g5qP1EpsTRPFfa/Ubr4F+vfRRHC+QNCSeyFEFUtILn3+p9w6b3PGfa/Ubr4F+vfgkRSeySfYr96"
    "OJ7JFUjtXvdT7h03ueM+1+o3XwL9fZJ3JJJE8j9T7h03ueM+1+o3XwL9fZz2SST2SSSWiSSfBx2wR2x3oI7I7II8XXSWHTOT"
    "3fFqqIkqM9epI3XwK5+WalJWLKCdQ8xLjEuMS4xLjEuMS4xLjEuMS4xLjEuMS4xLjEuMS4xLjEuMS4xLjEuMS4xLjEuMS4xL"
    "hXPqrBSp3aZ6jcvA3aalhC7Qu0LtC7Qu0LtC7Qu0LtC7Qu0LtC7Qu0LtC7QsIXaF2hYQu0LCF2hdoXaF2hdoXaF2hdoXaF2h"
    "doXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF"
    "2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaC0GLqhcU9i4p7FxT2LinsXFPYuKexcU9i4p7FxT2LinsXFPYuKexcU9i4p7FxT"
    "2LinsXFPYuKexcU9i4p7FxT2LinsXFPYuKexcU9hKTU0LCFhJn/Y/JJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJ"
    "JJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJP8mYIIIIIIIIIIIIIIIII"
    "IIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIII"
    "III/k01qvWGoM9GdQ7XL++B/ozqG6Z/3yOarFhyfyDp01qvRjfmdP0rOmbDdd+3qOlZ1LYdruVKa0nqx3y7UWRVj205wKsCL"
    "Pm70TT96p9P7/bsr9S+m6xTpq5TperTqZSIcmqdnpan7tT6f3+/aiwOdKGcZCqrR06oK7SCVVchyqhOUjVVdRq5ZiKqr2T8i"
    "ZE95T3hugqqg5VEXKRqqq9lo18z+iXJdObz2dR1LOmbaedDRqIr69XJXfLs9LOS6a3ntZ8x+gq5ILHyFWCIgT1VgX3hPwjdV"
    "EzyP4uyIG5ZC6kQg3JBY+QvyPyie8IsnzkanmfoOouKkO0Xsr9L1Lupv6apxJQTqkX/XVI47Ov6i/qQ3RO9CEe2hPN3S+kHU"
    "UsVM2/uM6zp36P8A+v6j+s6dmr/+/wCh1XpB1ZLFPJv7/wD4qr/KZ9VjPeUxVLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcx"
    "NLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcTqKS5Sa+W9BvUU3fM18F1VdWrYYUuldUS0uRgfzGC/MYL8xgv"
    "zGC/MYL8xgvzGC/MYL8xgvzGC/MYL8xgvzGC/MYL8xgvzGC/MYL8xgvzGC/MYL8wvRL8nFOo+g6FEWUlPLT222q1B3SPTTMV"
    "lSlnoN6qo0b1ifxIN6im75mvtqX+rW9bxHWtSyjjpFmn9wyT99upMfqg7pGLpkO6R6aZisqUs9BvVVGjesT+JBvUU3fMRZ7I"
    "9h03xU8R1nw0/U6P4a/r5hdSY/VB3SMXTId0j093MVlWlnoN6qo0b1qfxIN6im75kz3um+KniOs+Gn6nR/DX9fGSSSWiSfum"
    "SS0Wi0WiSSSfBPYx2qDulaumQ7pXpoWalPPQb1NRo3rE/iQbXpu+Zr2Itk6avb9R2vh+s+Gn6nR/DX9fuFPuaRVLRPsJJJJ8"
    "I6mx/vIO6Ri+7kO6R6aZisqUs9BvVVG9mh0/UXnqu1709k+26z4afqdH8Nf18WpJPZPYniU9nIqk+2RRFJ9qonsHUmP1TtYj"
    "5lgxXKnrJC9xRByiEkiKKoiiqJ3+s+Gn6nR/DX9fFr2J3E8Sns1UnwEiKIvdTtXuKJ3E7tJl49GjOmpt+UmndUQgRCBRBexR"
    "vf6z4afqdH8Nf18WveT7jVfCIons17JJJE7vTfFTvqJ2JqKooguva0nvdZ8NDo/hr+vi1J+518Mik+zgjsg0E7nTfFTvr2QQ"
    "QWU7IIIII73WfDT9To/hr+vioILJBBH3Gq+IRfZT3I7E7nTfFTvr4HrPhp+p0fw1/XyCoviEE8H03xU8R1nw0/U6P4a/r5BV"
    "fFIvg+m+KniOs+Gn6nR/DX9fICi+KQTwXTfFTxHWfDT9To/hr+vkB3jE8F03xU8R1nw0/U6P4a/r9/qO8Y3wXT5VknxHWe4h"
    "0fw/v9RfGIJ4Hqaa0320+ZS6tqpFQxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNLcxNL"
    "cxNLcxNLcxNLcxNLcXqqSfMc93UPGMu2o1Pv9RfGIJ4HUf0bF93IwX5jBfmMF+YwX5jBfmMF+YwX5jBfmMF+YwX5jBfmMF+Y"
    "wX5jBfmMF+YwX5jBfmMF+YwX5jBfmMF+YTok+binTbTSG+QF8agnmlfGoN+/YIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIIII"
    "IIIIIIIILJdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaF2hdoXaFkggggggggggggggggg"
    "ggggggggggggggggggggggggggj/AGPySSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSS"
    "SSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSST/JmCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCP5NNar1hqDPRnUO1y/v"
    "gf6M6humf98jmqxYcn8g6dNar0Y35nT9Kzpmw3Xft6jpWdS2Ha7lSmtJ6sd8u1XQWt/YqsFqdO7azjzf6Jp+9U+n9/t2V69W"
    "m6zTp2jperTqUXKFTVOz0tT92p9P7/ftZuO0ElUyEtKTmIqqSswSqLCiqswhKpqKuY523YqwSKsIR6sk5SJKpIiqqHrTAqro"
    "S5NRy7CLlI1VVT1hFVT1pgVVSBzo0M1QlZgWRFWYURcyfWgVcxzthVFlOxV9aBywLPyJVFzHSgk6qeso1ZyUSVGrJaNfJXol"
    "yXTm89nUYiEw8fU9HKiOqNd8T59npZyXTW89qLZyUV05IIkIM+YqS4avyURYUX1lyPdcOW1kg7JRUhOxyfM+cjs8izyN0VBI"
    "jMZoJ7wuTpHLOSC/I/KJ7xrqMP4h5GUmUZmi5HzzG+8IsKpPrDtRUhB2gqRHYqTmg6fmOXMynIfoatGuSBuayJHzGnzkankr"
    "oOouKkO0XsrN6u3NFyRydL0q0VdUqLLndnX9Rf1IbonsU1X2cEJ3I7kd2OyOyEFbsR7CO2OyE8ndL6QdRSxUzb+4zrOnfo//"
    "AK/qP6zp2av/AO/6HVekHVksU8m/v/sKVVktFtS0paUvFLaltS2pbcdR1HU0/cj+/qO9JekU/hb/AML/AOi+l/SKfwN/4X/0"
    "/wA49Jfgb/wv/p/m/pH8Df8Ahf8A0/zj0j+Bv/C/+lP0p6Qf/C3/AIX/ANKPU9U/30T+/qJUX5ltS2pbUtqW1LaltS2pbUtq"
    "W1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1Lal"
    "tS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LSltS0pa"
    "JWfAu7i95WoorGlyxS4aXLS5aJSahl5lbr4F+vcXvJ348ypr4F+vcXzkmvgX69xfOSa+Bfr3F9srkbqX1Pcvqe5fU9y+p7l9"
    "T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6"
    "nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1"
    "Pcvqe5fU9xtRrtF+6k18C/XuL3571WpYTkax1VZMNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbk"
    "w3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5"
    "MNyYbkw3I+grElCjVVfVd90pr4F+vcXvU6TnjPR6rqJ6NQ/y0/ywX0YO9HO+R1jVZWVi/IRLKQn33U9Spl90pr4F+vcXu0mW"
    "3QdN0qM1FaiD67aeoi2u2T0vl1r/AKf0QrU7t0ffdb4i/dKa+Bfr3F7vRJNQQr1VppKIV+oc9x0vUOfCQJoaCoel/wD7X/T+"
    "iHpBId991viL90pr4F+vcXu9B75lA+sy1ZU6jpkeqOah6nTsko121WyhqLB6Y/8Auf8AT+iHpH3/AL7rfEX7pTXwL9fY+j83"
    "lRFswhUpVVqpmUWKjPWU6+ra9RDoX3b7CnyKtV7XoiIelVnrH/T+iHpL3yI++q3xF+5U7ia+Bfr7HoPfETIukcpUddoMVKlV"
    "XKV1Rjke06Z9tp1FaxUREPSiz1b/AKf0Q9JJ6/33W+Iv3SmvgX691exezoPeEyQRZOufCQUuhVyTI/oVRNToalmWKdV8VD0n"
    "/wDU76f0Q9JO9fvue1qwoioun3nW+Iv3SmvgX691e50ToqCRZEK3T3j7QxqNbBEpmJ0sVbaFXpbb0U9LJHWPT9P6Idc5Ff3+"
    "obLZEWNBK70GV7axH3jW+Iv3SmvgX6+xY6ysodL1aPSFEeyC0grkLaCOQvUQ9Kvt9Y936f0QqOV7p7q1mIO6lf4RXvfl2Iiq"
    "sIU2WEj7xrfEX7pTXwL9fZI6NBOqqJ8zGVdzGVdzGVdzGVNxeqqu+Z1CzUVexXRqL1DE0HdQ5dCHv5G9O5dRtBiaiJGhWbD8"
    "ilTsJz95VviL90pr4F+vgHPa3UqORz1VB1Z7hGPf8hvTfiUbRY35d2EmfvOt8RfulNfAv19hBHcWqxPmO6j8KC1HvEovUe2w"
    "sKNYxubfvutnUWPuGPYJr4F+vsVVE1FrsQXqXfIl9TkTpnrqN6ZqaiMRNCDqKcpaQpVrOTi+p7l9T3L6nuX1Pcvqe5fU9y+p"
    "7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T"
    "3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6n"
    "uP6hI9QoMtutKQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQR7JNfAv19hVrPa6yI19Tkb0yrqo2gxoiImnefQa/MwvJheTC8mF5M"
    "LyYXkwvJheTC8mF5MLyYXkwvJheTC8mF5MLyYXkwvJheTC8mF5MLyYXkwvJheTC8mF5MLyYXkwvJheTC8mF5MLyYXkwvJheT"
    "C8mF5MLyYXkwvJheTC8mF5MLyYXkwvJheTC8mF5MLyYXkwvJheTC8jemamv3UmvgX6+wsoqz5uTXwL9fOya+Bfr52TXwL9fO"
    "ya+BXPzs3LwNhCwhYQsIWELCFhCwhYQsIWELCFhCwhYQsIWELCFhCwhYQsIWELCFhCwhYQsIWELCFhCwhYQsIWELCFhCwhYQ"
    "sIWELCFhCwhYQsIWELCFhCwhYQsIWELCFhCwhYQsIWELCFhCwhYQsIWELBYLBYLBYLBYLBYQsIWELCFgsFgsFgsFgsFgsFgs"
    "FhCwhYQsIWCwWCwhYQsIWELCFhCwhYQsIWELCFhCwhYQsIWELCFhCwhYQsIWE1/2PySSSSSSSSSSSSSSSSSSSSSSSSSSSSSS"
    "SSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSST/JmCCCCC"
    "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    "CCCCCCCCCCCCCCCP5NNar1hqDPRnUO1y/vgf6M6humf98jmqxYcn8g6dNar0Y35nT9Kzpmw3Xft6jpWdS2Ha7lSmtJ6sd8u2"
    "1wI6fHosr2Ss95yx3dfKXomn71T6f3+3ZVqNpMV7tEOj6rF07yI7PS1P3an0/v8AftRYG5rJKzAirMCqswgirMKOVZhBFWYU"
    "n1oFdnl2KqooqqmoquTMVYQ9ZMxXbCT8yVUzgW0mYrsj1hHZSetqIs9nrDVnURXKKvyEn5iTKwIqzCk+tAi5wpPrEqugi5wv"
    "Y8W1qWspPWJlBk+UvRLkunN57Kten1Ve7c5EY391PRD23SsnOV7PSzkumt57WrAmbshYtZia5C65iWSUVZEX1hfeFSI7F94e"
    "P0HaHqixoo3UWyStkWIF0QVyQfwiWROOzXUYMFydJaRRuqn8R/EOyWRu5CTmJHy7HiuSBUyLSQInqjF+XlLoOouKkO0XsXo+"
    "nVZViHQ9FhUW3Fr/AK7Ov6i/qQ3RO2E7ITtgRorfYQnbCdkILkSnyESEIQz+RmR3I7/6HrH6kdkJ2R5T6X0g6ilipm39xnWd"
    "O/R//X9R/WdOzV//AH/Q6r0g6slink39/wDYUqrJaLaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1Lal"
    "tS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LalpS2pbUtqW1LaltS2pb"
    "UtqW1LSlpS0paUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1LaltS2pbUtqW1"
    "LalolZ8C7zs3XwL9fOya+Bfr52TXwL9fOya+Bfr5rfVazUf1DnaZFFVViT7FNfAv180vrNYPrOeNoPcMotZ7JNfAv18zvrsa"
    "PrOeM6dztchlJrNPaJr4F+vmV/UNbpmOqvqZDenc7XIZSazT2ya+Bfr3XPazVS/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nu"
    "X9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuX9Pc"
    "v6e5f09y/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5"
    "f09y/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5f09y"
    "/p7l/T3L+nuX9Pcv6e5f09y/p7l/T3L+nuO6hiaZj6rn6jEavvKMfRZoX9Pcv6e5f09y/p7l/T3L+nuX9Pcv6e5f09y/p7ja"
    "jXaL3U18C/XuV6tjJNSnRdUzMLyYXkwvJheTC8mF5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyY"
    "bkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcm"
    "G5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MN+YejW6L"
    "IiToN6dV1MNyYbkw3JhuTDcmG5MNyYXkwvJheTC8mF5MLyPoKxJQoVVd6ru4mvgX69x/r1c/LSrGo7qGp7o57qijOnVfeGtR"
    "ungKn+nUy7ia+Bfr3Ptvr5YVY1H9Qie6S+oo3pvxDWo3TwVf4i9xNfAv17n2317F8pValiIEWcxXI3NR3UJ/CS+oozp/xCNR"
    "uSeEr/EXuJr4F+vc+2+vlNVRElR7lesqNrK1tlCH1FG9On8QjUbknhq/xF7ia+Bfr3Ptvr5T6h/8CdiDYjLxFf4i9xNfAv17"
    "n2318pr07VzMNyL0zvkpSa5iWV8RX+IvcTXwL9e59t9fN1f4i9xNfAv17n23183V/iL3E18C/XufbfXzdX+IvcTXwL9e59t9"
    "fN1f4i9xNfAv17n23183V/iL3E18C/XufbfXzdX+IvcTXwL9e4uVbPfzdWzqLHcTXwL9e51NP+NCnX+Ty+p7l9T3L6nuX1Pc"
    "vqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5"
    "fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y"
    "+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l"
    "9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L6nuX1Pcvqe5fU9y+p7l9T3L"
    "6nuX1Pcvqe4/qEj1Sgy260vcTXwL9e4o6g1dDDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNy"
    "Ybkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDc"
    "mG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3"
    "JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3JhuTDcmG5MNyYbkw3I3p2pr3U18C/XuVaiU0kmpVLmpsXNTYuamxc1Ni5qbFxU"
    "2LipsXL9i5fsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qb"
    "FzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2"
    "LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2Lmps"
    "XNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTYuamxc1Ni5qbFzU2LmpsXNTY"
    "uamx/qUilVvO4mvgX69yqt5Uga1GJCd6Se2PMSoipCnwancTXwL9e59t9e/BBYUjzJX+IvcTXwL9e59t9e7IymrhnTJ8y6ah"
    "DSw0d07VH0FaL5hr/EXuJr4F+vc+2+vdpUrSkIzQtFBjXajemapU6LPIqdK5iSKs5KVaPzQ08wV/iL3E18C/XufbfXuU2WlE"
    "hiEq5R6KiFN9RNFMXXYUvSLphRzrdIqZKNWdStT+aeYK/wARe4mvgX69z7b69upSZZbI9SkySrsM9XMcrVGIlsV/+kPSVns9"
    "5B7YXy/X+IvcTXwL9e59t9e1qZmjDVSmmQ9MxUyERSmkOHVsoFSUkdqMK6Z+X6/xF7ia+Bfr3Ptvr209R+TRupS0KuoiZCpA"
    "g2mrlHNstHDFzOo8v1/iL3E18C/XufbfXtpe8VM2jMhj4Qc6S2g5ZEGPslWsikjUzK6+X6/xF7ia+Bfr3Ptvr2pko1bTSYLa"
    "iqqjVFeNqQLVLwa4pFZc/L9f4i9xNfAv17n2317lJ/yHsnQb0nqjOklBelsqJ01owmUidNbG9JKwO6eFF9RIHLK+X6/xF7ia"
    "+Bfr3Ptvr3GuhSm9FFrwYmBeokb1NkxUpAytYG185H1PmVXyvgqlqm6JL+puJ1LvmhSq3ny8l1/iL3E18C/XufbfXutdCjXo"
    "4sIo6mXaiUxGiugfUJ8C56M1K1W87GtVywg1EakJ34I8hIV/iL3E18C/XufbfXvIqoNrKglYvkFrC1xX2uyPbue1mo/qFX3R"
    "GPqDOnRPeKrLDoKFOylpfYz5Dr/EXuJr4F+vc+2+vtI9u6o1mo/qFX3RtN1TMZQa3XPtexHxPk2v8Re4mvgX69z7b6+JfVaz"
    "UfXc7TIZRc8ZQa3XPylX+IvcTXwL9e59t9e9BBHt31msHV3OGUXPGUWs8mwR3q/xF7ia+Bfr3Ptvr4V9ZrB9ZzxnTudrkNos"
    "Z5Wr/EXuJr4F+vcqepVkRUckp4J/UNbpmOqvqZDenc7XIZSazTyw7/VqZdxNfAv17laleJlqI99FYMS8xLzEvMS8xLzEvMS8"
    "xLzEvMS8xLzEvMS8xLzEvMS8fVc/XsZVVmiGJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl"
    "5iXmJeYl5iXmJeYl5iXmKeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl"
    "5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeYl5iXmJeOqvq5FGjYzd3E18C/X"
    "uqiLqXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsWGbD6lJMmoLL1GdMq+8JRY1NC7ZsXbNi7ZsXbNi7ZsXbNi7Zs"
    "XbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7"
    "ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbN"
    "i7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsXbNi7ZsX"
    "bNhERNO6mvgX6+AVUTNR/UonuCvfUyUZ0yr741jWe75iTXwL9fbK5E1H9Sie4S+qu4zpvm8a1GpCeZE18C/X2tXqFatlqEPq"
    "ruM6b8Q1qNSE8zJr4F+vtXUGudaURETTzQmvgX6+dk18C/XzsmvgXedm+BsLJYLBYLBYLBYLBYLBYLBYUsKWFLBYLBYLBYLB"
    "YLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLClhSwWCwWCwWCwWFLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBY"
    "LBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLBYLKz/ALH587T/ACXV0FpVM9zPcz3M9zPcz3M9zPcz3M9zPcz3"
    "M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPc"
    "z3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9z"
    "Pcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3M9zPcz3JVBHT7Zyx56avy9qua+etM/app57TT2iadrskEtqkyS"
    "5HQvtnOskOXVfYKsZiWnZieXU09omna/3Rnui++g71nWRyWM0HLMIgjETMfm5EGJDlJRy+sMXOOxtn5jfdUa+EKiy0f7yDm2"
    "UlBzlhBWQhMsEZKZjFyzFs/IasoVPdLEoNd6ojLSSo3OWqWUtQOhEgTJ2XlhNPaJp2uzQakIKi25HNWbSCo52o5s5oJbGos2"
    "lERbSqWVauQ2fn2NRzfkI1URRLSJECsWzA5s6Co52SjmykHrrkOSGQJajIserBDogakIKkpB66JAjIbAltuQ1sakLbkcizKE"
    "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"
    "U94QX1SMln8c5mzX1wt1xVpxZqT+8T+4UIUtSkI4qSbk97+Jj/6J0/8AQju/+hHP/wD1SWUJK1HkAg2ghv5StkCyEOfJVtiy"
    "tJQochH/AKBoaRxlGkAJFV8rnKd0hQovkc5RC2l8ZJpuyC987430grpYs2cO73YEnn978Aqt2bUTEvat5pxSLVKVoemyOVd8"
    "298qpmrFLNx5a9yJeTzmazyrNulaRMSWdz2aIFuzSt1elOGUeJyu2vR1rAC0dz/5iXDz5VlhauGwhYKW093u4fwqenPPEfyA"
    "+v8ADcq3RLVaFxUOs2rWbUU159xl4D+Qn1fju5IMs4w3mWeFnlEYhPMO5Am5h6WW3YKaNKJPqjKBykXd7B500Z4yjawiYlZK"
    "UfkJtCLaFqcKq/X3odyblJnNzLlzM0FkUVzUrSP8RIyjL25mTatMrtEDiqNRz4CN+5YYz87M6PKlZTTumkPvT7FpzfGaQu2o"
    "UuFOXuw3kpwXh6wvvDE+KMnPZOYzSX7RJtqVUXUxMSElkyXsl9pJsWiaqqeeG3GU5ycQ4ltx+2bzy3Ybj2UlLbl5Vs2Qp0kZ"
    "w8ybr4lpmZW02qYvRL1OcpzkUjI8vLLabWiiyXSQKUPc7sSk26/KqbaVUhC1V+7E0JxSkSttOcKMaWBH6PakZmWdWDm3lO3m"
    "nhpEzk1TllqWUrOujmBpd34clm5WaWQCkTYWaV58fwiXkHcmOKm7BK3i6oAkd4xL5Ok8muMOpnktOrU6ohSbVCMYKZiXeftp"
    "BTLMrNEDnJJrEjlLJNpUpNkJDajWijhDDWVW3sozzibakNqspT9YhvLWR1LEqTZcZcxRyeuMgPSrFmYm26uEEm2aJ2xMuvIz"
    "mVGkBa1WzwCThTCJSbYlivKTz+bCgo38IjDCGZHNiayyoWnXraqN+CsZAelWLMxNt1cIJNs0TtiZdeRnMqNIC1qtngEnCmEJ"
    "yk2409LWrLlgm018oUhhbim3mH02m32TVCv4PPTnUdkF1+r2bkqi0bFutnkib+dVuNI7Irr9Xt3cg/Mq9SIT80uMvLeZbmZh"
    "la1MsO8VRtH2eOHBlJhiRYsHNsNoCSo1F/eH4w848c1IMLKnncPBH+I3pcAMSrFGSoVKuCu8wjLEnXf0umxMS1fVGUHmzRxu"
    "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"
    "v5J2x1KX8k7Y6lL+SdsJaoXOUNNDgjkr/wDMKLlhcyvFaeQc0ZSCb0JlnCVd2wd09OYlUS8mptlsNi0hVSAKdtBzksypulyG"
    "iU08dY0HW+7Gg673Y0HXe7Gg673Y0HXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0D"
    "Xe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73"
    "Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gg"
    "a73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe"
    "7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0DXe7Gga73Y0"
    "DXe7Gga73YK3MjtOLOKlKBP3I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5"
    "I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5I6xseNP5"
    "I6xy/jT+SOscv40/kjrHL+NP5I6xy/jT+SAhvJobQMEpdAH3YTmZVtsdlnFFVfFSHpVTEqlp1stmylVaEU7bdP8A7HcIwjCM"
    "IwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMI"
    "wjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjD/3U"
    "WUJK1HkAg2ghv5StkCyEOfJVtiytJQochH/oGhpHGUaQAkVXyucp3SFCi+RzlELaXxkmm7IL3zvjfSCulizZw7vdgSef3vwC"
    "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"
    "/wCza9XT6dMvim4ByxTpr57nx5k9zpdOgpFN3GMYxjH4sz4fUd09OyX/ALNr1fHL9y/pznx5HSx0F8D44z4fUd09OlWR1dlF"
    "UJHZjl8MEEUI5D8aoYMVgHlivTVfHk9LrF8HuRWLXLFfjk6pyqX3mVoS2R2PKd09ObShLfAAAN9fXH67IsTP8yKtrr346z/a"
    "TsjrP9pOyOs/2k7I6z/aTsjrP9pOyOs/2k7I6z/aTsjrP9pOyOs32o7I6zfajsjrN9qOyOs/2o7I6zfajsjrP9pOyOs/2k7I"
    "6z/aTsjrP9pOyOs/2k7I6z/aTsjrP9pOyOs/2k7I6z/aTsjrP9pOyOs/2k7I6z/aTsiv6Gv/ANydkdZ/tJ2RQZH+0nZF+R/t"
    "J2R1o+0nZHWj7SdkdaPtJ2R1o+0nZHWj7SdkdaPtJ2R1o+0nZHWj7SdkdaPtJ2R1o+0nZHWn7SdkdaftJ2R1p+0nZBaeyNbQ"
    "eTfRH4R+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2"
    "uR+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2uR+z321yP2e+2uR+z3"
    "21yA0zkawgcm+lH8I60faTsjrR9pOyOtH2k7I60faTsjrR9pOyOtH2k7I60faTsjrR9pOyOs/wBpOyOs/wBpOyOs/wBpOyKj"
    "I1/+6OyOs/2k7IoMjfaTsihyN9qOyLsj/aTsjrP9pOyOs/2k7I6z/aTsjrP9pOyOs/2k7I6z/aTsjrP9pOyOs/2k7I6z/aTs"
    "jrP9pOyOs/2k7I6zfalbI6zfajsjrN9qOyOs32o7I6z/AGo7I6zfajsjrP8AajsjrP8AaTsjrP8AaTsjrP8AaTsjrP8AaTsj"
    "rP8AaTsjrP8AaTsjrP8AaTsjrP8AaTsgbzycywrlU6ouHuU5odzobJcFCaHdP/sdwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwj"
    "CMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjC"
    "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"
    "9OkPmEfdHQD4lfGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGE"
    "YRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRhGEYRd0lW6enSHzCPujoB061MPIaFCRaOPe540r"
    "Vq2RpWrVsjStWrZGlatWyNK1atkaVq1bI0rVq2RpWrVsjStWrZGlatWyNK1atkaVq1bI0rVq2RpWrVsjStWrZGlatWyNK1at"
    "kaVq1bI0rVq2RpWrVsjStWrZGlatWyNK1atkaVq1bI0rVq2RpWrVsjStWrZGlatWyNK1atkaVq1bI0rVq2RpWrVsjStWrZGl"
    "atWyNK1atkaVq1bI0rVq2RpWrVsjStWrZGlatWyNK1atkaVq1bI0rVq2RpWrVsjStWrZGlatWyNK1atkaVq1bI0rVq2RpWrV"
    "sjStWrZGlatWyNK1atkaVq1bI0rVq2RpWrVsjStWrZFGJlC1VpZNyj4D+6lbp6dIfMI+6OgHTfgz+su3N3VpzmHHlLur8I+v"
    "1CNP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U"
    "+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U"
    "+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U+2NP1PtjT9T7Y0/U"
    "+2NP1PtjT9T7Y0/U+2C+24JlpI4VBRQ8HNG85twuOEfBKI5heCf+8v7pVunp0h8wj7o6AdFVaqRwE1jqcdTEdTEdTEcJo+CC"
    "scRKAlPr/GG2GhRDYoP326WAKNOJcSk4ciqd790q3T06Q+YR90dAOhUo8kFSjcMNxZT2Ir0K/B6oSvn/AH3Nf0/dH7pVunp0"
    "h8wj7o6AdCe7uUtpR8qCm1bKsTClJeTTmPQLgjm/fc1/T90fulW6enSHzCPujpI7+4t4cVGMPJUvkqKxZFVLUeWM25xt1cOd"
    "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"
    "yNI9BWyNI9BWyNI9BWyCiRqt44OEXJ8fLG/HqqQyq0SqvDX3/rjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwjCMIwj"
    "CMOmK3T06Q+YR90dIdl0LbaDZuKE1qDhWsBQS9Mni21GoHcqYq++hk8yRa8cVzZfVzumv1YQENoS2gYJSKDolOcJl1VSVIwJ"
    "7ojTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1"
    "XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1"
    "XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1"
    "XtjTdV7Y03Ve2NN1Xtir7i5g83FEBKQEpFwA5P3SrdPTpD5hH3R0gPrZQt0UAUoVpTm/i5W6enSHzCPujpKl0rSDZknF304M"
    "VMi6B3YQ+E2bXJu4VjDcwv3MDu4bt27hGB6HMS7BmXuUJhKTI5po4k7mEc8YbmHQ4bmEXCMNzDo0OSot04yYbTLsKabHVCsR"
    "+MKdcNEJg70yetbfIo8sFU2zmlcgjux+O5gdzDcwPQ4RhSMN3CkYdDhH4Rf0H47l4+Kq3T06Q+YR90dJd+TEx8qF3Q27iQLh"
    "G+xOZqt6W+eFy8yBn2oEpLTGZuiXZdnM8FmGCw5myTjzwxKSRtzKwLSxyQbbhcdSm9Rh91E9m0JOBiZD7hcsXVicDzlppGAJ"
    "h0sTG9ZdGFOWN4zys5XirhUupw5rtYlQ04UBSr6RKZpwotEWu7CEtXzDgupDlXvhq3LPJDLCp+0XDSsOvTczviguEGaTOpYT"
    "2KKwuXmL32+yhZ5QIeTJJAdJvdVCZbKFFhWCkwhDAq85gY33vzOcpahDktMCWWOPDkoJ+ik9lFlv9ame2gzS55KVJvzUJcXx"
    "t2b3y9aaRgIVMrcLUr2KYEjJHN864bW9Mb6ZJvrCZiUm8y3ZrSHAmfsWDywzJMEKmFYuQmaVNZ9PZJgzLRzayIqiYzKR2XPC"
    "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"
    "sjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQV"
    "sjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQV"
    "sjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQV"
    "sjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQV"
    "sjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsjSfQVsj4C1Mr5gLI+uKOuWW/9NFyYrNTYl0cwQSo/VFGHAFcqyhRU"
    "fDSNJ9BWyNJ9BWyNJ9BWyNJ9BWyNJ9BWyNJ9BWyNJ9BWyNJ9BWyNJ9BWyNJ9BWyKMzCFKws4HxHoVbp6dIfMI+6OgQywqzMr"
    "4VbNaJgvqXm2ib3F3lR/GNN1XtjTdV7Y03Ve2NN1XtjTNV7Y0zVe2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y"
    "03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y"
    "03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y"
    "03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y"
    "03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y03Ve2NN1XtjTdV7Y"
    "03Ve2NN1XtjTdV7Y03Ve2CpU+EpAqSW8PriwxN76UMSG6J8dYCGkKcWcEpFTFqYdTLnkSBbMabqvbGm6r2xpuq9sabqvbGm6"
    "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"
    "b/B10X7sz/T90dArdPTpD5hH3R0H/M/v6K/4ipNrPvD/AC2/xPJBShQlW6/5XG8eyEqs5hk/5jn4DlhK7OffH+Y5yd4cn8KT"
    "P9P3R0Ct09OkPmEfdHQf8z+/4qU2s88P8tv8TBTazLJ/y2/xMWnf1VvnWOF4oFhkOLxzjl59n8LTP9P3R0Ct09OkPmEfdHQL"
    "cIKwHQ8OSt9fZCXWlBbarwR8SstfrTnMg8HxxYUo2VXBlrA7YBepLN41VefFFWm7Tn+ou9X8LlSiEpF5J5IVmq2XXAkECt2F"
    "fx6BW6enSHzCPujoEluwiYRgtXKOaFNULfKWnBceSv8A8R1KX8k7Y6lL+SdsdSl/JO2OpS/knbHUmPJO2OpS/knbHUpfyTtj"
    "qUv5J2x1KX8k7Y6mx5J2x1KX8k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqUv5J2xR1yy3/pouTuUYlZYK5VlJKj4ax1KX8k7"
    "Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqTHknbHUpfyTtjqUv5J2x1KX8k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqUv5J2x1K"
    "X8k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqUv5J2x1KX8k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqUv5J2x1KX8k7Y6lL+S"
    "dsdSl/JO2OpS/knbHUmPJO2OpS/knbHUpfyTtjqTHknbHUpfyTtjqUv5J2x1KX8k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjq"
    "Uv5J2x1KX8k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqUv5J2x1KX8k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqUv5J2x1KX8"
    "k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqUv5J2x1KX8k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqUv5J2x1KX8k7Y6lL+Sds"
    "dSl/JO2OpS/knbHUpfyTtjqUv5J2x1KX8k7Y6lL+SdsdSl/JO2OpS/knbHUpfyTtjqUv5J2x1KX8k7Y6lL+SdsdSl/JO2OpS"
    "/knbHUpfyTtjqUv5J2x1KX8k7Y6lL+SdsdSl/JO2OpS/knbAYpwT/lMp40KfmAgvnigX2OfoFbp6dIfMI+6OhKHEJcQcUqFR"
    "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"
    "YNmHP0kFLmq4KrDthBabpckxvlxJzLg40NyknVxSjeaRKNAqDQ49mKSrJcfpjfCO/EvzxMd6FNTFFOGvY3xPuWChlXFBh7vm"
    "HN+0ztbsYnRL2t6gcCsPfpMLW/W5KqwrNN5tvkEOwzFt/id0QyqQQeDisCkd2JZSuKDFkKLirNLNmJx9tBbCuKIc/SAU5M1w"
    "VWHbDdhHIiFuPKspEMvoWc2nlpErmF2qGJNQvIiYmn+rOJjwxQ3iC4HHGrWITDrSK0GFYe/StqxXgjkiX3mzm2q488O/Jh75"
    "USynOKIYak27T3OExk5K+MKQ2pxKl2uaEuDBQ5f30rdPTsnJ5FSzRHkDpVXGUrV3RAShIQnmiy4hKx/NHwTSUDuQlDCLagrk"
    "hkPMJUtI5RfFAAkdyM4WElXPSLYbSF88WXEhae7HwTCWz3BFFAKB5DBCWEJrjdFEJCBzCLZbBX20UUmo7sXS7fiixZFntYsJ"
    "SEo7URozfiiiUhA5hFtTKFK56RbDaQrniytNpPMYohNhHcirjKVq5zHAbSjvDcGcbDnfi6XbB70XCg5otlhBXz0jOJbAXziL"
    "SmEqPOY0ZvxQCJdAp3IFtsLIwrFkiqeaKISEjubpWGwFdtHwrKVnuwDmk1Th3IoRdFEICO9HwjaV07aKoaQk96KqbCyOeKOI"
    "C+/FAKDkH76nFNGy6hhbgNMKA7p6dJyrjE2VssoQSlKcQKXcKLK880kDjuox8VY0n0F/ljSfQX+WNJ9Bf5Y0n0F/ljSfQX+W"
    "NJ9Bf5Y0n0F/ljSfQX+WNJ9Bf5Y0n0F/ljSfQX+WNJ9Bf5Y0n0F/ljSfQX+WNJ9Bf5Y0n0F/ljSdWv8ALGk6tf5Y0n0F/ljS"
    "fQX+WNJ9Bf5Y0n0F/ljSfQX+WNJ9Bf5Y0n0F/ljSfQX+WNJ9Bf5Y0n0F/ljSfQX+WNJ1a/yxpPoL/LGk+gv8saT6C/yxpPoL"
    "/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yx"
    "pPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6"
    "C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8"
    "saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk"
    "+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL"
    "/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LGk+gv8saT6C/yxpPoL/LCc2XH645tGHlUiclkMzedeZW2FLSnEinbb"
    "p/8ARPufx1Tk6fzRjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYx"
    "jGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxj"
    "GMYxjGMYxjGMYxjGMY/+jClVCGk4rMcFsvL7Zdw8UaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGj"
    "S/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGj"
    "S/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGj"
    "S/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGj"
    "S/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGj"
    "S/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGjS/kRo0v5EaNL+RGj"
    "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"
    "fZH2R9kfZH2R9kfZH2R9kbZH2R9kfZH2R9kfZBinRiKlyQIvE3FkpQADkh64nJnJnInIj7I+yNsjbI2yNtG2RtkbZG2R9kbZ"
    "G2RtkfZMHDbCTWXiigfi7JAZwkCkKJNNU2Z8T5HH3H1nIWQ+sI6QACCwbEcT6FDFFFF61wQ4LrOSdIsRYixOSckWIsRYixFi"
    "LEWIsRYixFiLEWIsRYixFiLEWIsRYixFiLEWIsRYixFiLEWIsRYixFiLEWIsRYixFiLEWIsRYixFiLEWIsRYixFiLEWIsQd4"
    "Eg2A1BEEwJ0VijcFlpVOBom54LsD2+6JkyZMmTJkyZMmTJkyZMmTpkyZMnzpiIoaNss/ZUZtBvoGowiT3sPYQ6QD4cADj7j6"
    "l+BIhOaAA9yEIVMGE3Z56a3nnGfOE+MJ8YT5QnyBPgCfAE+YJ8wT5gnxBPiCfEE+IJ8QT4gnxBPiCfEE+IJ8wT5gnzBPiCfA"
    "M5nbOZ2zmds5nbOZ2zmds5HbOR2zkds5HbOR2zkds5HbOR2zmds5nbOZ2zmds5nbOZ2zmds5nbOZ2zmds5nbOZ2zmds5nbOZ"
    "2zmds5nbOZ2zmds5nbOZ2zmds5nbOZ2zmds5nbOR2zmds5nbOZ2zmds5nbOZ2zmds5nbOZ2zmds5nbOZ2zmds5nbOZ2zmds5"
    "nbOZ2zmds5nbOZ2zd7wc6n7oMGDBgwYMGDBgwYMGDBgwYMGDAAAAAt9oIdBKmINPxt13id0vgKJbq8aud9RJuP70/P1apBBT"
    "+8AFkqcm+sQmCoBcOeLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueL"
    "ni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54u"
    "eLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni5"
    "4uFNgoU6l/8Ai15oS5zOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mc"
    "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"
    "n0gDhUzHGL6Imx4ixCrqLIctePuPqX4BYKR2LHKEKlXCNDFsKiW230Tz/wD/APfffqvgQVd3RVpTi7ogH6n/AN4488+66++4"
    "EqHIhBunpwic0C1TgVvq+/8Anxwo44g0s86oJfivvlrWta1rWta1rWta1rWta1rWsbTxr8x9QDijGf8Av3j7r0Hd0TQRrvOc"
    "IAzBTAGkS4kfT688449//wD++TCcAFiATlmESF8CyN5KFh9Imn//AP8A/wDzzxM04COp0wrvptHQIqyoW/GrnfUSbj+9Pz9W"
    "qQQU/vABZKnJvrEJgqAXDni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54u"
    "eLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni5"
    "4ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLni54ueLn"
    "i54ueLhTYKFOpf8A4teaEucziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mc"
    "TOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnE"
    "ziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mcTOJnEziZxM4mW5QhGtPrEEB"
    "BZnf+8ABGEQfq13U8RW0h4npLAW1Z+6XAt6S9kNbyFiOn90b7i31QEFwHQ0mMnED3XWH+AxxD3XSH+gxB0JpnIxxpb56BsFK"
    "ZJEJkAZg5AQHzIh/okEiDsfoY7AGQ0Gna0oc6q51psZv6aK6TYlr0quQ/wBhFCz9bo631BcAODXQtBVDSsPO1eD4R34dXW+o"
    "LgIciw8gfmaSVDIiCmALmYFcCKwoge75gQWSMIlCoF0+sKN0CkgFVxdPBKacKtAGkSyOgs1oCPQMWqdIrmSZwqwNxIUFNcmq"
    "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"
    "EGpAsVWlKDIF1rmYBlC3ixrLWo3LpQpQBAEgqPAlAAHYEZRVrl0fCpkoyhRdNHeeQ2Q/91mXEK0IPVVtHNo1CEyVCw69Y2yc"
    "ItEMSAF84yDaLcBQNC2NzA86p80blXpf2E08Llw6s0LpBHVkaOqtUcYRkOEgoAbASOyMYmKyhqRzFhAqSNVsIghvqCNIgodm"
    "lktbkltRn4uBsrAXg2J1jIBUSqm6rzEW8WNZa1G4GeD6zGwuisHAFx9QC4pb4LwZ5q9DAUvNJy8cqWzW05jmlMaaKAca291k"
    "KoJAuTEKGQAtii1ahFIP7YSgBZHstrSW4TYwhIaA91E6hu0QASS5Q0oAIUrgY0GA7UCsMm81jDILk2RQEt60mrjB0FAL9P6H"
    "7j6xB+wuhiSSO5gEeL1deLUqGB6VefPnyZYetgfqRG5ofqhEfB7Td8nE8R/EBfB9ovQGf0xh2R+n/YTBjz58+fPnz58+fPnz"
    "58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz582Tg"
    "cVatV5yzaA0iAyBBAN+PuPrCCNao0vcdD6sbS8IkRREiRZQZTgP6/e9BiLclXqxXj7j7FPofXlFF6VF/YL3o+J88fcfYp9D9"
    "HL6RIGszDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO"
    "8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw"
    "7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8zDvMw7zMO8z"
    "DvMw7xDY+m96PifPH3H2KfQ/Z6i1f1atJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJVpG0em96PifPH3H2KfQ/Rz4"
    "OPi6lABgpdL8g/mmqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqgo4kBqQMpBNNf4a96PifPH3H"
    "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"
    "9TvMQE4H0fE+ePuPsk44Orlpd9EoK0KgQJTA1PQQ3xIBFQsukLhIgDvrdwdDEQO6oKCNMGi1tCgCBMbYVNquUCE7Ff1f/QD0"
    "4ZDoVHYV/Z/qt4NxGW+B9HxPnj7j7JOPQamyWE+ho4IyJpoHVQoggCO2FWtSuUavAWtmmrZVzWoKKk78jaU7C4g0CargFh4g"
    "XOw2CLlZRGxVc/zeTvSOkNXRb/wF7MpIpPnifR8T54+4+2TvcUliphA6mMQ9t7qygRyM1gYjbBoNqEGXqwHIUSkL3WuY4HRw"
    "5uAYHuLQCJugRq1CBVal00QjSqzXMzzcNurYS646ieRNaQ1ELVgbtw7i1aL+Zmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm"
    "mmmmmmmmmmmmmmmmmmmmmmQKtTGdVHoVekfq7YXB0FF03nm55uebnm55uebnm55uebnm55uebnm55uebnm55uebnm55uebnm"
    "55uebnm55uebnm+EQ2mpqcy+D9fxPnj7j7ZPdglMIDc1R0WssM0OQrohez1zH0AAh2NxUBTDgwYgkjZUrC6iWawEDoPUK9WD"
    "Uak/CbP81/8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8AjA4Jprhaqubw"
    "f8QDQDYfxPxPnj7j7ZODATNOJFVqk29PtwzLS8dPb1X4e0U0e3DPo0fDrOctw1Xq9/R+PVpPHOnDpw+TpDSdZe00cNOGs+eG"
    "sNOPtz4bjaaOEqaqCvDRzVTVbcHw/wBluOmJrn7f4nzx9x9unEYQLW8sC2GqMg3c0Q6IRuMdVHtMKAlQgmZ2gI2vACWFCJrV"
    "itg4hKCxmF1pASzxEXpiMrEDeAG4HtAZs8mJ0V4iKKOVPwTUDvH2JlWzcaQB210h0N+QnNPCiAYgHr5wMwA3xMw9IQQbSncY"
    "q54KEKptAWhGC/6EIBUhGCoZWvXKUJriEX/EDP8AyAVoC8iG+rzEdjzVJp+4jVVRNcbVUiaC5kfKIsa0rCa0ZzGSLIVTSoRe"
    "EA4FgHP8Cc9ZmKI6CmIhPYiRRfXg5IHtFXWEJvZxCWpzyuwVcTJwEaAOEEbEBBcoiTu9p8mPoD2nSs1idLBQgIojBYNBGFw5"
    "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"
    "t/Lg/wDdzYS/7hoBTjUEnuQhTY6GowZ5iynUAAfedpTU8NYwSpRsAwEm+ogGZpzlYJOpRzOatjuZ0gFttE3UH5jI5WjrZI9o"
    "P5c6te0OojcVzBD2gvDEpAoN/iDkTXWFpHNRICWE+06ObQMGf9wte05Xod1ID5nIf3bLZ94UgemfqSR7R+Z6QygGrpUk/IV9"
    "4aZdqLz+HNWrVq1atWrVq1atWrVq1atWrVq1atWrVq1atWrVq1atWq85ZtAaRAZAggG/H3H1hBGtUaXv4e2jlQraIHEvU32E"
    "ACtK8UP+wDYrnwCBiYqAYQ7o89IsDgVTsZbIiAtScxPEJqxbHAU8tNN0QN0szrSWLuYtr/MVqRAjO85jlEDd95vEHWc6m1Yq"
    "bCWedYkEGKRX1/iDEW5KvVivH3H9NT6fy/xPnj7j/wAET/E+ePuP/BE/xPnj7j+8p2aj7ZfahdVGROqlr840V9YJAgpzfuDA"
    "8H6PxPnj7j+7p9b+DGjXAKhHXE1v4MTFchoUdMS8Rxoo0d75rnNL+KGjTAKhjXP0vifPH3H91T1PIMIGt7BbnW01v4MTFcho"
    "UdMREDqpe3ONVfWI1H3y21CqL6nxPnj7j+5p2ROilr8p0dtIKvgACCUor1AXdbTS7BYpg/oSDeI1H3y21CqL63xPnj7j7NOc"
    "EQAYxglMBU9v699999999999999999999999999999999999999999999999999999999999999999999999999999999999"
    "99999999998AlhYwFrkOdgbRm4+2X3qHVxFRNbbLJBrXFpQpSs8X5AKFMfT++++++++++HCkSBl6Dogn0/E+ePuPsk9PkCNF"
    "bejJC11xKrd1VeZAyuSe6Pp//wDFg8wT+q0KUpSlKUpSlKUpSlKUpSlKUpSlKUpSlKUpSlKUpSlKUpSlKUpSlKUpSlKUpSlK"
    "UpSlKUpSlhzk8ALkmSAkxIq7THYWCreXgruTWwhwQ+qF0QBpYm+n0EpSlKUkMZuPT3//AA6UfMs3UFKvog4ZgtUqKFRyDZyz"
    "b0fE+ePuPsk/IeBKA1XVCD/iAaAbDha8OdpeKwALDioooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "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"
    "OLHFjixxY4scWOLHFjixxY4scWOLHFjixxY4scWOLHFjixxY4scWOLHFjixxY4scWOLHFjixxY4scWOLHFjixxY4scWOLHFj"
    "ixxY4scWOLHFjixxY4scWOLHFjixxY4scWOLHFjixxY4scWOLHFjixxY4scWOLHFjixxY4scWOLHFjixxZgt9FmZAhgTrAWo"
    "BJzQIAjmAaACwA9HxPnj7j7FPoZT3zRLceQ5ajeX6WFFkHQoaqXP9sqqqq6666q666666666666666666666666666666666"
    "666666666666666666666666666666666666666666666666666666666666qqqqqPAmkQcBFbmgsa0gOwE1A6gvfTRitfR8"
    "T54+4+xT6GJnWKgTa2jl1lzMSXNE1P0x/Yd3gRPN4ZIMWhAWqhZc/R8T54+4+xT6H6IdAOZRV+3nsJQILf2Yv4nzx9x9in0P"
    "pC44cZMexNHhCAoIjoqCJvzMC5WNRbA0Lc5QLBptQxACCAlqI4OJoOHJxAv7QGOPtB6TgYDXhckgDMrgAyfrJhMfpfWOOn8W"
    "X8T54+4+xT6H0hIlHDL5JhPaMv2IXEwbDSBjlpSOG3zTEqgBoNIrrdUSJIcuoha9WKnnm8uUcHE2ltvid+Xo34EV4k6x2heR"
    "lHB8HHwcfB8XHwcfF7BvDQFZYpRUaBhaOOOOO+CsF5QIOiOdTSBA5PTu3AeDjj4IgVaQSDM5GsKC7aEwNP2ficAefaOXOkpW"
    "DMmzYmv8WX8T54+4+xT6HiExwxophOTz5MQsEr3gaXk34MHYqoh8ptkf9+BSoGhCMaZ8Vgwo8FbIY1QvF4oy/TFhIzg2j4iV"
    "x3hQiDYZaQtLcktp2zMkKViqBQBhQQ9W1tiOSRNBZcHahsgDcKHpJGRwhAGKplOsJnwsgeUvJEVkYK30rVGZokQbDBhEvzkd"
    "BLoFMdIHtANQhp4RBBwpmuuZw90oEgFWw4SyqMRrAzQjJwZNBkgqi0kQkIKIOVgQorQbIAJOUZQlIVEQGu7GxEnZpIEiDUAg"
    "QOlZiHSAYEyIBWWciYo4PmCfvBvw7BIrfnzkIYV10LEoyh5qUaH6oAa/tJw5aktKyoIRQgoKkF8wgCDnaaoFB0VrCB0EjQTz"
    "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"
    "WWWItesfy9bSl7IUyDqpXj7j6iI/3pgdpf6l8i/95YQ/WGzG0IvHgE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8A"
    "ngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8An"
    "gE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8Ang"
    "E8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngE8AngEDtWHosP/ABblxLRgbnEGwHFvqclxg/3bOc5znOc5"
    "znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOc5znOckVo2y7F"
    "QSvm6FEsNPN/rBTwqvfDJg8SAHoQ/wB4R0DsRsYCxIAeaPz9Uh5NSVzZ2FP70YYha1gqD0+r5mH968tvPN7vqeZhxunKZDCO"
    "0KieoCjhpbAWQXocW+s7WFg2Hv2iiQKGOd3x9BD1OJsNz0EpcAWWyh/ccIcawO39c8tvPN7vqeZhx8PBntHw4Ro45x1F+5Xt"
    "CbyF+4/Ol0Dr8SmC9oVA0PRRLjYBhJO6C6gINo5DvHHaoZlAWh6AkGnI9+AwEO9CrJ2VoIRpumDvzgyoJEUgNEbOxi4OuICS"
    "3VhBUOKmKFANEATTtALWoVLynvQQfaqb4oc4ClmAGl3cBxMLWLvhBwBEmuhpliDF3sQEOgBsytqYNmAK94VLiOZL8zniuWA7"
    "lYLcKKbQ6NmB7w6EQTLauWqk+4NdTA3vVpUWjb3hwyh6n8QoFePIB12t/V/Lbzze76nmYcbJymSgjtFIrKyHQaGXeRyTJLVL"
    "MOQy3+aymAIbdQMdtJfz1wBPm0q2oIAKMaB3T2vw0DywjAgtVh1IUqUTgB0BiGtlGCok79+Bxl1wurdswevRBFQq9vdBwmF1"
    "VGzEPODZKoqAOZqcRAL19zBxYQPA+5IHed+s2LY/UIqettNFSBbnOj+VBAQShwiUDcECj1iVnPICDzV9hGfVhRjcqMrQDEfg"
    "Y8HQLGPram8oR2hRU07JVKvOMzIDb9+bMpUMSLMEAiF+WDEDPaBj2qqrpRML87ZSnhmElIDoYggAKBS+5/q/lt55vd9TzMPt"
    "/dDsyF0d5zYEKjZ/2Ly2883u+p5mH968tvPN7vqeZhxNIE7Sa6D6oD60C8gIYA8/ypBAJKADWF6tHiG/Fqz+geW3nm931PMw"
    "42gVPNJVUqW6gzUQAqmYGFhv1mdALQq66wRDTyIdWASU7HKHnSlE1C1KWHO8aWFKAjebhIeYOAwAO0RphJqKrPHXrBg3arpW"
    "uDMR5QQIMlhbDVKo0dAEUBPsVs4GjEasYFREopURC3761W0qOglqBbLHBSGGHTDZJrZIbEqqIhjaGRWfECzFjBioKhWsFgR9"
    "TEV1BZckJJvcyYPvLWUpCtCtzgFQRT/ju1hmzVszjG1K/tTqQDRS6wr0NFAgoqNcHQ5Q0xiA5G4NtHCiD4ERoIIodYSCD4Bp"
    "YcKozJ4XrZHfDJeg+jlwy627IILXLraagzkDC0KDuUUr2BBoUZi8jhDzpCjA6jSAtDP0z70gkQaEL3UKu/ZInhN0zLl7AGT2"
    "bcASt2TPqrHa6M34mvzrDKptpGH2CyDVGu864hHbo17mSjsFk9KCFgyEmfWHgifIypbSkyoW33oEQQkCA1RYEppunGspHO4q"
    "XpJNBgIeUQbhUCteULKcFJRK5KrgKgroXchSrsXDRRF1ZgxhLnV6sVkCQ4Q70nQaK7b+sGVVcAaAh6XTZE50DBbdUpA8vWJK"
    "/wADoSIBNIwy4EAMHoYCrG5eRuQhqcE3dQBlzihwBtDj7YDLg2KWtVIUNO0KIPgRGggih1lEaB1yA6kT2IZKcFUqAJqyJ1UE"
    "jhJVAV9454u2SumD5xZSFwFKiA3b1lWBZggei4IFupASg0sAs1RqQT1nYNbtiQEI6i0FKuRnESpI0Qq/jvLbzze76nmYcduK"
    "1UAIjYngLqFDIJJVlOUCpFIKqy6qKk2MuJl3OTYMhsEQ7UkYG2AAHQSsq8ZVI1NMwABQADdCTc7wgBQADdQTcbwm9CQAA1Nh"
    "XmY3DOqgVIYBC5EZbCgSKmGhcKqoGuqxpO7oICATc4nuQEBG5e7ImtXAGwVTFa0g3TG6SwtSCa1RBpFBUACtqgWgAVwBIsuL"
    "2NpQQZhliuDObiNYMDbYFRBpFBUACtqgWjCxAwASsjSPKFWjYBprCDJVHNdOjIGpCrJZlZwAVtGWY1YppLCDoFclL1JvAJqA"
    "IVq2gAtF6wE3ZYR5iFwrgAAkAAZMYqVhFQ02iUIqYFwEAhQIuAmq1hUQEtQDJxiwB9wWk5eFthSo8TEw4PIBKORWVVTDxIiC"
    "VK0LdW8dBYRMypbUN0PwwNuTC6BYgFiAAUpArMBpkoUuYLxov5uhtx3g4pCEUCsmtjpBOw4tNwVGK00UKIFSAE6gNb1pKGhC"
    "FAoIERW5JcMvPBEglnAe9woFRwmyqDZooGVrVUgC72KUEGYZYrgxT7igd8XFdoDQ0gYRvVdkLlwl4m6IlUm4lDGiDZAAOgR3"
    "neiKcOURiEJomKZpvaNxwgohCQpUgamoycxLYAUAwgQg4JHSNC5svK+LIZANBVuQKhMfzubFq5tpQYPBVTW1HZAFgKEiJYh0"
    "VEXrATdlhHmIf8ezGt0RK+0GUuooJQAALnvKBwvWLO+iMqtsISATTc1Mrm1w36AARzJgiUhAozcmD6wgOsz8JAkaGmkKicg4"
    "NsEAFgVxGKRTq0oqPdmLuCNApoUFbm/8b5beeb3fU8zD+9eW3nm931PMw/vXlt55vd9TzMONfrmASoDo/ZaYQOz7xrsA6Ug0"
    "ZuICZEexgqgJqVWFdBPJYwVEmqNgSO0JoNA8a0rlDdgKNozkuFR1ECaawEAAaAUJdPxBJQfGNoAks9EAYMrk4AqdQQjAZi1B"
    "gCveAh6RcUAjyMLmsge8E0Bemhhg9YwgkgAiOhgNgocGdADPaa6IICaAU10lnkNxNXcIZn1gLaoCl6hikHN5bLB9DFIoFxWW"
    "3UlRMTBSrgdmwhE22ha5okkqOjAigXFZbdSVExMAJLS8A9TShvAlTRKltVX/AFSz60QivotYsvv87AAgEOuJ3NqU3EHVeHbQ"
    "iDRS3BrjRgoETRaiawdM7nJCryN41sE6tDAAq1MKYUBqEVYoFqn3iy+/zsACAQ64jDqEyRYVrukH4AZFSdRPXaNy1XVXFmFy"
    "gSUaFUoBdTBy9ALTCzVrDBgDibGSoOby2WD6GKQuayB7wTQF6aGPMHewLLANwYsZdhjRArNzBzeWywfQxSCTAx2QGgKB3SFh"
    "GT9CIQwJlMiBYhcbxnCSYADqKVFOeIG5S9gnsRBbgIkRLbJgPtD7jW/4oOtKQSYGOyA0BQO6QsIyfoRC5rIHvBNAXpoYYPWM"
    "IJIAIjoYDYKHBnQAz2muiCAmgFNdIQKrJIWizlSiyWjwQ5tZ8ggTCi614GWjyIsGcnQVgVihjASUGSG3onArw0OYgCDUUI1g"
    "s3dQXIRI1AJgiEos8TdDFNqw6v3XYYBKERUHlAKJ4JIJDRlvAvDEeQ9CBJPIR/IwwFAE6ioMFoB9GhV2Zj6vI6KQoaqpziV1"
    "Rem6lRr/ABvlt55vd9TzMOIELAkFIQIgCUCHAwIDAaKkmSrdsAltJQAC28KilISFkakMjhCysduCSiLCxF0DRVvtG0xUhvMP"
    "baLSKWYCg0oIekBIgIWEBwD/AMhPBoL4AWuAoYYmdcZBLIAQe9tYbAScwBYSdwCCszD7TpEVuTIMBnARHVZHqdDCesVGgNFj"
    "1GI7TwA5NKiCotIEIaGxIwA2vbeGRK25BFVDqViMTK+VDJbbB3MTIz2UDWFzPWHilugYFOZLK2A3gKoUwkaWowNwHEELLTUa"
    "GHilugYFOZLK2A3iX/FVHayKXgH4iF2G+ou2ketjQ6gL2G0FFekpkWAkknaVvaTuVKejUgqS4gjMFLhWNzrASCyCRFSgMGgh"
    "S04caBGyqu7xAUJvGIUCD0o69o4iRJliEwnQajMFFekpkWAkknaHYQgloTFqXTsd4zLqoEklHoDRvCQoRNCwCt+rCOIfUODN"
    "oCtYDn2eMglrNTG8MghZbCqJkZ7KBrC5nrAZwER1WR6nQzT4CpgSEtFuq0rGytqZUAAqAuKA3vEyM9lA1hcz1hv5PnTUClRo"
    "VWERtrgWB5QjKaFqwIzQi0ecQwgyBBQFiVCPwhtUuRQdoPQfQSTQgfAvaB0frtYUQA23WG/k+dNQKVGhVYRG2uBYHlCAzgIj"
    "qsj1OhhPWKjQGix6jEdp4AcmlRBUWkCENDYkYAbXtvB0w8CC4AgXYPmI4tDIiBUCtd6zlvRSkTNdpV3VFWQBIJbFLUhhmwgx"
    "foAqi1o0HX9zUuJcukpYMIZyEF5a4jWGrAVuEEZsGwlqDZCB0nYJBBfeHmBIepVAJTUOxhwwgWkLGOQtEKkSoqo8NQhDCSGQ"
    "ABp7QimLG4AojY2hjQgQagHC1Il2hYGTmfxoBdA0QEysaO3rut/4zy2883u+p5mHFcGCKwsxqoW3LQHI/UEXQLXAADr9Rogm"
    "sVJd0H2Ii6Ba4AAdfWBXKJoLQQ3exPqEXQLXAADr/QPLbzze76nmYf3ry2883u+p5mH968tvPN7vqIC1xuAte396MoMDViv4"
    "jxAAu9T9S6JwAm5/pdl/eqaQomq/Au/1SDpR+oKYAzfnvHmohDAIKEHT+7hziUH4NB5rAIKAVhWA+scgQwRcQ6nWdrsPynj/"
    "AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/"
    "AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/"
    "AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/"
    "AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/"
    "AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9U8f6p4/1Tx/qnj/AFTx/qnj/VPH+qeP9UIIKNAquq/aEBZu"
    "Vk//ACr/AP/aAAwDAQACAAMAAAAQtttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttt"
    "tttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttt"
    "tttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttt"
    "tttttttttttttqu2222222222222222222222222222222222222222222222222222222222IttttttqW22222222222222"
    "2222222222222222222222222222222222222222222220ttttttu2222222222222222222222222222222222222222222"
    "222222222222222229tttttu2222222222222222222222222222222222222222222222222222222222222dttttt22zZ2"
    "222222222222222ze22222222e22Kz2222m2y22222222222222222223tttttu22naauWuayDyuRqaqG2ZStUMIRdm2mLCh"
    "+q2J2r9iG22222222222222222229ttttt22WfHN5HvJT/iIvhdAeknWIgYTIRtzMNuO2D++1gLfW2222222222222222223"
    "tttttu2yzFFBkHB3kkZHWPMi0w43tiI8msqihPJuxpA23gdm22222222222222222229ttttt22OOJC/6rdW2OpONRyWuJxN"
    "pupwmWR1KSqtx3OyWR1W2222222222222222223tttttu2222222222222222222222222x2222222222222222222222222"
    "22222222229ttttt22222222222222222222222222222222222222222222222222222222222223tttttu22kSW22SSyWS"
    "ySW2yGW222myyWSWWySSSSSSW2222222222222222222222229ttttt22wG2kmwEGAikmi2Wgwi2ywEmUigWmG0m0UC02222"
    "222222222222222222223tttttu22W22SWWSWm2Wy222S2y2y2222y2WyW22WWSW2222222222222222222222229ttttt22"
    "22222222y222222222222222222222222222222222222222222222222223tttttu222222222222222222222222222222"
    "2222222222222222222222222222222tttttt22222222222222222222222222222222222222222222222222222222222"
    "22ltttttpu22222222222222222222222222222222222222222222222222222222222btttttttKSSSSSSSSSSSSSSSSSS"
    "SSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSpttttttttttttttttttttttttttttttttttttttttttttttttttttttt"
    "tttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttqJJJJJJJJ"
    "JJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJMNttttttp2222222222222222222222222222222222222"
    "2222222222222222222222zdttttts2hM222222222222222222222222222222222222222222222222222222222xttttt"
    "tWg/E2Ru22222yy2222xm22222222222222222222222222222222222222222ztttttuz93e06j4FcGNfQJcgE/A/M22222"
    "22222222222222222222222222222222229ttttt2nB82ARan7L3IzwuT7iAqEG222222222222222222222222222222222"
    "222223tttttux4POVtxppJu2OK2aOuNNN22222222222222222222222222222222222222229ttttt21ku2GWW222S2S22S"
    "yyWyyyW22222222222222222222222222222222222223tttttu2222SSy22W2wW2y2UW2WWWWW222222222222222222222"
    "22222222222222229ttttt2fF4nvikQuZTegGoEsMlFoINg4XbRJqo/T1D7t7u704ruw90kkgAkkEkkkkCm3tttttu3E6rYs"
    "2EheuamkMklkGDIzf6ZKRIpJNoTMM/7oHKF/pohhk8kgkgEEEgkgm729ttttt2gCpRQpfd0hsq4EAVA0Sm/zVVKl7ZAOr3kj"
    "47GC+y3iOaculEkgkkgkkgkkke3tttttu0kvEjZ6lHxEsUptzX4kmCoNVjqVrW2kzb9PiFJcWd/BhN+LaSggkkggkkkkj29t"
    "tttt2kgr/X0p0rYCDdIC7/lAhl8wrWvC7gBUm88+rNyBqcZEUisGykEEkkkEkkkke3tttttuwAxz8MOnH1jLntITA3DJVBXM"
    "tJiX+0S4vM4WzPq+cZu0zsI+WgkEggEkkCyX29ttttt2AE/1i3PYqaHXtoiT1uhT+tZrobG1bZZBfSoWTRrjUjij2lIQ0gkg"
    "kEkkCy2e3tttttuwEjdPiwR/Oi7xEAnHoosaghhkhOX0BJMbHSxOWGE4CXqa24wAkkkEkkkmW2329ttttt2Agi67fGGDHU+O"
    "gmD+z+0B3/yWUwjeT7R31H0e5tysEnHoiSAEgEAEkEAw22e3tttttuwEdlM6jEX0lMjMEIAAMlxn/kEJJIzXTubVgVimDn+n"
    "QCWG8YEAAEkAgkCG2329ttttt2AHJRwGRHjla8EEFu2rx/7SQkM3pgBplUgkXtal3OAS2mSqcGwgAEgAEkGmwO3tttttuwg+"
    "Ar5bUTB7ITo6Wi3SdSfJajwT/tb/AOTTFIn8RzSHEtttBlBEsAJlgBAIEo9vbbbbbdpATWyJcyD/AGyXQJJLL/LIlLbJvvBN"
    "JZJpAwADzfne1LZZLbbbbZQDLSQSQAB7e222227SBwSRXaGOh5uiDSWAGEwp0199ZJ79vtvKerQDTwzpeAAaCBASQASQACAQ"
    "CAPb222223aSQQKw1TpIkePwCQCzmTJRtkkgkkk22lP8eQCSCKSSSRJAAQSACSAQCQSSQD7e222227SACQBOSo8M9mZyGGgg"
    "EWzQC22kklw2y2yFiASAJJCQABFSAQSSSSSSSSCSdfb222223ZySSACAQCkDcGAAAAAW229ZJLZLJJLJLJIkAAAAAAAAABFb"
    "bbbbbbbbJbJLL7e222227Av/ALvaP3/t2JJbbbbaSSSSSSSSSSSSSSSTbbbbbbbbbbbdJJJJJJJJJJJJIf2dtttttO222222"
    "222222222222222222222222222222222222222222222222222222dtttttv+2222222222222222222222222222222222"
    "2222222222222222222222222ytttttttLbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbrttt"
    "ttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttRJJJJJJJJJJJJJJJJJJJJJJJ"
    "JJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJhtttttttO2222222222222222222222222222222222222222222222222222"
    "2222222bttttttm0Jm222222222222222222222222222222222222222222222222222222222Ntttttq0H4myN222222Ru"
    "22222222222222222222222222222222222222222222222dttttt2fu72nUfArgxuLb4SO2222222222222222222222222"
    "2222222222222222223tttttu04PmwCLU/Ze5bYBF9222222222222222222222222222222222222222222229ttttt2Pu5"
    "ytuNNJN2xRRJO22222222y222222222222222222222222222222222223tttttu2sl2wyyW2yWWWmymyWSWW2iW2SS22222"
    "222222222222222222222222222229ttttt2222y2W2Wy222Wy2ym22W2yy2Sy2222222222222222222222222222222222"
    "3tttttuz7bbbb/bf7bf/AP8A/wD7SWS22WEAgsA9t7ikgEEW2yyyyyyb/wC2+22222+22322NvbbbbbdvAIAJJJBIJAItkJA"
    "LbaTSyTe/wB9v/Lff9/t9v8A9NtNtNMG22gEgkkkkAkAgAe3tttttuzkEFEgAEEEgCW2kkFtJtrZN7ab/fYD2Db77b7dpNNp"
    "lNMySgkEgAAlAgAgEj29ttttt28kAttkkAAAA2yWkAppNpZ/7b3ffcNkFbfbb7/bbfYsNNkSYtIENJJFpAoyk+3tttttuzAs"
    "kkhtFpkACSSAglJpNvf7b+777/8A22/++37D/wD99/yCHbfG2mwSCCSM04QPb222223byGSySTS2AUQJJKUSEkmktJLZt9/t"
    "vt/t9/ZX8qz97fJLnb6mzSSbSSBe2az7e222227eSknZdJUQ0wSAYAG2k22vvtdtvty38lqEvtvvgfXkWNdU2mlZLYRLZLIi"
    "y2fb222223bgUlJYr3ukmACQQAm0m0t9/wC/f/v6Yoi78fb/AG/3oVJDNG4aa8kkAkklhjxbHt7bbbbbtwBYNvqFVPbBBJIA"
    "TaTTe/L6/wBs3dQXY7nZdv8A7bW0t5gqOnJ+CQCSSSSmnhj29ttttt2ZsuiUd0yolBkkEgJtJsHvZQfSuW5ugg95ICzfb/a8"
    "f9dXEUPUmeAJCCQ0lI+3tttttu3pNTf1IHAhsEgAkBNto25OkXcz8ckkkgkNSt787TedWC35kfyBAar32aS2tH29ttttt2dt"
    "qSiyx6cpMgAkgJtvTIQR/wDwokpJJJpNGwL9ot3usZnPww+lc4mu3rElkRXt7bbbbbs7aUBvCTggTSJJJATaXX/W03kVZppJ"
    "JJJtJ+g1v/kuHi9t/wDvLuONB8CrJak/b222223bm0air14JgWmCASQ20jQOnA/sKKSSSSSSSRMvWU/ZAIZv79/5QI0642lL"
    "YAj7e222227c2lU8YnlJE0EQQSGmn8kSd/bxCSSSSSSSSYk4v/rqbx4N9+no4tH5JZLKImfb222223Z2mtNdEZZekwSCCSk0"
    "l3vt5iLSSSSSSSSSSaKt/wCe0l7G/wD+aNACEWk29tjTHt7bbbbbs7b2t8yGkn6AJBIJTaSe+2f14ZJJJJJJJJJNNjH8+6df"
    "jsv66nEAk0YIstSY9vbbbbbdnBSftsssmabRIIJCbSTX+32fBJJJJJJJJJJJoX321t18l1SafkvstklsvyTHt7bbbbbs5IbX"
    "1su3yTJJBBASae2/m1i5JJJJJJJJJJJMvP8At7v5tdgm3tvpLZZbJv2kPb222223ZwS3m88ll2wCSASQmkv/AO39ckkkkkkk"
    "kkkkkmY/6XbbfCO9NN5tNNdJZpJCce3tttttu3EFINptptNAkEAENNNf/e+e0kkkkkkkkkkkkwm/7bfftZJREgAAAgEkAAEX"
    "H29ttttt2cAAptNNpIAgAA2y95r77zapkkkkkkkkkkkkmxz/AO++6bz78LTTTTaTaaSJTHt7bbbbbs4IBQJbabQBAAIAsh6W"
    "+2m4tJJJJJJJJJJJJNq+2/8A/kst2AAQSQ02kAQAC2Pb222223ZyQQQQm02AQAAAbaSk9v8AzdwkkkkkkkkkkkkkxL77/wDz"
    "SWzAAJBKSTbABJBAPt7bbbbbsD/22/8A9vv9v9v82nvv9/rKI/8A/wD/AP8A/wD/AP8A/wD/AP8ADWX77dJf7/7f7bf/AH/2"
    "23/ztnbbbbbTttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttnbbbbbb/ALbbbbbbbbbbbbbb"
    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbK2222220ttttttttttttttttttttttttttttttttttttttttttt"
    "tttttttttttttttuu222222222222222222222222222222222222222222222222222222222222222222222222221Ekkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkmG22222207bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "bbbbbbbbbbbbbbbbbbbbbbbbbbZu222222bQmbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbY2"
    "22222rQfibe7bbbdbbDbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbZ222223Z+PvWOGAcThLE3ggPig8sKJ"
    "8T5qlwdpHbbbbbbbbbbbbbbbbbbbbbbbbbbbbbe222227Tg+bSWCx90XscoSGEXyMMTgAfMxYwee7bbbbbbbbbbbbbbbbbbb"
    "bbbbbbbbbb222223Y9XnJlU2FklbXVYokk0lG0rkk5E167fbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbe222227ayXbZLJZZJbZ"
    "ZbbZbLZLbLbJLJbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb222223bbbbLZabZJbbZbJZbZbZLZZbLbbLbbbbbbbbbbbbb"
    "bbbbbbbbbbbbbbbbbbbbe222227UgQACznVbtgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAZIbb222223Yp"
    "QaSWN6PgaSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSDBL7e222227BKDSQbpJRFSSSSSSSSSSSSSSSSSSSS"
    "SSSSSSSSSSSSSSSSSSSSSSSSSSQYJPb222223YJQaSAL7AAGSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSDB"
    "J7e222227BKDSSTSSSSQASSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSASQYJPb222223YJQaSTBG0kSSSSSSSSSS"
    "SSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSDBJ7e222227BKDQBL6I3DgAAAAAAAAAAAAAAAAAAAAAAAAAAAFySSSSSSSS"
    "SSSSSSSSRZJPb222223YJQbAWE9vDCSSSSSSSSSSSSSSSSSSSSSSSSSSSSaSSSSSSSSSSSSSSSSSKJJ7e222227BKDaKGTd9"
    "oSAASSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSRRJPb222223YJQakDtiq5SQSQCCAAAAAQAAQCCAQCCAAAACCA"
    "CSSSSSSSSSSSSSSSACSKBJ7e222227BIQdvlwFPeSCQQAASASCCSAQACSQASAQSQQQSSCaSSSSSSSSSSSSSCDSQRJPb22222"
    "3YJKQW8C6s3mQAQSAACQAAASSSCASASAQCQCCSAATSSSSSSSSSSSSSQBSSKBJ7e222227BIIC6FdI9cyQSSSQAQCCAQQQQSC"
    "CASQCQAAQCCSaSSSSSSSSSSSSSDBSQBBPb222223YJLQDriiTzuSSRwAQACACAASACCQQASCQCSCQCASSSSSSSSSSSSSSSDC"
    "SJIJ7e222227BJRC5qRXo1SASU+SSSAQCCCSACQCAAQSASSQACSSSSSSSSSSSSSSSZASDQJPb222223YJCCdCLHA2GTQ3fyQ"
    "CSSASSSAACSASSSQASSQSSSSSSSSSSSSSSSSSSaCSJJ7e222227BJQLBWu4x4scGWSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSS"
    "SSSSSSSSSSCTQJPb222223YJDTQZlyb1VaSTySSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSQTTTJJ7e222227BIaTSS"
    "UTrIAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAAAAAAAAAAAADCbSZJPb222223YJDJJEbqMLpJJJJJJJJJJJJJJJJJJJJJJJJ"
    "JJJJoAAAAIAAIAAAAAAZLJBJJ7e222227BJJJMlZJJTbbbbbbbbbbbbbbbbbbbbbbbbbbbbKJJJJJJJJJJJJJJJIJJJJPb22"
    "2223YJJJJpIZbJJJZJZZJJJLJJJLJJLZJLLJLLJJZZZZLJJJLJJJZJJJJJJBJJJJ7e222227BJJJIAQAQCSAQSCSASLaARIQ"
    "SAQCACQASACQSSCQACCQQCASRJJJJJJJJJJPb222223YJJJJACQCCQSSCCQCCRQATBaSQSQQSQAAAAQSCQCCSSCASQCASJJJ"
    "JJJJJJJ7e222227BJJJYCQAAAAQQCCQAACQCBRCCQQACQCQSASTQCCQQSQQASAQAQJJJJJJJJJPb222223YxJJJAQCCQQASS"
    "SCSABZAATICAQCAASQSSAQCRSIQASAARCCQAJJJJJJJJJK7e222227D/AP8A/wD9vt/tv/v/AP7b/f8A++3/APvv9999/wD/"
    "AH//AN//AL/fb/7/AH2//wBv/wD/AP8A/wD/AP8A+nZ2222207bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "bbbbbbbbbbbbbZ222222/wC22222222222222222222222222222222222222222222222222222222222ytttttttLbbbbb"
    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbrtttttttttttttttttttttttttttttttttttttttttt"
    "tttttttttttttttttttttttttttttttttRJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJJhttt"
    "ttttO22222222222222222222222222222222222222222222222222222222222bttttttm0Jm222222222222222222222"
    "222222222222222222222222222222222222Ntttttq0H4m3m22222222222222xz2212yu2222222222222222222222222"
    "22222222dttttt2fN72s7kD738AaFHAC1Ylz0z+jh42222222222222222222222222222222223tttttu073mwqhIzOBTk5"
    "+BzGvGIw800eDe2222222222222222222222222222222229ttttt2Px52yVqLNRPSrWRpRdJxpNVS1S12yy22W2W2222222"
    "2222222222222222223tttttu2sl20yC2WyySwm2y2Wy2S2Wwwm2y2W2S2yS222222222222222222222222229ttttt2222"
    "yySWy22WyW2USS22yWWSSW2USSyS2yyW22222222222222222222222223tttttu1IAAAAAAAAAAAAAAAAAAAkwmAW0wmgik"
    "A0AiEAySAAAAAAAAAAAAAAAAAAAG29ttttt2KSSSSSSSSSSSSSSSSSSS2WSySy2WyS22WyyySiyySSSSSSSSSSSSSSSSSSS+"
    "3tttttuwSSSSSSSSSSSSSSSSSSSSSSSSyySSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSST29ttttt2CSSSSSSSSSSSSSSSSSSSS"
    "SSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSe3tttttuwSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSS"
    "SSSSSSSSST29ttttt2CSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSV+SSSSe3tttttuwSSSSSSSSSS"
    "SSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSi8SSSST29ttttt2CSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSS"
    "SSSSSSSSSSSSSXqSSSSSe3tttttuwSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSSJVSSSSSST29ttttt2"
    "CSWkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkh+UkkkkiSSe3tttttuwSR+2wySSSSSSSSSSSSSSSSSSSSSS"
    "SSSSSSSSSSSSSSSSSSSQl6SSSQS0NST29ttttt2CSUS2gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEJYAAACGGA"
    "iSe3tttttuwSSSQkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkfHkkkkm0E2CST29ttttt2CQC0kkkkkkkkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkk8r4skkkkkkkgSSSe3tttttuwSCSAkkkkkkkkkkkkkkkkEgEkkkkkkkkkkkkkkkkkkkk8vd"
    "kkkkkkkkkAyST29ttttt2CQU0kkkkkkkkkkkkkkkkkggkkkgEkkkkkkkkkkkkkkkilskkkkkkkkkkgSSe3tttttuwSGSkkkk"
    "EAAkgAkEggEEgEEEAEkEEggkkkkkkkkkkkkkackkkkkkkkkkk2ST29ttttt2CQwUAkkkAAgEEggEAAEEEEggAggAgkAkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkSSe3tttttuwSmmkEAgkkkEgEAEAAEggEEEgkEAggAkkkkkkkkkkkkkkkkkkkkkkkkkkGST29ttt"
    "tt2CU0kAAkgkAgEgAAggEgAEgEEEgAkkEAkkkkkkkkkkkkkkkkkkkkkkkkkgwCe3tttttuwSm0kEkkgEEEgAkEkEAgEAgkkk"
    "gEgEAkkkkkkkkkkkkkkkkkkkkkkkkkkECT29ttttt2CU2kkkkkkkkEgEgkEAgEkAAEgkkAEAAkkkkkkkkkkkkkkkkkkkkkkk"
    "kkggSe3tttttuwSm0kkkkkkAEgAEkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkECT29ttttt2CU2kkkkkkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkggSe3tttttuwSkUkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
    "kkkkkkkkkkkkkuCT29ttttt2CUBAAkl2kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkAAEASe3tttttuwSjkk"
    "hLaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAkkSCT29ttttt2CUPkBBK9Ekkkkkkkkkkkkkkkkkkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkAkjwSe3tttttuwSm0kn8NkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkECT29"
    "ttttt2CU2CcCkKg/kEggkkkkkkkkkAEEkkEAkkkgAAAkEkkkkkkkkkkkkkkkkkggSe3tttttuwSm1zvvxEmEggkgAAEAgEAg"
    "EAkggkgkAkkAEEgkkkkkkkkkkkkkkkkkkkECT29ttttt2CU2zyyaoY1kAAgkAEAEggAgAEEAggEEkkAAkgAkkkkkkkkkkkkk"
    "kkkkkggSe3tttttuwSm0XwlaUomAggAAgggAEkgkgEgEkEgkgggEAkgkkkkkkkkkkkkkkEkAkECT29ttttt2CU2u5wO9PuMU"
    "EkEAgkAAkgEEAEgEkAkEAgEAEEEkkkkkkkkkkkkkkkAEggSe3tttttuwSm0bV7c4B1CAAkAAgEEkAAgEkkgEAkgEEggkgEkk"
    "kkkkkkkkkkkkggEkECT29ttttt2CU2yYkSuxu8QAkmyySgkEkkEQiEgAkCWUACUgAkkkkkkkkkkkkkkkggkgyCe3tttttuwS"
    "WGoRs9BGoAA0EAmWwAgAEEiG0gCkAWSk0SAUkkkkkkkkkkkkkkkEgEkGQT29ttttt2CSWMvIAu+NkGg2CGSkEU0gikgAAk0k"
    "AA0EkWCUkkkkkkkkkkkkkkgAAEmyCe3tttttuwSSUdcIYr8UgimwCCgH32EQwAw20UEAkmkGg2wkkkkkkkkkkkkkkkkkkk2Q"
    "T29ttttt2CSWwJu1NJakiyWQACSMNiUyAWSyWCiW2wmCQyUkkkkkkkkkkkkkkkkkggySe3tttttuwSSAQQjEGYsgEgEggmuq"
    "AAACQAAkkkAEgAgkEEkkkkkkkkkkkkkkkkkkGCST29ttttt2CSSxIZ4+iykkkkklXMmkAAmW0kkkkEEkkkkkkkkkkkkkkkkk"
    "kkkkkkkGySSe3tttttuwSSQwWviQyQAAAA/FAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEgySST29ttttt2CSSAQgG22"
    "2222w+W2222222222222222222222222222222222222ykySSSe3tttttuwSSSSSS222222C722222222222222222222222"
    "222222222222222ySSSSST29ttttt2CShSSyySySSRJqSWSSSSSSSSSSSQWSSSSSSSSSSSSSSSSSSSSSSSSSSSUiSe3ttttt"
    "uwSSiSWWWySSXkSS2WSSSSSSSSSSSmSySSSSSSSSSSSSSSSSSSSSSSSSSSSST29ttttt2CSWAEEkAgQn4AEgEUAAgAggEwAk"
    "gk0mEgEgAAAigAAEAAEiAEAAAAAAABSSe3tttttuwSSSSggEgNJE2UAy0WQQyAyAAkmGyGEAkGEgUgkgAEkkEAAAAkASSSSS"
    "SSST29ttttt2CSSSUSydIIEEEkEW0gAggySEQA2ySUEkkAkEAEgkEEgAEgkAkgCSSSSSSSSe3tttttuwSSSSS2NHgECSUgWS"
    "WAkAASyQEyWWSAgCAAAEEkAEgAgEEEAAACSSSSSSSST29ttttt2CSSSUWSSgiACSEggGSEEAAAAEgEgAkSSWSSSSSSSSSSSS"
    "SSSSSSSSSSSSSSe3tttttuwSSSWggkkkAEgggkgkgAkEAEgkAAEkSSSSSSSSSSSSSSSSSSSSSSSSSSSSST29ttttt2CSSS0E"
    "gkgAkgEAAAgkEkkgAkSgAEECSSSSSSSSSSSSSSSSSSSSSSSSSSSSSe3tttttuxSSSSQAgikAgAkAkkkkkkEkgkWEkEiySSSS"
    "SSSSSSSSSSSSSSSSSSSSSSSSW29ttttt23//AP8A9v8Af7b/AH3+2+3/APtv/wD7/wD+/wD/APf7/wD/AP8A/wD/AP8A/wD/"
    "AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/APk7e222223bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "bbbbbbba222221bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbY2222221a2222222222222"
    "222222222222222222222222222222222222222222222hW2222222222222222222222222222222222222222222222222"
    "2222222222222222222222223TbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbIW222220Ikkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkko2222224kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkkkk+222223kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkl"
    "e222228mCkkkkkkkwkkkwEkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkr222223k3bU118q00/TwoV1q8kkkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkle222228mIS9ppHW6ajhDJGm1kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
    "kkkkkkkkkkr222223kkkkkkkkkkk0kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkle222228kkkkkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkr222223kiEkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkle222228kG0QkmywAmSGSCwGwSgmwCUm2CAykyyAkACkWUkkkkkkkkkkkkkkkkkkkkkkr222223k"
    "kg2W0GAAS2ygg2UA2w0yygGQg2yimESGWi0i0kkkkkkkkkkkkkkkkkkkkkkle222228kkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkr222223kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
    "kkle222228nbLWk6kpEvVZp7a837Tb5VZVL4pJJElU1S1Y7Wkkkkkkkkkkkkkkkkkkkkkkr222223kzvZ76ra1JEgoTQ0pnF"
    "VY83YqkypW1JXLFI3U1fEkkkkkkkkkkkkkkkkkkkkle222228mQkkkkk8kkkkkokkkkkkknkkkokk8kkkkkkkkkkkkkkkkkk"
    "kkkkkkkkkkkkkr222223kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkle222228kkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkr222222Ekkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
    "kkkkkkkkkkkkkkkkkkkkkkkmG22222skkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkq2222"
    "22AEkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkklW222220Fttttttttttttttttttttttttt"
    "ttttttttttttttttttttttttttttttttt/22222222222222222222222222222222222222222222222222222222222222"
    "222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222"
    "222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222222"
    "222222222222222222222222222222222222222222222222222222222222222222222222222222222222222//8QALhEB"
    "AAICAAMHBAIDAQEBAAAAAQARITEQQVEgMEBhwdHwYHGx8YGhUHCR4ZCA/9oACAEDAQE/EP8A4RrUt+trYN98tfXQ33rv66Mf"
    "6IPALrxBKa8eusRU8ALiU14LFfaz88ch3AxcS7Nf4g14DRnmRq5ZumgiXBBZEi4LURFTFC2IqOALUxd4mnASsMUWxFREtTMg"
    "TIMRURKal4LrwQWwSo4WuG6GgIFtQ3TlNmCZi7RNIkXAVRwKVrgIqYukSi7xNIlxFq4I2EXWCFxNOFzQyIm0QkYMWcFFsXSZ"
    "Di4llJWF0iURVFZIqf8ADmvAaPES6xigjYQVwJOubFGdwQrU3xa1HkxRu4MjCjUYbSC6TEEYEjdFKwYGc05iPLDWO4Nu4Ktw"
    "3TIUqVeUdlguXMMtchlJMtECjBtkteNyrLGiLqjEbEEVhzLBS4DbOSQaZzRVi65zLnNFW5mWbYqa4Z3SJBuIKRwELMVgxL1R"
    "VBHBDViav8Qa8Bo8RVeULi7CshKAIKRqrH7Wc2BYvC3zkiJlvMuUQCiPkjq8qDFwt4Dy4eachNMZwELVw3TMRFBznNDJl2Ch"
    "bEFkuM4t0g8hEXiKrqaoVTLFxsQqnhJTXBdM8yaLOaO4VTwuaO5om2DTcoGVtuJmJ1uNNBFG2EuIhi3HG2WMSqggVT/hzXgB"
    "TTwFNTcFNRV3Ctc8yOZMyEiruW1cVdwU1FvcVd8FO3iKai3uCmoq7gpqbltVBTXC2q4eZxFNcFXcFNcBTUVdwU1FXcFNRV28"
    "FXbFXcKrIpuLsJqKdvFV28BTUFNPFTtgprha4gpg4Wqr4Cmop28aOcU7eF1FXbwFNTf+HNfQBqMW5/Qxr/Q5r/Q5r/Q5r/Q5"
    "r/Q5r/SY+ug75K+uAua8CWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlp"
    "aWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlpaWlv/wAQLJSUlJSUlJSU"
    "lJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSU"
    "lJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJSUlJZ3+ot/XA1BvvV4piAc3BK6z9hr/tS+s/cb/wCXAxEeZk+t"
    "Brv9PAv55ujziRK5OQ93z9McQiXzcj7Pn6YmngX88zT58caXKiU13rUQLVBau6AY3AGd/Sp32qeqP6H4+/LgnovSte6zMd2S"
    "+tcN09Af0vx9ufFyVGVsxaBjCoVhhUtylLNoBbC0KCoQCDwcBJCgtlhCKhld4GoJbBdKhxVHKhA2YYDD1ZgVAgJEZdyiU3CN"
    "c4QEiMu5aoT6PGu+Aww0/kW/ycMv4yrldT+6/PBBDBT+VK/Dx1OERUzRezqFSIKU3FSY0WeAZ5keCiD0lXBmqaJQq5zBREXG"
    "IcL1xWxFVCKG23MAXKDbMoWcjqYAuUG2YwXFOY+jzXfJ1HTeTmfdxWNlc+B1KK8bmkNKXvPBOg67wcj7mbxtrlxVdwU1Ldy2"
    "7lurlpqCmpbqW1UVdxV3LarsW1U1Ldy27m5bVwU1NESYcBTU3LXEt1LaqW1UtcS3X0ia790tu+j1dWPyrbldbnpn8XUvrcdc"
    "fmrhpaddDo6tflWX9amv9Dmu+oMSjvgMsVxDMxYL536JAdD9oVxD5D7xGwnJBNcHz+Yhyf8AH3iXsPvH/wAR95+ufeI+0+8/"
    "SPvP0T7xftPvBR/Q+8Msn8H/ALMWYP3YBhLm7ZAR3F0RDxkdx1f40Tg3ZA2LxCOEgh7dXKnR8xj9BB/8iAgP9hg7wCVoPsnq"
    "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"
    "FMGCpHvVErghCLGTvSiuIJXKVNHCu1/dx/q4hwIoRb1ZbJhVNWFW202gpBCKqqVRN4zz34v7uJrwIRlwkAqLkRCmaYiSjcsh"
    "HEymeFwcxcASodzfA4HYezngSpXZO29iu0RIG8S0zBgZaYjKxF7X93H+r8a/dxNeBHhUpwFDTB2Qt1CBwm4MiQUEZcHir4oV"
    "2a7AdoJXiTwABLSuzqLDZEYEtdxCblqoiYalX2v7uP8AV+Nfu4mvClxiXCYwrjPDZVLSmZlsJEiUhxIcHiFwmsrHjtZSUlEo"
    "lEolEolJSUlJRKJRKJRKJRKIBKJSUIxInhP7uP8AV+Nfu4mvEFXwU4KxMZOEcNXlEdIUgk0oHYHiy5dQZLS0vLy8tLdZaWl5"
    "eWl5eXl5eXl+svLy8vLy8vLS0tBR4V+Eq7prrVLQ+6oBjL/CdqnS6lFzqw1QgXYPFv8A1UB8wG1cBlxG8EA9RMwzkLpnA55z"
    "dxNd9iHwFcalcb7zExMTExMRplSpUqVKlSpUqVKlSpUqVKlSpUqVKlSpXhA6IGxMImkeSR6AQBkHKuS4XQ3QAWOHN5Ao+1jz"
    "yv8AzE/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dx/dxoouUva3Q1nGi6GF"
    "IlZPCrPMgvOaMtGSrEwNcTXfAFVAP8RVVVVVAVVVVRRRb/I1UUFUVFVVVVVVVVVFA7QEHPANbzPinrPinrPinrPinrPinrPi"
    "nrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrPinrHCW2ra8srnWIpFDDid"
    "47+ujPepcSvrjcCv9DAAAAAAAAAAAAAAAAAAAAqv9L2SkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKS"
    "kpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKS"
    "kpKSkpKSzv8AUW/rgag33q8UxAObgldZ+w1/2pfWfuN/8uBiI8zJ9aDXf6eBfzzdHnEiVych7vn6Y4hEvm5H2fP0xNPAv55m"
    "nz440uVEprvWoixUuV9QHfap6o/ofj78uFLNlHNf46ecqc6458N09Af0vx9ufFyVGVsxaBjCoVhhUtylLNoBbC0KCoQCDwcB"
    "JCgtmuaVzZTeYMIi7lELQcLiERVP0ya74DDDT+Rb/Jwp7g2ug84nlOr6vP8Aj5rgghgp/Klfh46nCIqZovZ1CpEFKbipMaLP"
    "AM8yPBRB6Srgwqi8o5DAtSW4GJSqQppzIRYFmKzhODl9Mmu+TqOm8nM+7isbK58M6lbVM9MrqqgGrbOFV/DwToOu8HI+5m8b"
    "a5cVXcFNS3ctu5bq5aagpqW6ltVFXcVdy2q7FtVwtcyzzlpLaqW1Utfps137pbd9Hq6sflW3K63PTP4upfW464/NXDS066HR"
    "1a/Ksv61Nf6HNd8Fyw0T7J9k+yfZPsn2T7J9k+yfZPsn2T7J9k+yfZPsn2T7J9k+yfZPsn2T7J9k+yfZPsn2Q8kvyl+Uvyl+"
    "Uvyl+Uvyl+Uvyl+Uvyl+Uvyl+Uvyl+Uvyl+UvymOkx0mOk+yfZPsn2wtyjTlC3KX5S/KX5S/KX5S/KX5S/KX5S/KX5S/KX5S"
    "/KX5S/KX5S/KX5S/KX5S/KX5S/KX5R8k+yfZPsn2T7J9k+yfZPsn2T7J9k+yfZPsn2T7J9k+yfZPsn2T7J9k+yfZPsn2T7JY"
    "7Ila4mu+cAf4A8Gdh4u4R8WfH5Y4mu+5O0SpXiDwB23tDwl8K8BXaqPfxrvuTtHYxMTEqVK8CeAO3UpwUSiV4WuF9yOPApsU"
    "01Qm+Q3vo99qIIJJLLJJJJJI9xIIIJLLJKjeh1J0whf4Mb1EoIpHCJsTknajXfcnElSuzcuX2HwB4A8OlBFAZVdAc1l2vdQ1"
    "vvNWA1tHIaWolBFI4RNick747DozuC4bNA2WUC07ItHBIqiA3Zg2MWBRdLYnhgiIiIAAAAAACIiAInNRX8CDnydc6gitVQp5"
    "hhq8Ii2oFoWlOo3SNJjG+nZjXfcnE4HdqlSo91UqV3dSpUqVK8LUqVKO5bMk2sHTTrNU4ZYErt0brJlePKvTMK9gpUtl51EL"
    "5azpl2uzasdcVQM6BVAUYloJzgKVl5hMGg5calqvdQ1vvNWA1tHIaWotASkcImxOSSpXC5cvsHGo1q1TsFtsrAbErOa5SpXg"
    "b7DLy5Yc4DgzRZli2ssSyban3Lx8qHXN/bsxrvuTjUrg5muNdm67D3R3vKX3N91cuX3VAvCrXld5XzLredygJXbs3WTCsecO"
    "uZYErt0brJlePKvTMK9gpUtl51EL5azplmszasdcVQM6BVAUYloNzgKVl5hMGg5cal2vdQ1vvNWA1tHIaWolJFIlImxOSdJc"
    "uXwqGO5lvv77q3bjXfcnEOCxe3mZmYcXujveUrg57rPBuBKlSpXG5fdmZULwq15XeV8y63ncqUV27N1kwrHnDrmWEK7dG6yZ"
    "Xjyr0zAfYKVLZedRC+Ws6Zfrs2rHXFUDOgVQFGJYCc4ClZeYTBoOXGpfr3UNb7zVgNbRyGlqLSRSOETYnJJruZXwJwe3btxr"
    "vuThUOLCVLxrxGsTjfGu5O9vu64g2W7AXyijEtKYnA7oi8KgSsXhVrF3eV8y63neZXGl21ZusmFY+065lga3bVG6yZXjyr0z"
    "DvYKVLZedRC+Ws6Zfqs2rHXFUDOgVQFGJYCywFKy8wmDQcuNS7Xuoa33mrAa2jkNLUSgikcImxOSRhREDC6kf4sX5dNzBVNQ"
    "GE8ysCcU1YBhDie0Ee7vuDduNd9yQ454mYRYS5XC7jcYd6dzfaYdm+AMEwmpUrs1EMTEijgQldw8CEWosGEoFoVaxd3lfMut"
    "53KUFdtWbrJhWPOHXMtAV26N1kyvHlXpmFewUqWy86iF8tZ0y/XZtWOuKoGdAqgKMcAoApHIjsTmMTmq+63k9V/6FPA8RMUb"
    "guKCMp4UzXfm7ca77kh2WFIWiKyq4EuHgB3Vy2XLhGHEjDgAQ72yMtJcvsPEjwIa4hiix41m0KtYu7yvmXW87zxegUlFaq1a"
    "ZMNmRpKanMb6pE8ghk6mHDhscRLoMTFMmGMc6mHg2xSXXeG7ca77kh2dyswyouBDfE77NdwdtjwuXLnPsqjAvghXgK4SQK4B"
    "GPE3HgQYJUDMCPG4IgF23R5tC0eQvQYPgJQ6tAvyLusXVWbgqSwYHI3dlLyaBWKy2lBFq5Vdq817UWS6aixBbhqNXBxwErgT"
    "PcG3ddiNd9yQ7N1BmnBllS4RQe29s7bF4XwuEEeBwC5zYHgngipzhHcS5Uqa7GcTgM4ENdg7crFFxOc0gZgYgzBiVbBjmWe8"
    "Nmw133JDinAzKzBKzAghDwEd9p7Z2nhXCpUrgcOXAnU8IYjwk4vYTshDBI80FPB1DsSp2BqWY0YXhCq3NoOo58Lfubs29iNd"
    "9yQ7FQw8BCjTLu4wS8dyuvZvtndUSpR2OU2yyV4ar4CTn2ajwqBKioEGovAw7Mr2LjKmZuVKjKlSuyHE2bexGu+5IdllQIwi"
    "Q7BrvTvq4Xyl0CvEJDUd9gjHhjhcpLOAxYEru5ccNS4OJeZcuX2DsGzb2I133JDsvC+Fxb7Jrs13B3pFhmbSgvxRMQhxJfBO"
    "GeFQlx4BHtS7lvbrhXaogdwbNhrvuTwr3B3puMNwXArxSQdupUqVwqBKlcHupXuw4X2DZu+wGu+5OJ26JRK7h7k7s4G+AQw8"
    "WY9q+xUrhri91LUqVKlSpRKlSpruzduNd9ycTwD3R3ZwIsIaPGHEeJHuLi8LlzltQnKxcK5Vm9VnXh7hVVELyFoL6tNdaekD"
    "0oHybaejSP2R59mNd9ydi5cHully+7O7OBHg08ZkPdrly49h6Q3g1TWGOgqwTZVoGmOwgbLzQRyb2pGleFllllllllllllll"
    "llllll0AiL0I+9lnWB/5mXoumkKaC2t4u21aqo50u66GhgLoAus1bnsxrvuTtHdPfHdHF4Dxjmb+BSgixMImkeSQcKA53N80"
    "GgYoHJvd+FCIiIiIiIiIiIiIQgclUfyCOPJ1zqCrNXQwC3Qf1baAtaO1Gu+5O0S5cvtviDizY8azbsXLly5cvvrly+4uXLly"
    "5fFh2Y132yu6uXLly5fgDuTsb+NZvxe+e5uXLly5cvvNM8TXf5bJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0ln"
    "SWdJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0lnSU6SnSU6SnSU6SnSU6SnSU6SnSU6SnSU6Qpo8dVVVVVVVVVTbZKdJTpKdJTpKdJT"
    "pKdJTpKdJTpKdJTpKdJTpKdJTpKdJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0lnSWdJZ0l"
    "DRFvid47+ujPepcSvrjcCv8AQwAAAAAAAAAAAAAAAAAAAKr/AEvZKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkp"
    "KSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkp"
    "KSkpKSkpKSkpKSkpLO/1Fv64GoN96vFMQDm4JXWfsNf9qX1n7jf/AC4GIjzMn1oNd/p4F/PN0ecSJXJyHu+fpjiES+bkfZ8/"
    "TE08C/nmafPigtj1r7kipmJeyV9Qh32qeqP6H4+/Lg9JlVUr96OUaaDtHP5Zw3T0B/S/H258AtqLIR0JUrIKsSiU3CNc4gFj"
    "YQCCj1QC2AlwwFUQbflBghZkwowYiZFqIRSAitQMZiA3FELQQSZIgPKVqQWKg5MwVxSgqUmYVIEBIC7ALYCXBNrygJo4AWwL"
    "XwFg4VDiqBNGYmNzDCzUHLgUqXEIiqf84a74DDDT+Rb/ACcC7FMVh/N36RBxrVbx975+3BBDBT+VK/Dw1BSDDcZiaxQs5HUQ"
    "agX3zlV1yhSwAseAu4RY85hyamIqU3BwERYIRomi3FUFYoU05kIjSs1Ibw3ZKJGiJaw2kggRL7xBqCsQWh4CG1lDDgYR1Grp"
    "BhTcyOCurR2xMCGYOl3MVnCcHL/OGu+TqOm8nM+7isbK58Kmnc0Z+9zl0ec/u8E6DrvByPuZvG2uXEa1FXfBV3LaqW1Uxgq7"
    "gpqKu5aYgprsW1XC3rPM4W1XBV3LSW1UtqpbVXLXctqoKampbuKu4KaltVPMjjay2CmpbqCmuIpqCmpbrBTXARp4eZFvf+eN"
    "d+6W3fR6urH5Vtyutz0z+LqX1uOuPzVw0tOuh0dWvyrL+tTX+hzXfUGJR33AAAnmK7n2r2hl/wB0Dn/9IfvIftIftIFX/ZB2"
    "mv3r2PqRgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACpqNjGeJrvsi+5N5mGpzE5Ls1FXuWGcvqMXY4mvEFxenFcuXylVN/Um7"
    "ia8Qb4X26+o93E1/oQ3cTXiy5cuXLlnDYNCherdB5qGHp/mnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn"
    "nnnnnnnnj0ABXYtouwN8lvXU/wAVu4mvDmuC1DMSoGJzhuppGhzpbujnlM1WByNTKDr7DOU6F2rVtpH+aKqqqqqqqqqqqqqq"
    "qqqqqqqqqqqqqqqqqqqqqqqjVo2bWqMClmhKaobQgUwuN4LcIdFtINGgLf8AE7uJrwxcIxgnNQHKJhLNLGI4i2x9708qHXNx"
    "O86u/wDhatBgLwARr/NJQ2qNoNtt5Bat5zXL/E7uJrwxzlaTlsSMS1GG45RjIpgBHxaYAgJ9RvbuJrwgzlwqGMNxjDKJBDEs"
    "VCkCy58rzQ/Uh7dxNeEGc5pNsQOUPQlwCN8wCktQ0R38W31LPbuJrwpzmkdMbBHBGZBE6c5ckrVxSpM/g2h+pD27ia8Kc+Ay"
    "yqjZctSIFDIcMIzrPgeaD6kvbuJrwob4FTDMQIYcRsjN9ZeYKxXCjUl32jvhtBKUCNspkUtVFasuhF0Ea601jH049u4mvDDq"
    "UUhXKKkYFAYwFEwCVrKB8ZQoUdpXFsYo5gV2BRjmlnMYBbYgjzyON5l4oltWnXFNB5AVQFGJcrDoUvIBmc9Kq1q0+mXt3E14"
    "UeDkJvYKQ1IvNiN3HrRFmGB8WjusCuIcFTQGVXQHNZzQDLKVe8jdY+x1mUrHh0PMVtR1ZevNw3DK7sl9UoOqus+cDoKLEyI6"
    "R5jD/itX5ldAZXBmZQhw3I5uMWsu+RaB9NPbuJrwo54trA+c0DMisCKuefFruMxMbuU0IugrXWi8ZlQg3TbbWMhYfIRk5GYW"
    "NAbzzMIKiGMo21qqwrpaFiF00zzVQ5sMx5UiZ5VclGw4pHPOrsHEswzTq1bYc9melMEttVV5ZXOsSkdkzQALP4GCtab6qCk7"
    "QvPK902mhMGt2fpp7dxNeKLlSpUbom6utgulUGurzOpKdQm6bq1S+pdNYvSmY0NkuXeyqoCOZabzeCrQGkwtOVKVM3eDO8sK"
    "Kloy6pvORsrTTLrGW23YNasVsyHaKL+xXYXuOI65VfkWtaunYV9NvbuJrxRUobRLTUvnooc6KdMllBC04NU3jA2Vspl3jOWo"
    "Bc6bZYLmtH/CuSAYLa61g6vP3G8QgVCirFKULrSlU5WZTLjAMOjDdIAsepeDof5vkRBFxRZbfKs3qs6/xO7ia8OEEtoBV54D"
    "OsyqQCWLrpiih8kKpunEqFAbxQTGEy62gw4pzCRrmILeaWt1dK66EvALVlFF5wtE80WHm4jaWArHIyKLC5wHRe78kQspe6ta"
    "ui+Dre2sAV082gBnYAZMtKVtTAupyE4bobtRt/zR55555555555555555555555555555555555555555555555555555555"
    "4eYTkI6KzYFzb0oCwDzCsCq0uOiXgOaI0f8AEbuJrwwgtORClJo4UatWq03GRrkALeLCt1Vg66EopBbVr5JhI+YjGOhSQwYN"
    "abuyh5NArFZbYJbaqryyudY7ToqkBsByDOsGAooor/NKqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqnIlDPOvkoUTFK55"
    "1SdFTauVXavNf8Tu4mvDD9QVQ0NAbrYgFNm6239W7uJr/Qhu4mv9CG7ia/0IbuJrvsQ/W2Bria74AqoB9aVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVSkUMOJ3jv66M96lxK+uNwK/0MAAAAAAAAAAAAAAAAAAACq/0vZKSkpKSkpKSkpKSkpKSkpKS"
    "kpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKS"
    "kpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpLO/wBRb+uBqDferxTEA5uCV1n7DX/al9Z+43/y4GIjzMn1oNd/p4F/PN0e"
    "cSJXJyHu+fpjiES+bkfZ8/TE08C/nmafPjU2x8vHgB4UZXntEG+yiYfoQ77VPVH9D8fflwIK1VKjeBuq6+b04bp6A/pfj7c+"
    "LgqYEALiKUYgCBSAlLFQhEVYGTvgxLAcYImEgNSDl5IkYRCWXP8AhA1BLaQs0S2qVaLFcBJkgFJzmSYKK1EzBKVYRBSQUEoX"
    "dyiMFRwOeBMFoZNQFbHaqz9CGu+Aww0/kW/ycKzhooWznjm/Ocz+wF01duL68EEMFP5Ur8PFAVNBiQRZvIqYVMxRUjVgE3LC"
    "4bJz8J0yXeor0g2dynCYhgTmCiLMiGUuso0cRK54I0rNSbE0W4kW8MbxnMh5pyioGXXg54SzAX5wuhJjXf0Ia75Oo6bycz7u"
    "KxsrnwDAA82IJghY9S84XgnQdd4OR9zN421y4+ZLuCNMVdwU1LXbFWxlIjLaqCmuFuoKagprhbrBTUW9y3XhbdwW3BjamYlt"
    "XDqj03Laq5a7iruairuWstqpbVQU1wFNS7Y/nLqLaq+FtXwtcX9CGu/dLbvo9XVj8q25XW56Z/F1L63HXH5q4aWnXQ6OrX5V"
    "l/Wpr/Q5rvqDEo7+tAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACpqNjGeJrvsi/rYXY4mv9CG7ia/0IbuJr/Qhu4mvr"
    "krK323Np3kVg07qVg+ePNrM082RZUW0LsulgdFC1yE1o7jdxNfW5vQ1yLMeZg2OFVUmtDXMpx5GRY5F1aVInlmas8rQoovTT"
    "N6G+RZnzMGkwqundbuJr61M9BcpR5XWxps0qSjNaGuZTjyMixyLq0vF8teTeZp5MimpeVvtuTZvIvJp1XebuJr6zKwfPHm1i"
    "7ObBsuPz0KqKGbbwClGhZu4YSHTF7pLZ1kQHFPS8rfbcmzeReTTqu+3cTXgxoDbS6qwXSqDXV5nU+nnnnnnnnnnnnnnnnnnn"
    "nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnqpQOaid6FW6u3ay9Ssvff"
    "c2nWDWBZu5ey5tA3uwkawAI6MuYbWAxuqOYtLbphXu3nnnnnnnnnj0ABXYtouwN8lvXU7O7ia8EXLryrq8cYzQNuTKVZSUNG"
    "CqgGKF8mQBwFgI/TaqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqodCmgLqugN1lXjGB6RLo"
    "oy7aJTzRgEtqgHLK43iXCjmrXlqxXmNmmLG++VVVVVV1VUatGza1RgUs0JTVDaECmFxvBbhDotpBo0Bb2N3E14I5iLC+qqtV"
    "i2utc7zE6Km1cqu1ea/TJBLaAVeeAzrMq4N0ImtUoEvKqJsROIClFt0ZWhQwVbQS5tvMOTWC5mcHJSXKs1ejRgKC6LoL258A"
    "lDao2g223kFq3nNcuxu4mvqo5BLaAVeeAzrMqabyHm3kOTjIw2iguZXbVtW2m6MvIJ5YJhN9GUTSpnJim5Vmr0aMBQXRdBe3"
    "Phnt3E19THBXRAOdzDys2FM5YWCJSonRGkxjfSGENyDWQt5BaWtBzY8FoObkOFSiNaA5pMMUFzK7atq203Rl5BP5jP8AsX/w"
    "R+xlwhua6ytHILWgoOR4h7dxNfUp3/qoD5gNq4DLiDaYMtV1Fly/dooMBLFKQsYsc2zQgQBxYG4IbkQ21RdFpq3LzWPJaHm5"
    "BkBoje0OLDJHCG5rrK0cgtaCg5Hint3E19SnQjMjoeaaMOKZGsikWWjYQpdNYacNOadx2EpEtrwpbbqq39vGPbuJr6lOEiIB"
    "WIFBm+usGQGmUqLzotR5W11YYebAo/hVOPI6ecIIoAkcGCjRVUsRLptfGPbuJr/Qh3t3E1/oQ727ia/0Id7dxNf6EO9u4mv9"
    "CHe3cTX+hDvbuJrwRi5LQGbcKK53it3jf1dyIgi4ostvlWb1WddjdxNeCFW6y4ybeRUVG7L0CwDKctqpvrxXNFKrYQfVbzzz"
    "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"
    "zzzzzzzw8wnIR0VmwLm3pQFgHmFYFVpcdEvAc0Ro8d3E14MMIDritUFMbyqjm3p9Vqqqq7u6qqqqqqqqqqqqqqqqqqqqqqqq"
    "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqpyJQzzr5KFExSuedUnRU2rlV2rzXsbuJrwRQml0LXQ5FroUwLmqhpKay"
    "DQkw82QgWtAv1Y8888cccYc8888888888888888888888888888888888888888888888888888888888888888888888888"
    "8888888888888888888888888MCsVpEEcXTZ2YynUj+XtNUo0mlTTi6wV2N3E14IELmqzdlKus1FYLP5b0y/4HIDkHuqqrxY"
    "EqVwupWXf1C/9VifMjpHCYcQrUC92A1XhWlxvGzZ2N3E14g5FjAOsEZQzX1M9u4mvDHplQBmOaJ1cs7nQZsoRTB2JTUB+o3t"
    "3E14U5uUCVscLzJjhKtkWDlSlwimGAC5f1G9u4mvCHuokoxLKUCA6qaRHUIS0bKVaSxZOenn9RvbuJrwh0uLRMDBEUcRVlhj"
    "aIRlTjBUdEMj6kR7dxNeEOxw5nCEDmYIPWBsjaqBLM0YYQUxWQ+ont3E14U6ohmdagdJrmUOI7HVzlwXbjKH1E9u4mvCnLUU"
    "iy+GIGJsQZHRUsy/CxEH1G9u4mvCnVlRgdKnOSg1ADEoqGwzlENMQRZxBX1Ge3cTXhDkTg3J5QhMIuEiBVRF4hnkikZSNChX"
    "1G9u4mvDHshTU3bzKvKHKYCEpeICV0huaYCbHgr/AMUTcnatM2MMF1eIjSk2XnmWizzE6jHByYtH8gJx5nXynOG2qFBzc3Lg"
    "xTWWKfo57dxNeGOkpVOSnIYM5UCJhRjhh4G5Vm60bcrQXTVpejMOGAWGFLTaWaegLbMEi1teaLctAeaocjqhmLWX5oowUB5A"
    "Bzequfo97dxNeIPV7gMcp9/AQAleAu5bUrA1dvUsaLpkGXg0taLh1uiJnCOZYciqbtGlNXu2i0tzKwbS9syb1YRxkTE3jx/4"
    "tugwrDBdcpnjPyVYsc7oYNW3st9IPbuJrwp2XNTfBYTMz4E8ZdMsbJYuhTdFOl1LwaWtFw63REzhHMnz0ACKGKKwCkSxZq5e"
    "L5a8m8XZyYNN8f6UZTyCZsGlJlFuq+kXt3E14k9yvA1lb7bm07yKwad1KwfPHm1maebIsqa0N8ynPkZFJkXdpeL5a8m8XZyY"
    "NN/Sz27ia/xJ96GuRZjzMGxwqqkeFUnK2pGtKb1gopvM1ob5lOfIyKTIu7TehvkWZ8zBpMKrp9MPbuJr/Dn3oa5FmPMwbHCq"
    "qTWhrmU48jIsci6tLxfLXk3maeTIpqPo7iZaEBHNh8ztqhr6ae3cTXghvBKfUDNMZSqYwmecf+qkfmR2JhMmPBVg+ePNrF2c"
    "2DZcfnoVUUM23gFKNCzdw9sFSjGCbRkc0gNchGXlb7bk2byLyadV9LhwVNAZVdAc1i41WhQyXLku1Eu3Rmuxu4mvBCVk8isc"
    "gGs4pyWZboSiipQ9DVHec6Ls4Fn7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uNZe++5tOsGsCzd8LmdrAgb"
    "qjnLS26YVn7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7u"
    "P7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7u"
    "P7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uP7uN7fJymDRRo6XV5S"
    "6agyQFs4veMbpVl1a5COxu4mvBhBJaRROWEzrE+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+SesFopoB1XQF5"
    "Waga1czDq6sKZyJmcgqVaXOC1KLeRRbgLlzbeYcmsFzM4OSkcdCDQFrVCXV1bbVWs+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Se"
    "s+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Se"
    "s+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Se"
    "s+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Se"
    "s+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+Ses+SesLgRVpWult4z2d3E144IJbQCrzwGdZlTTeQ828hycZGG0AC809aKUUFD"
    "F1fK5c23mHJrBczODkpLlWavRowFBdF0F7c/UW7ia8YNwIugrXWi8ZlTTeQ828hycZGG0UFzK7atq203Rl5BP5jP+xf/AAR+"
    "xlwhua6ytHILWgoOR9SbuJrxYPQQi/bHITICx1aCG5ENtUXRaaty81j3tJybN6ClmEVTYqOENzXWVo5Ba0FByPqbdxNeLFDC"
    "lRAoKNBVbyRbAqiMEttVV5ZXOsfVG7ia/wBCG7ia/wBCG7ia77ADr9bLFHE13xQGBMSsrKysrKysrKysrKysrKysrKysrKys"
    "rKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKys"
    "rKysrKysrKysrKysrKysrKysrKysTyjcOJrvHf8AodL+ug75L4KZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTK"
    "ZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTK"
    "ZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZTKZaBX+llilrQfMHnF7o9DL/3r5n1ta1rWta1rWta1rWta1rWta1rWta1rWta1rWt"
    "a1rWta1rWta1rWta1rWta1rWta1rWta1rWta1rWta1rWta1rWta1rWta1rWta1jDkOnu3K3+T1+zz+dO+qOmHv8AxAHVdvX6"
    "5xqql8zqdfR/LnvU5Cafxv8A7v66bnS/pwn897+Z+X66/qv4n9B+O8/M/LxeykIDpT5ECARzgPPyOnfUtLSgiYkOgev/AL3G"
    "oshQiaxbXz7QleHPr9Of1X8T+g/Hefmfl4/0J/W9Z/SgAUJbXPnLoDNIuGNpzV8oqSzz3iaDKV+fxNKJ7wY4GhfoQmhOU/PP"
    "gMjtjev4xLmd0pnlUKPVeQxvrcd6x9DAI159MwHSUc9xNqqP/Y0Wpd30m4nA/wACINuiuh0x+Z0um+h/5Lhy+b+46OU/GIkV"
    "dSPVxKbwYxjpAZHD7XBD3JvUzFTm51c6/he/KOrDkbZTYJSOn7fS/wDVfxP6D8d5+Z+XidlqQ/KSYmgy48/55y+BTY84+Mbb"
    "Ta/mJsrXDFAF894xzazAiqlBuj56ypFocQdm66cVCqDoD14VN2b095W8oaDRh/jnKbOeac/K4Few58nrUaes5Bhuc3d/2w0d"
    "Osuil4bf++kzoVX4QNgTDzL9p1BJfPpBZEBV2W+W+fNYWlJf5ZzAJQQ1gty+eUZu+Z8//JTeTTf/AKS/l52fYGrx0/7CAFhT"
    "/PvDqUOhMF/2/b6X/qv4n9B+O8/M/L4fJlXWd87mXbor6i/qv4n9B+O8/M/L9df1X8T+g/Hefmfl47ZEKW6/yzjrP0B/VfxP"
    "6D8d5+Z+Xj6sSQ7hcj+KlKJ/jrBa2xmx11/EEqY6VHfxzhv45xHIgjtP2/Eag54gtuhFBnRCRjib5qoCF7HMRZLUufpN32Za"
    "h/8AXnKNvSNZXP0mPIq/5/cyjU5zY8zn2hVqsdJj+L0hCF5JVRza/MFxtIQrM5Y4ZZI75V+YInBcoLpf/ZeB0uBU1iv5/wDJ"
    "siFvyr/k/uMMzSBTh+5dhroRkuwVPsFOWGn+IBAmCIJ5ekXRB9JuvowBavX5lgBrHSITrS+Yr/qXJ6vUJbV8/MFzmT+opHli"
    "P+eWx5xA5VvpA6TN/wAS8r09IQheSUsKblQm5mysstWehHerMuThcxWagRRvzl0Rujp/jv6r+J/QfjvPzPy8UVnDzIGoYqU5"
    "uZVRcpYCAdeRESLvyIkBcERWxVBoeRFRzEApYNQywJ1itbywQIc5byImRL+RBAhzgqdEdIb5xYDpMi3uKoPKUZMxes6l6r+Y"
    "oqu+ArVzgYAwJXrAaGKNr5TRnWo5zFBXUVVsBKH8TdncDqnUECdYtWdQal4Z1vctXREyIIEOcGoYgo7mVUx1ctXROaZlkOhG"
    "+x/ojKNGWoOk2F73AHLMXrOogo7iK2WTolqLUXZcBUYGttxAV1FdsVS+X+N/qv4n9B+O8/M/L9df1X8T+g/Hefmfl+uv6r+J"
    "/QfjvPzPy8TLUUaVwBaOIWl+stnyliXNsRKlOILVLXULTapa6i+XBa0llCeaJTUsoTzs0uWuo0Ll+sqi51RRuBZctdRoXFDU"
    "aNMtYQLLhYuGZa0lrqCdS0taQhKaZ98vGjwNOcISmmWuoWm1S11L4nVc3q+AXwKJeBVorNy1h1hbMUXBNQSXL/43+q/if0H4"
    "7z8z8vEm3lAZL3BBCUCO5YCRSiKK+csKrlBCy4rlDDcVuIO38SgAuKC5iLG9QBUw5zdiKKiC6jtJEUVEI/eUItlLrlMBREdY"
    "YBdRRx5wBMyCH7zAUREpdylmYNiTF1flDDN2Dt/ERCi3N2NUVituKWRpH7ywhEVuA3GqKxW3B2/iUAFxQXMRY3qCYuUICjFZ"
    "V3EUjCgbSlAlhozCAyxsg2G9QaLTmgMXEIkTkj/jP6r+J/QfjvPzPy9jXeKu+8vFeBVd9u8V2lXf0B/VfxP6D8d5+Z+X66/q"
    "v4n9B+O8/M/L9df1X8T+g/HeG5bSP/froGvT84iq7AP67w8X4Pz+b+uq0uMn0P55+XesdLFpzjr3dH5yYIln1vZZ3ryP/fnJ"
    "ggW+r1evfICnUslX5av7emp8A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958"
    "A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958"
    "A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958"
    "A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958"
    "A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958A958"
    "A958A958A94Ul/6H/D3gtNH/AMq//8QALhEAAgECAwYGAwEBAQEAAAAAAAERMfAQIdEgQVFhceEwQJGhsfFQYIHBcJCA/9oA"
    "CAECAQE/EP8AwjSkhfu0Iajxkp/emo8VU/enn/wh+QU8PGk4SvPqeGISVg0hicqfJZ0bWRyFhkEeA0OBMK7Eqn4h18hSOUIS"
    "UIomQMTMEPDExAhJYhJQhoQhJeEhJQqkRVgJp5oQ0IQssaEoyBmQIWssThJAF14NIQhJYgTnCgPI43CRptS3lAaQUURUJjgQ"
    "kvAlJS8BCShNYmC6RNYmYzDw0QxdY1OBFWBujyglZQkiazENDwQ0Iqo0jFKc4hInWVQTBCBKcISV+HdfIUsKQ0SESYXmQ84F"
    "muUlFrAwWEKYl3InUGlED1QybIhwTGiRmAcaYzIICGzEbpuomQSZ6IgkDTDCgLAhIklvFgDRkE2azIRYRw3DJhjhoZUNHQCT"
    "IKiEqw82RSxo5hdkh5XCkN+FYbogsxKoZUQbolCDK6KRMpwiQCAURDyjwabZIQzZi5BZmEthpgxc34h18hSwpCzAc7QihOW6"
    "ko3UasGUiOrYA0SWDTFlsR8EIjIYksYSzIQswJjQJzSRSFUMO6TZyqI4+C0JwoYArke43R5EQoMIQhoY7MomKZQGYjjIlIks"
    "yoOZRKNCljmfdgpypwSzGcsTMRuioS4LA3RUKhSEhBLaKUKBnEx7jGDGxTUIYmYm8ygtqBWCZmxCSvw7r5Bo6rBpOolFBpOo"
    "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"
    "KeLr5jeWCdgSLFsLDYbJ8WCCBrBDIZLBDIZIhkMggjBIgkQyWCGQyCCBeVe9rdmJJvWvm3pkJCtacZ/3sU8XXxs5rem/Kw/K"
    "ySSSSSSSSSSSSSSSSSSSSST5hpNQzcz5br4iShwzkr31OSvfU5K99Tkr31OSvfU5K99Tkr31OSvfU5K99Tkr31OSvfU5K99T"
    "kr31OSvfU5K99Tkr31OSvfU5K99Tkr31OSvfU5K99Tkr31OSvfU5K99Tkr31N+nJX8k46hnqavF18ZseZjZXwVVFh/UBVSWZ"
    "VVVVVX//AP8A/wD/AP8A1VVVVVVVWUob6HK+hyvocr6HK+hyvocr6HK+hyvocr6HK+hyvocr6HK+hyvocr6HK+hyvocr6HK+"
    "hyvocr6HK+hyvocr6HK+gghIQlCrDxfiKn708vFTgTn94oNz/wAGAAAAAAAAAAAAAAAAAAABM/8AF4ZIkSJEiRIkSJEiRIkS"
    "JEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRIkSJEiRI"
    "kSJEiRIkSJEiRIkSJEiRIkSIfj1Eo/eGpGo8VLFEcbe5KWSMV1Ovu9yBiuh09nsMjia3NQ/3RqfHrRNHfoqvkKiJ3t56Ll8v"
    "PF0RG5vLVcvh5laJo79HVcsc+RZE5U+KpsFrJHn9gfjVNcEfu1FuG/DJOUuMkp5555UJrd2GbsKGuLP3Si3Hdit3ImJFEZk3"
    "IyAxviGRUDyEOIOGg1sxnimDVMJWhYR5m9yJ0mZtiSUEkhCHgkab4CaSV+suvjMIPNS/jSj4eE3M3RKrfIS3LnkW6eenHBBh"
    "5uX8Sc/KxqwDlAZYjQqjyjGzlBJRciThuOCV8gWWGTNcd5LMRKaW8hAh1KxVzbMm3McqU3iQ6Es0jN5IxZvf+suvjL4Zp0ye"
    "557lnOdHO7DLQpJJLLi4SrM7xxmwyzTP9SwXxzSpm97y3PKM6Kd+KSVBpOpCiDcQQmYGk6jSdSFUhTIklQSSoQpnYhTMYQko"
    "gSKIaTqQpkhTIklT9bdfHWFQU4OCrDXLdxhJErFlx7E/whYs+Hcj+jwqSvFxVYS5b+MNrzM+NP6c6/8AB3Xxm4M+JnxM+Jnx"
    "M+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM+JnxM"
    "+JnxHPEl8SXxJfEl8SXxJfEl8SXxJfEz4kPiQ+JD4kPiRzIfEz4kviS+JL4kviS+JL4kviS+J/T+n9P6Z8TPiZ8TPiZ8TPiZ"
    "8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8TPiZ8RN4uv"
    "jb35eCCCPHnYfkGT4i8hBBBHmXlni6+NvfllsvxX48kk/nTpi6+NvflltQvEjx5JJJJJ8rJJOzQWOTpT9X6LMtp6F9PQvp6F"
    "9PQvp6F9PQvp6F9PQvp6F9PQvp6F9PQvp6F9PQvp6F9PQvp6F9PQvp6F9PQvp6DWHzXyJpJVMXTF18be/LLyT8+2kl0KHHrl"
    "2E0kqnjTgsJUQ9/a+GZKCDpx9OBZHcujuXR3Lo7l0dy6O5dHcujuXR3Lo7l0dy6O5dHcujuXR3Lo7l0dy6O5dHcujuXR3ElW"
    "fSP9ZRcpzV28twpVJ4OmLr429+FJJPhySThP4xzRE76k43XsenuMnB8y1RUnPW0xrL0dO5Q49cuwmklU8NYSKkNKtt+79CSd"
    "iSSSSSSSSSSSScFsMbzMX6fI+F7m1/v+4OmLr4297ckk7E+KN+cgknCSSScJJJ8GYjc+vrUnG79y19ycbr2PT3GTg+ZaoqTn"
    "raY1l6OncoUeuXYQkqhHgvn/AA9mfJ3HJlxyWDpi6+Nve09jIyMjIyI/C57WRkZCKkEeNMRufX1qTjd+5a+5PNl7Hp7jBwcN"
    "61WRWnPW0zQencocXzy7CRJTywknBM+f8PYfjzs3HJlxyWDpi6+NvezOEjZHcLGLCTwnzTwe0iDJErBlGg8QilYsKfINkrE2"
    "/X1qSbd+6/UzR0/m/wCjdcHzLVFWc9bTGv8AHTuUePXLsJpJVMHtK4NeqgfwKj49764PBjwdSSfAgSxkuOTLjksHTF18be9p"
    "kDEMlEEFAoQ/MQ1gx4PamB4F4EkvZkQXGJRBEeK2JSJEMiHJLQOfX1qT7Z+5a+5ON17Hp7jJwfMtUZE3K52ngm2lVEL9nvxX"
    "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"
    "RP1tf8Fpe5qXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noX"
    "E9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4n"
    "oXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C"
    "4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE9C4noXE"
    "9BWGd8R+4l7u8zePCli6+RFgfzkfsXx3L47l8dy+O5fHcvjuXx3L47lsdyH17l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3"
    "L47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L"
    "47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L4"
    "7l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L47l8dy+O5fHcvjuXx3L47l8dzNHn2v1EklCN48KW"
    "Lr5EQpspb3DdaWfxfUuNalxrUuNalxrUuNalhrUsNal5rUvNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUu"
    "NalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxr"
    "UuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNal"
    "xrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuNalxrUuN"
    "alxrUuNajXFXxoLVpqGjePCli6+REOanGFfUSNvQJTI/Yz0iUxr+L9vo3jwpYuvkRDs6jrsvF5BNGaOv7F8b4RvHhSxdfIiH"
    "Z1HXCcVDEM84qJEyhInyaKZUp4jWQk08/wBg+N8I3jwpYuvkRDs6jrglmPMjm6DMm3AnzFGYxyORAhwUDTyMSz/X/jfCN48K"
    "WLr5EQ7Oo64McGUjAYmqjSCmw0htEyJa3wJtghx+wo+N8I3jwpYuvkRDs6jrgnKBCg1sSss30FRJSNaEqHISxhuHkJ5DJsb/"
    "AF743wjePCli6+REOzqOuEikwEwkOQzEjKCVmJkMqWiRpjOQyHhv/XPjfCN48KWLr5EQ7Oo64JKjwmeQqrISgUxMJTkKAiFk"
    "JZQFA1hv/XPjfCN48KWLr5Is/uDGUBYUhJZiKohnIkKCQmhW80KqEOQ5tjlIU0ljv25JJJJJJ83JJOzME/jPjfCIJnCli6+S"
    "LP7gjLNYLJnI1UYklMY3mPWQ9h8DZoZOZmzZJHajZjCCCCPHgjZjZiSCCMYIIIFhH4n43whiwpYuvkiz+jwTECclvDGj3k5I"
    "xotW1uFCQVJLdgDLxkSeexGECRA0JDWQkhxhQgggjGNh7C2oyIRCGsyCNh4rGBxP4pHxvhDFhSxdfJFn9GMSyGSj1NsZlVBQ"
    "5CpEFpbxZoTG1vG0BLD2Ezso3CqKBiyQ6YNokkgdxWS3ZunagkOfgiRkQMm8iV8MGLBtbx4rBxs54LB7EjxVCcEb8J/EI+N8"
    "IYsKWLr5Is/ox4bx02FNWUMVDPHJkNRWUY8kOQlGxOEkk4SSJkkkiKXgTFJUwQ6zFukiZJZLJZJE40RjJOEkkiZJJJJJOxJJ"
    "JJJJP4ig+N8LYpYuvkiz+jQ0MgfAzFMqRPXBQOG1DzFAnxlmfUyPIve7kcSk3O96mZ5n7XcEilNxxgfxtzBOCZ/QIFQ+N8LY"
    "pYuvkiz+laEDWG8zwyGSZMgTBJHiZc2fAyPIve7kfQX9ZmWZe7Fo247XgQQQR+hfG+FsUsXXyRZ/cWQQZkEYQQN4rwsibPgZ"
    "FkXvMyiFxZmWZe79OS2PjfC2KWLr5Is/uCwqRgkSIIIIIIIIIIIIIIMimXwV9zKlkuWv0ZtELi77GaRL4sgggggggggggggg"
    "gggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggjBAjZ+N8LYpYuvkiz++Vy6r4K+/Iy6YXBX2"
    "5GZ5HOvoUhS+L/VvjfC2KWLr5IRubPOf97CwyU8I8hkeZyp6jiT/AIrzM+yr3awZumfF187H5ptJSx+qfaf92KWLr5IgHkQd"
    "KOTv4OUvfU5S99TlL31OUvfU5S99TlL31OUvfU5S99TlL31OUvfU5S99TlL31OUvfU5S99TlL31OUvfUydsuCpglyuuc/Jyl"
    "76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+p"
    "yl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyF76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe"
    "+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqc"
    "pe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXvqcpe+pyl76nKXv"
    "qcpe+pyl76nKXvqcpe+pyl76n+VQ7jvjYpYuvkxDCSjkfRHI+iOR9Ecj6I5H0RyPojkfRHI+iOV9Ecr6I5H0RyPohrUtfRGW"
    "lvoo73mNkVnwS0M0eOSrp8kQSPrmcr6I5X0RyvojlfRHK+iOV9Ecj6I5H0RyPoh8L6I5X0RyvojlfRHK+iOV9Ecr6I5H0RyP"
    "ojkfRHI+iOV9Ecr6I5X0RyvojlfRC4H0RyPojkfRHI+iOV9Ecr6I5X0RyPojkfRHI+iFwPojkfRHI+iHwvojlfRHK+iOV9Ec"
    "r6I5X0RyvojlfRHK+iOV9Ecr6I5X0RyvojlfRHK+iOV9ELgfRHI+iOR9Ecj6I5X0RyvojlfRHK+iOV9Ecj6I5X0RyvojlfRH"
    "K+iOV9Ecr6I5X0RyPojkfRHI+iOR9Ecj6I5H0Q+B9Ecj6I5H0RyPojkfRHI+iOR9Ecj6I5H0RyPojkfRHI+iOR9EJYWNmli6"
    "+XFtoZIRliTzdNfgcpjfLsjPGjkq6fIohIxggjYkkkqQQRsQQQQQR4EEEEkki/FPyVLF18wSSSSI5eDLEnm6a/A4Ny1/xG/P"
    "4v8AXp6kGYWxQkknYeKcEkkz4UkkiexJkTsL8U/JUsXXzYwQGt7/AM/z4FIqWv0IXL/xXfEgjC2nsz4C8VeEtuSdmSSScJwk"
    "kknGdidicZJJ8rSxdfMEEEHsfv8AglhIRBGK2IIIIIIwggggggggjZjCCMIIIII2III/OUsXX9JH+bpYuvlxfh2T4T/N0sXX"
    "xszb4eFJJO3JJJOEkkkk4SSSSSSSThOE4ySSTisJJJJJJEP82mcvF18ZyGhu8yZMmTJkyZMmTJ7FUyZMmTJkyZMmTJkyZMmT"
    "JkyZMmTJkyZMmTJkyZMnjUyZMmTJk8KZMmTJkyZMmTJkyZMmTJkyZMmTJkyZMmTJkyZMmTJkyZMmTJkyZMmTJkyZMmTJkyZM"
    "mJ94ojxdfEVP+Dpx+9N+MnGCUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSi"
    "USiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSiUSi"
    "USiUSiUQG5/4srLvHvUHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1Dq"
    "HUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h"
    "1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQ6h1DqHUOodQVMxbw8n40PKrEo/eGpyZPmqvFaV/z96bkk3f8AB50ZT6ePM2tEMlvQ"
    "SR/jRMqsW7HgIVsMqDhCNKG5/XHRlPp49UKR7bUZ5iEQk8e2SkapGInIhBE42y3GeVysMl75POoKWmmIU1xEbQZp5oyFViZE"
    "8xu9sQtsPbpqCQmTfEclvCSobeY2bdUKkc2TqUHSxFGcIZKzw/1d0ZT6ePI2pDkpjSJQgKpCpYQ6FVDNVDT1RZijJSMplVpG"
    "CJrMThViFkFSsjlTqQighEIm4cEl/RLkHVUkiZNLNkuWOSmLcwlAr+ipO8REFI6bqZ0keXOgbkX6u6Mp9PwdLCA/Yjoyn0/4"
    "NOjKfT/g06Mp9P8Ag06Mp9P+DToyn0/4NOjKfT/g06Mp9PHkpcDh0DQnY1DgUifElnyJGpKshqCWXMSUMlMCkVQSmBvlglLR"
    "JtInvGpQSbSOdlEySmBwUk+JEKRZJGyqJSpJzA4KRsnA4OGTlISlSJ2pFmSlolMCZ0JEpaEWocM6yY4PA47xFqHDJTApFUEp"
    "gnlBxSVxOCTeBsiYlEhtm2SlLiKWY2UiZxzEzQn+NdGU+nj0atwizU1E0mkQEh5tkpJobUIlNvmSlEbhNKVI0iRcw0uUTV/C"
    "Eksxwm86jSU5oIUDTj7FbIQoHCYHlmiEKBk0+pCaSyExuJSTSGnEgSzgbTUcxCGcE0fUlJNIhUhMkJWYoGjKYnkLJlbJq/gg"
    "NGkrY8htjSkbUoyafUlGlwGkRIiHmPIbY0pJq/hCSWY4TedRpKc0E1lngCSDJFEyMoaYoSciEEhNHBmTSTZByhOSc0E4SN4S"
    "ZSNGmiOa5D/GOjKfTyFTxG26+JOUeRbbrtzlG023X9AdGU+n/Bp0ZT6f8GnRlPp4iRlw/esiEhF4mccf3rIty8VEkMZk/eCT"
    "eApY8ao3ZrIn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0"
    "J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0"
    "J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0"
    "J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/Qn9Cf0J/QS70iSWS/8q//EAC4QAQACAgECBQQCAwEBAQEBAAEAESExQVFhEHHR8PGBkaGx"
    "IEAwYMFQ4XCQgP/aAAgBAQABPxD/APhGk1g2pc7eucAABQaD/dalVdcZcjZvk8/8yU3QysMhQ0f7wgESx2Mq+wdu3+Xbrf2f"
    "71n+lMef+UAAFBoP97AIKBUHn/QEG54U2AUp5TxjRtrK5gS0J5h/fEG54U2AUp5SMPa8WQoC7VjPmeAhWuMqxwVJnJTzFHxk"
    "rVkgpaqw8v6Qat4XsfnH1LV/y6L7cK/Iee+is9t/tZBdca8AEy01ugAW6PMaP8Gvlb2IIB8lj3uUOroCXtcRAA1tSGREqn+B"
    "VYrTNUaLwL/5H5/9/wBBECiFE48DaBOqShCoGkX0We7dpZEE84BAfcHFxzWRGNp5GldoiCImEeJYLsuuKBdPN4cQlgnYVgAX"
    "PKQnqCutDXOqWalsZrQtaI5CuXWUIgjvsJTBdhUstC4Ne2P2gRYwpZTW/CMwslXa0YBsZHe4ehvcCnRmFVcCwIKK6aAXTWSp"
    "oalUzgxOg2/2RpCmJHCPakdIiYY0B8q0ywFGlmtmZyr8wYS33hx9Y7m16A2QwdjNy0yorXwoBdnmYYUTyd60UBK1al0XaW+0"
    "yICwU5cAUOYtTtxdK0J5h5SpM/w1j5+3b3xEDuA5Wtr5LrZuG5+kSaAGVXFS0OK1fAE3TTTMYWbtLEu6ujulzotxHdx2rxoy"
    "cupWC2pDIjx4IICw6Ap+SW+nPAWfxlSJe3XiyoL7C12GO+BqOdXstdavh5POtQUsA8eEFmTGYbpOzG70KQoBdNXTT8YgUrdO"
    "ILTQtU0NMpcE70UXDQwi4yQOgtArC1MobQhk55TL6W5fsiCzDFcgFaos2tUXFSCycvS7f7IRfQ/sKq4S9mzkIT1FsDInbRmh"
    "VmqhTl4DJYSjADLD8+KhDoXDVyoOtHe1di3gWumGGoHCwZaufZK8DEbOQFI9Hwt3WOpJfGId0j80i0UHGar9UZBfGsaUeaC7"
    "qHF1HEeS6yXWzcpZ2g0Qrwjs9CYRhyvAhHSgF00baaGohtLEdF0N7Ht3SettupU+cIB2KRiEtKhBY8iloBZZSWz40bjtAiFt"
    "2NGYhuqUrYKuFpaWmahwmUFlqAowYJhbLwPmjuYoqclTmrI0iZee28OUNGWgXxyaueQmkEsU+sLEEo8BduyirEZl7KgCknKu"
    "Sl7iObha2uDmAss5CjmrjJWK/Bugi9ht4uXUTU2LMoSuW6hawG0EQVopZvmopVXXLA1xuxm//H/P/v8AofhvEoKUl+KdQeui"
    "WnVoFdQploq7Gimq0UmhBLayDDklglQLpVpQijPVyFwIJnN0s9UK1GWudeAyuVqwDRsXVTd6buVcRta3TDSPkBgTPn0rDp5p"
    "9thFXcs2XLVoP18JLMWMoKbNyu6mfCl0QgLdYjp4lPXJW0ShUWnY0mYcTDqWpeqV30pwR/ylHzglbIDBa6ZhujMiAR1chCsx"
    "XkvBTqT10Sot0b6oArlWC3JkgWTsdPFNigWDbF5e7KvVkPxLJnD6wEA+S+cNANngCzGDsZvwmakZsPjOGgXaxmqFGyC2hZ5Q"
    "WeW42FtLBdDUCxdxlAlV4nFlJkrMWWVcogGCshe6R4PdOiXUSZGWNzUzJRRToqF3Ud8spsJbb9vRIEKPLpTOFXR2WQa3bmU8"
    "DfIoMUrK3FPNiXlBugLxQJW9tk6JeGmqHlh9hFPsImQimybLipu6pKAeCyHc6SuMPbGo2FjowVoNUPO+Wn4qEuEdxGFAeV3s"
    "OOg9GoIbJhRrqC1hTBmWvK8F0P1AMdeRExNfMpG4ljYTooyYjLUADsLLGrFKRESDiblqVNtkcF5pSoFfQmfr4JmzAINrbL7U"
    "HuIZew5Hc8PxTbEIjNrbXRovYnt3SXC1/M0ny7UOXFAAZzdYVBiZXhgyiFRzZTLLsQ7gJ2nt3SVgvE+hS2BkZXe6hEeY1K+y"
    "/g8FGBK02K1A7q+WXLyj4EpbWa36tdoONP76kdcDspmMWAt8w26cK8sEyTrPVEiMUcurWBSaF1pLyQfTwLnWhjdz9g8mY+Hr"
    "dHAIJkW1w2k5MbPbk5QKNA4tCrsg9hqGUROeq24S1c7oAbTACnFaUioCn7y2o2pdYTjw2VgltvnOZR4+wpFFpVwV/wCP+f8A"
    "3/Q/DeJQN8W9oyC9EK+sbxdakvWTmCU3zVMy1GHbfAMQLV40DWcSvhlOWvHCbwTDKGBppteAH9ETFSm+grRFPudWFgYdkFXU"
    "7wMf2ByGUox2IZUiDBYuFaAEfi1xFHUZADyi2lOtS6v18JIKYGwCu6S4MrNo0Ci24qZGmLsFc9jS2qUpooVpcQsgwlNBYAPB"
    "cMUt/wDwACwHCq2QK0FsBVB0QgS2xbVgSgcfeqJX7gn1jb8pYKWhplDoUrJLlYkYIpYLPLA6jV1tYhETwxxAcSxooWR2sFLA"
    "CGtgJw86eau1nM/kRndd6Xm+GZQcWJlMejbV4JU+VvrE+Y8clg3SeVdhonunRNoxq1YPITJj9tUI+tk8PAmlUuwbjlGI3hAr"
    "UqYsi1ZCqrK26KPSF2rYAAWCssG4vIDK7B9q+Corm8UZlY6LF0uAchALpTUZjAQLAAUgPkyyJNeXF1RISgoaxceILAAgQ6SU"
    "6FjFRo1Irrqou3QswCwhFhZkotrebwmTDAFAnmm4axWollVCSksMrHmjgLgprp8SoYbK8IMK20aqJTqmIh+SH0lQU7UknLhy"
    "Mu9xCBRoAtWXpGMKVkboJTFeXwDGuCIijAABFk3XfDojFSIWEnvHXKLk1jgtxLpGksNKOUbWSWtVWuKO5Pbunh0fk+AUWfnT"
    "V+ErTtB4BCnAvJE+Uoiha4uzx7C0HQJSvdcTM5a0FsDVWFxUPArUecHIdyOVJlVa+B5vXmWijWXiDhxAGJWgiqrRVZsJ8YIH"
    "IYANMLhYWmug2qpMtEADejYHv/dD2ACBK3pRdhAP7LHMUDyF5lmKNKJOCrTZvgNzAz+cKAULaF9//H/P/v8AoY41SGqgC1i3"
    "wT4ayiG4FsGtWHSKq6oXtZV3WAlylSdFRjuQLz91Lqm9R+4Yp5Qm68CxMhVyoUF5a5U04q5h9d8Yy3CiNWL1gAeGwKC6oGDO"
    "InQi4F2QQrcVWZTF4feiZDygHxJRYQcaeS4yo0/wWFVALcAHEW4Y4WhQlmi66RfDWBPUFI+UBNFkJ0Wn48cjf51au2NNGL4i"
    "JPF6gAqaADLoJTTjtOWlcDJ7vJ3UuqJ4OlEisC2StWHSNZo/btZU9VgFZ/aieDwVvO4kCUDFbSYYL1b1iFgplm1HKrlWUZix"
    "wNllNKpRuH8UzG0iZGMnWxGOlq1FUqquVfB9uK6aWTjDGTWPB1W2aSrLlGjB0IFWgnC1KmFtXq3rHHyVdWhlVVV3Ge1T1dUK"
    "5hIoIfJazLDRj8yVx8MW312uqI0sppTpaMBpQQrTWGIZCDgDIiaSIQYastUoK8oqt0skR5BIUYFIvRgRBDY1uiqutRvbXwhl"
    "UKVChq2CuaKXuhst58ncXay9kEgBKsoOikrwMT9IE2ImRHNxmsGQilCApyeAbDLhLBAWLavrDxWOxUIS7S7bdWP/ABJ2p2ry"
    "xjWWJPUCJ9IeHrKZ0un4iQJQMVtJhgvVvWIfMvaOVXlnXK9QpywQowNEVLjWQlIoAbMgD4AjUBbVlY1FFUUeFZdNPulozmE/"
    "4dY8R19O70NB2CYFupDzWeGIZvkpIJAowiHtayjwHB+v7UF/WPHW3CqrUrg5f/H/AD/7/wBAREpr9bVkFVoLVYt9huTdnR5H"
    "D/o35/8Af/4P+f8A3/QxUOLslgOYu5c0x3kUqDoElsLTXEqSHIwBZsClPJV1KP25wslIH6MHppYsHWhYKdDSXrQNTQFbT3iz"
    "U0MNbj7FMTtiOR7Q2mAMh0i/ZEjxHcYNC00LmvDWXM8rWrlHfMpW7G4Bz9y6vvUJBSget2w/MNQYFUdAdZg5CgJaLkoWi8DH"
    "ooNgLVXECArhMEOnU3Ycqdw0kCGB0oDLCXUXrdoJdz+lrzhMhMhbioJNtUqzQbF0CeqMt0pG6rlWCFpaYjCTsHlGlsHF3iHH"
    "Q6BVCwRbMVzK4qZwwFISxEs0j/G8GFx+5TlRih2mnYtxRQKAAlaGbtyiAWt2oQi6AdUKKDYBXGKYm+zAJpEciPEHjhd1QACi"
    "jnWH+ShYYNEvS2a1Q4sVUu0FgjFUStArRoZnU5lk6KBBPHfQ0gxO5M8Pj7oEOeMZi56pmuggMtOfpwm9KYMXeTrFi0LYoKFa"
    "FM9yCGUsrjrQs7+VPFyWadHDHooNgLVXECBPEjGwYqmDV04m0VToBaKqoOVWQ81VT/esQ/MJeVmZ3C1CqlQpAGmRSirbgpF1"
    "Cz0D0WF3iI+zBJC1ZQFpS0wQU0TeIaOVQQq6WRnVC1M1KuOpQRSxL7MJjRUgVULQCvQJwH6Zl0C0Aq8BBDKWVx1oWd/Kni5L"
    "NOjhj0UGwFqriBCxhZGdULUVOlecgAkGLSODqrU/9L8/+/6HtXRKkbw1oqIGF53coZAYUE1hDA7pvcoR7AIrxpu2Oh0IWNpB"
    "FCRkSyM56sWwqdWyE0mTo9I0SZRiFggYHV02lVa0WEKabKOTEQO1gOUgLo4VLjY4KNzKUaFQl0XdHgGBTz/AbaujoARBdfV0"
    "mxwrBegdIr506q1pcQztu5VNsAmRrhS3xbrEBbMUQDLdBWVdwnMREIQO9DGeIW/WYSuARWrals0lMc1qmXeRhtWqqIHXQ6iw"
    "DAaZvpM2osXnDgtJ7t8zCxk7D+dAQAJpjASXOhF9SrMWtwSpVBy7HdhBbLpVAZbmsC1nOy+aFiSyIUcqqz/jR89iurvjwv8A"
    "2Wi+6cHgyhhR+SAKrperIdC6LELtAB5XtiddmEoobijazyQukEPmWQizYzZWIc0C/wB5ovoROBbtLqn1Xw/5pKPUXdVXfwv4"
    "0KwL97Gs3NHhzgSLexdvYg5Tj49pA8gfXuFkBoydDokIGOsD2mnHYXs5nbDJ6JukW0mrjcQg2jtkdbwL1lZZbsLlaKgznyi+"
    "b+pPOwP6pfPgLLXl/JJUxaURC1oFWjemKqO+wKHREC2qS4TmIiEIHehjPESjqCpo7hg3lQaqGjaZAFqucl1uFR9Vdu/IYN85"
    "ilYoi/VMEAe4JibVjIBAotfRN+DNdyXBgDlVCYs1Csad2V1TWSVVjp4q4K5YAQMXUAHOcI7RivfKZbcISlIOAL6FspFIUTbR"
    "QDOV8VKmLSiIWtAq0b0xVR32BQ6IgW1SXCcxEQhA70MZ4goovhYFDhALizoU/eU3KduTgLdZ6sdlYYvcUxWsA76zJKlogXkX"
    "h6V/6P5/9/0CRYqHmShp1QY3cDgbEFAEXQvO4SaziKqIrHS2bwwACzaKqJRrS2bwwXixABQLDRRaNHQjphFBZSi1pxaea2xE"
    "ljMBLpbtaUdiO4FUK5RVu2pRjRA/U5hJVlU27aMlixfg64eaq24HA2IKAIuhedxMUYGTvT9AyvniFR721qDVdYoBeEcy2rIY"
    "4FNmjCvW7Ca4St0LCUaCGdQ32Km/ShpMUGN3H1thaO7KAFGERiWiaTdHSoKqrRnEPy4QDhQM4WY90C4ujSBQAgUcFRapYwwE"
    "RQUASqDREQYCO98AWq7HGqgC6ryHYnQEThKIPIW3sbBayqq44AA1taBThGlc7vWv4iZnNc+qsF1QGWNUkZRUeK1FYMtnaGtw"
    "LEUo1BVAekWFD5tYuhdA7Lbzdqm4BFcShGpl2mQfqBIYUqtauFBKVDQdSOAtwcfwbq5s7ZMAKrszqI2cqLV6scDWNgDChLQ5"
    "uaL8liKtcqp7wSFmkVURRrS2bwwQqgnbhwlGHoOwi0SWRtpQfNA61A4DnHSkUGMXd5iVFAmORvYl2V6xMSgyXUAJRix4JUa9"
    "IAAjQDAWtVVWG+xU36UNJigxu48wODIFyBRABq7QZnRVytQJYBQGFu5agk4hRCKboTr1pHIRRblGztlgmOshAgAogCoAFQ2S"
    "KT3HeYvrAF4oNI4KQpLQBYdiioUD1i2XsYTDmI6jKHgAxu3MQd1oPRaryWlHbwBC2TiWpF0qzvrMSgyXUAJRix4JUa9IAAjQ"
    "DAWtVVWG+xU36UNJigxu5zeyHOMseQay4yyjS8u+2gatyra4zggpShegNYooUaom7Yh7rXGDLox/6P5/9/8AgkngnFmRqzLV"
    "W5f9m/P/AL/yYzqg1/veM6stf5DRvIY4/wB6NrVhMff/ACmUoaZ0CN/9f7xvQuuqIntfx/mFQjSaSXtQfZhSWbzVT4E9Z8Ce"
    "s+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9"
    "Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6"
    "z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1"
    "nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnr"
    "PgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8Ces+BPWfAnrPgT1nwJ6z4E9Z8CesrhPr"
    "dxylrb//AIdBQBa6CZ/D1wnyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX"
    "0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIv"
    "pPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+Rf"
    "SfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+"
    "k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9"
    "J8i+k+RfSfIvpPkX0nyL6T5F9J8i+kCsDzojAUcJ/m3hPWBNZNr/AHinutJsjPNyPU/yiLO+nHjcbK6rFtDLgX6TiI8HLeqa"
    "1zWyrzOYjwcNbpvfF6brEqNldXiy1kwj9f8AdDxrC8M/5QIFAUeFYPbxS26BQFq1gFjoogsdpS92BQaKNtr4GigC1y0L3Ytp"
    "pt00l4PaxCmrAVFI1kR8f2HYwNvl9DOPoQz8fN73H/T5Z2csqtq9v+X8y5qebHGvxM/tbWLdo1SfnMA+4jHbX+I2BRXpg2Nn"
    "K0L2EDHCXbhlhNOLdLLP+qEQUCD7/wCb2Tfeee08nfhgptMrCjgBgOS0n3KR4NjV1q3w9k33nnsPN14o3UCL5y6yXWzcWzW1"
    "Imj1jOYfR9ivfkoE5VwNkAOFFWCCKwDWHRfzb1OUEG7Notq7jnrVXGzgai1YQaSwG8cq7BpsLXBqSXGl0KMESbqQqoNkeJP1"
    "M17Sme5CLONIbBilNxV3mDAeW39rCi8gE9YP88Erg6lAAsAC3fhvq+GfJlhWateYMCtp3Osg8Vd9KsAomGSCwW5DFVQ5mDhX"
    "GKiBc8pGCxaZVQ2yQDDuWVoG8t4oAsZHSIwOPthhwNVXotRwSGKlGIFZhZMuKRGdbtgBavgBD/8AmxilWmAAGyEE+Tq0IB0N"
    "Qs1SMCggsKJyvBdhuhihChrawwBVWNBCXzqm1UiVKolZiEtmz31aFJwicK1Kr3B1RAq1LRVdGlaCQQSLqwrpVWu2IE1ANIEa"
    "MKORZDbNnvq0KThE4VqVXuDqiBVqWiq6NKt0Xf0LQuQ08nUgxoBrcoPEq0HPn/p/5/8Af+a+7iNwYr1l+z3PBeEq7aNjwXQF"
    "9ie0dfhXdxG5MV6w/d7P8MHuXSWIAIgFxyWJsM90iQTW3ClNAq1dd8XXTawnS0F9SXKEwNaoMW1AJcGxdhUgKnM1V1dLcnG9"
    "y7NAvuEGpexo+cWPy8JGj3ayVF8hYcYG0WdB4MA5VVfA94LCA060y4lUxRA+NWFX5FCjqiPgHRUfUvkKAXGdrLItvM1V1isE"
    "gMitQ7NtBg1dIOpJtBZiFliuynB+hPb+uflIxj0Y1RVJDTLdYsmdRfpJEI1ZAr5URFGlhAIZABHAYEHNQUoHJi0FpYnumAKI"
    "l8mDG01P8CQtQOAAHABBp5YW/ByGhp3ZYq1wyHNNitoh1uAgT3TAFES+TBjaan+BIWoHAADgAnA+QQNUBmrVBzgevlPKImmQ"
    "ijWXj/p/5/8Af+Z6tXYDej0ByYTmg8DbPKLKTAoXnlIYuFgKEAKXnhfA6t1YDeh0Ay4Di08HkiBWp0BKfTOEgX4FMFNOkKA7"
    "Akt2xprJcZGHToqlyKq3bbGmHAkIlzgOVWHSLGZkBWzyHLZmYSHFtoYUHQSl1YTSROTYILB4Z9aksh5hpYXDOJ1ZvM/K1zh9"
    "xmKojISzAUFAwDiMUswkqy5RowdCWMt4Wlrg2XNXl/gLQx6AgqLQYDbrMT5WhVahWXIxsWEQQI1sAA3iiBLoDGlhmpGksUja"
    "LKVtqSpeVuM3W1G6LYu1R7ikoFAcQTZcQy6xJKZRVVyrOFhHdxEp2ZbKF0OqFXzimEsderQr+scJFpP1Qr9YAbQZtAhTgUKM"
    "QDnCBqpGzhsdHSW2fV21GC2l1tXma8n3TVcoANmQzADaDNoEKcChRiAc4QNVI2cNjo6f6j+f/f8AnFBwVhRQqAUlNIOGhHGw"
    "ZFrvVODi6xeyc7BkUqt05OavNaYKDgqCmxFEsKLUMtLxe/8ATAaslqUrCji+3YCyjHq+CMsIQUdVCPm2ZAlDuTQG4AcFF9OQ"
    "wNC2XEsCpjX1DauQS3EQLkvhUU6nNlI3ViRfIoigCxk3TI1syoUgMAplLslXiz8jECgoLeElCd3qSVRQprNVHl+EVFiDYBGX"
    "NkJycFga9KdKFlEMOJqwChdTpXsKUlFNLPhjwVgueKKKMBGwYDQZ5jO0WjkWJZEQES2xFFGw46EmHBqqLxk2PociAKAALFuM"
    "omj1kJOqKptMMcSZMQ2MgdNBifO/GyZmBLLRsRi/G1XuEiCM1eMTc+JmXm5tja3eUVenwJKCzUpfNeA4uhZAgAZgVVBtBctQ"
    "5YqioYBaU4DcB1urIKUnG24KUglZQotQm0Y10bghHfsqlAiBtbQDLmglYkExRvSlQKTK2DMghNNiwIpu6yYHAIGxaLObgLd0"
    "VAJEYg45HqhMiQKBoh8sULUN1xVxY3CYjBA0tLRu4nZ2XPbZAtVDh1kJAWkRaF5EzfDbKxfJQEe0VhY2wi8qiUntra6HP+l/"
    "n/3/AFe5l+sg1fGow/1BQGRCAFUvUxnovFt7rMFKbsageb4HwFCAIGF1ty015hTuacMamkvFrwX4NmpJGlgGsrZZfAVYCEdA"
    "cHBcqADcEWNVFUBjJVJtqBpaW4KctmgIy4RbD21N8qxfUloBRqDOulvYNa5F9x5QAwBUAqxstAg1s9Fw38+hW5hCZeo42hu9"
    "0AULrLMKI6KiUeQOpoB668Y07WQ8M2LJnmcdO8POd2VhWcJrwVStXkXDk1cYSNEUqAmrorYKWy7hcBOKnUvLmqmuu7zwmZaA"
    "lauKfEkGzGeVQEAoIT3JRy+w5dYq7zqFzyYbC7ldqAurQjcQc10K4pBBEMmWUjwQ64gC5SrUyQVCNJpJaKEdMMhzAWinO03c"
    "sv8Akpvyl1ncODh2xOso5wNtFZX72yR2Nttimh3M0Tx3EMrgoAd83eFIN961chym8A1GSnURchRYXbp0ABLGGPfSSLOzFGoY"
    "J8zWRSDCN3AUlAi5CjBdunQAEczY5QWgQEBBENcR7xhAlmvaqWVtWpbCIaYlIaC7M2z/AKX+f/f+ZUdFSB4qq9SVzAS4F94E"
    "uLs1fkR8PftKTab0RhUfNuD7CHi2w5DJYCtBB91GuHwZrWy507qidGUfB5cQnGycSnEkDqoJF3io3BohRfO4JBEJav5AloXm"
    "hLiyuTBATKgIXeQftD99xH1OFIm6eEbY5I/OR6VR+1l0mXInKrkzamznk8/FG04ujOebcSTOSbR1JmQ3RgcSYmp9NKXRujU0"
    "boFKasn/ABDIZbfazQ/EPEYVRDDI5dpGDUGlzJgoSadyZDEG0tkwaYncWCN44vZxaRyx4MDljcmHEZUElEJBHxQogI3EZ4ix"
    "lPKCpu+pPeLXdtCO9VB6ayJDN529aJN/yknZbLgmM2pdptlrfgioWwYCBcx2b9aYGEbuPqxe9s+pVAfuDQA0tO1sCz5RKtcS"
    "urTdn1AsYYuwH2D8R4PMlPeAhZtXWkRyaSvpDUby9aMgHagfW09nf8ns7/k93f8AJ7u/5Pd3/J7u/wCT3d/yJ7Lz9KEQiz27"
    "CTE1sq34mKFE14V4jb+mZ1UJKmco99+J7u/5PZX/ACeyv+T2V/yeyv8Ak9lf8ns7/kVJrZbVMW6rFWxpJgvWqkR0gFFtwNIg"
    "2rymxdpQERtG7p8fz/7/AMxKivHqG4O6vUXwUWMtCruNnaVeULeZXFwrQUcy0iW83MpeLPEZ8kr4lDcc8mJQKybXoRNc6F0P"
    "KUNKHpMFDJU6hRhl4MY9LV1GYhfIQ7MqyVSlUyzlIAZpHSA7KFAXSAlY3dQAqoJKQFxHSN2UlekIAzKLiEoy1FVOrDH4so6B"
    "GVbbb9eY+ULcA7jcAVsoQIBqJQEtQqiJSUqAcSxDyo05i2APMq7xqYYWhmhII9s3idWQXrWIy1WEcC+INrnz8QTZiyKUde8N"
    "BbiKxhNhhayLK7owL1+OseIToCdIYDI1uOELlHE8FwDlkN9cxLkhgwnWMXqAFPlE0gC0Mqm02GpLBEF2JpqZawIKb52A6M6u"
    "RGvMBquYnLFI3MXGpY3X8cigy89JdIFNCDUQoMWwHAfScSgOnWcoUmmKutRNObFWqexWYMdwDqmR+sLRtLPAeNQipgIBVYbN"
    "W0CtANJBUONQCpGogOYZINivAyQBQgWrC4ULrO/D8/8Av/P82jqLUYlxwy1HJuWoZcwaLWXlRtgzBBKkqSjBqCI3i4yP8jUQ"
    "J3zBgmerNOpzfBhQ1k55uGRgb0dH0jspcVZ1uJyiF1HFRkJtLbUXcpKsQFQEojR4NPBM4ERKyOOMxfWWefCHRXFXMMKPetRx"
    "ZgadsWIJV2W5higTCsZbDlZ6yxzcv1gnmW6xXWXl5frBPMGu4thiu3Gouw1KzHMuYTMC5hEwxNHnKbOQ/UU+y3MF4ovMduSV"
    "iDRUupYIc0IACqZb+k8vAW+Y9YNOCxWbhmTyzR9INg92ZbjbEhCooBhtucTHfKf/AKgdL6siJCi4W182K0RzeH2gLYfsmCqU"
    "80JSRySqWD/B05iOgjTsmT8xWPLl/iXSEEBrIleUXxhi6FuVbTaGPHHT/EXvz/7/AM/xy+DSN2kpbhZKeMovvCAaCGoVqW2w"
    "26mAlAil3KVDZLruYwlUb83WBWZzqutzgDzuDHMO5/6ovZZXLyB1LNzS2865hYOmalqx4b4zAO9ml7SivEdEv8GLRKjrKmpn"
    "2l1ZjasyoijbAhKY69Ih1cMTsNaD6tSrUPXty/SA+IraxkZo8oQAqEwnSZrCdlgXeB13iiWlSpUIsKXXFxStuIDwXKGPE0iL"
    "lHpME7E2Ld2IclwsyPfiUTp6OjE8YlFCI6uaqYVG4zqiW4+0BnsxKqzaYNy0pFOcrtDTntpLGN0JUCmascRVtQ6WjVprqr+k"
    "Ys9dqqdmjztt78x2aw2AFPNwOSKAoJVKAXdHMrrZCDAgGDXjgnk1Cdb6jhmTf3/jcyRoyfUi+WJ6XUDOM9XH+Uvfn/3/AEDy"
    "xcxZp0gtc0QOeMFoOPDEBlhzdSgeCswwWTbBTKo4V0qqIflO6TNh9o/DLKvoBMkoLRH0QGg8rqPrUJ0t31PoiYgpQuyQtbuW"
    "qujvAghqmcV3GLETeJq4Zi6+GKVkodwtzLm7l5LPDZMJiFo9kRxUdG4Oy02RjxDEC9vKEDKLurBzdkCYrUXuLSE0TOabg9ZX"
    "Jve6ld5A+izF+DyfwU64aAmsc4USptHiDZ477pUsCHaKacgJV1ri9wHZURsXB2UO0VtlJjQwAnJg3vGGInUg1RHOAo7ZhhRH"
    "IJG0LSnZFs1KdczPWPdgCXcRic5Y0Q5tmLIxWaxiOqYeA1DPh1QydYHp2CwWXyjIihrO5VfwuOLhbgZeDmMZoopq8QCgdP8A"
    "K5d/yt35/wDf+f46jqUqEMA5qBJY9O8TIgax4FJC9JgQRpBNJgd5mhScdy6Q65We3u1EONQCeCjz5RqWdac/M4gb7ZhQ41ws"
    "spkDZQ/SufvOOGMu0N8EpW1WyH68MSNnZBmCiJOU5Q5iXqDJBiWqC4IcTEQ6zN8QppccRywDQo8nEc9ZKFdAmXbjB5gaYhqr"
    "HS4JYtocdZSMocPJeoFirAh0hUz8ynaeSeSWE4ZoDmJWJUJEqbwWQUfwChOuDzhqwjXaos9ipjLdwIubqhGK3Xcg+kopgFLD"
    "ZMg8DbOTcK7+rT9xr7sQGcyoCct1gRSDQtupuGQ9FSKgu2WxBGnWGjjnzjSXT4uZ05HyKPWKlPEugFTSENeA2pXKiDme7aKS"
    "VS2o5W8+KikwCNJcfzCUN5rtLFyFpiJpSMPNdz0hjhGgoK19IUEN8/xWWRjzy9orx9+f/f8An2Oo6md9JTzJk+hKndOay7wK"
    "8wekKES/AFE0fA3R4MvXtFSdsSN3Fwp60cA6DYBjFukHbA4DO+8CHGQF00Qm7ertXCoDLdUA4ladLHcNzJNUGJtlsWZ4O5VX"
    "P8CUMHrE+BvzMFXL1hp6m5UMxIHN3Lpu/ag4icVQQLqR9z0QDCxINGup2RwsndGwVjznnlOsp1haMNyWTMfCcsyQVUNSr8AU"
    "xAZrqX1i6ovG8ybG3YCOMNq56xOb6z3nMQlLaAzkMLU9pnohlo3x2jzSbvqpulnLipeqytdCUDsbGkQQWUZQeXNLSZgKjZYD"
    "lnLbxkyq1Azbk1w6xnByk3QQqWYbi6zkuAlTpo/TR7ssRhIEGoahrwtMjjmFmWH6I/4VI6JJenpe6ANXcfkhhBh94nQPmRaG"
    "XFLWZ3pHl8l+agTKigYeUiUO6wPWUE5N/wAFjiIuIl+HsPx35/8Af+c86jqLR5xKRBLzzPMCmUVkqVcXwpBVTEiTwjUqkzJr"
    "2lETn8RVWFPZnr0OASPDdrw78pzRCAfRmUG0SadvpHWJsCJn9oLEVZWVzdzNNc+DqUoZMeJl1MPDo1KblpFeneWpuC5xBEVS"
    "42YNSs6iYlo6JSVUF9EQfJUFynrMzIy2ClqRVYY4ldpaWlzKCCVZ4KMqu8prUB3qDepd6fWWMAJq4mll88m+07SoEMjEA7Fw"
    "/eVWNwElwZAgAJdKX3YaWkveWUrqNYIUNbvHkvVMg01DSuxM4tpc0tJmJqNlgOWctuDTKrUDNuTTDrGcUSTdBCpZhuLrOSwH"
    "UjgbI/mmWe3LQlArQOAuFZeWqWBhEREjeKLgNu5V14sSnLbLSxosxC0B0nTzGHjq2wheY3X10+ku5TDPJb8lUwGYCI8iFFBG"
    "mBho19JVxNRLCPWOb7x1fWLf8+d+f/f+c86jrwbRElnOIGxxp5wWXYKYNU5hm2pYSnwDkcMRuj9E4jBqqzHMhA5SoYHhxLtV"
    "sgbuBhq5YZV5wXekOJXKL4DB0Vu84Znm4uAC+GFg9gkcXujtzqVp2ubnQ34oO4nwKkcJZG6QTFptS6ltwQNguYklgepUXeF8"
    "oq6LqLq8BF8kXeG88QIIralQdOFYJLi78juOANsQ/wDjBtd5bgjqqCuWiDTFRGMZ8DyQy1KXWIEK8KidV8y4RNOksQdjURbE"
    "HCbnCxW4LB1WIF6LN5lKyobqPRSJ8yGUcA4W7gGdl8MoC/EElwZAgAJdKX3YaWkveWUrqNYIUNbvHkvVMg01DSuxM4vJty2q"
    "JrIroFWuRQoAFYigtBVN8grmMkeUuxSEGbCsOcNP6NFm0UMCEZ2Kjk4tdNWSQq1BDlajDhWqtys5c7ozGkpjrUFvI11CNWHX"
    "UtGi194AsFjfaZt+DEWmng6zWjPc4g0Zw84pctDYkoUBXGNErXFLuqlj7O4BYx9U18AM31JS3FzGiDt1Oa57QcQzxDdcM3/H"
    "mvH35/8Af+fY6jrxBdmUykG2UNV6oV/JXhQTNFYseC3btKFqwZTkgT0N5DNGmaxuUNV+VBqXfFMAOpVZS5ZfVSnKvDepjkS0"
    "wL6Sy0UBap8iFMotfoG4+XVAa/Qdw4Yq+6dRGTXhDrTULG0tRsvUAd4HT25i4Dl0mklh6YuneYxxKRmouL8F/wAQczONI4mQ"
    "LR5rglteQ2nk6xhRGdU8zUTzGfX2xLAOln0GGcmpt69omcnW2j7jEqq80J9IKNM6jp06sDbZNVbFl6Ie0S6Q4uxdJiUKDm9M"
    "R0jKlyjUcxMcYmAeImVgV4rCaQdAhWiHleEwJeomciAm6jWVWtUTt5DX9aV1gvKo7xpScKZPKEbu4ZmhC18SsYJDaxwxEhyS"
    "7/kBIhaF0g7BXTmnQmcBX1BaupwoquY3B4iFwEIMLYA3eGhCfZGUCZFWaVnHgTrmxDYByI0EKKFVgGETCpdvGeYxg2yAzlHw"
    "AHUCbKWOPrTEZNzm7CDw47jC29omxchCkaTMssu7s+sasiYVbldEruhIkq8oK+yZaUiOkday9WHMKMEFANEuCId6IUeRYhFU"
    "NKb/AFB5IjRU2kvocmjuIjawPAGkjihdhIlYd+JGa8Xfn/3/AJ9jqOol+BsZd67yy1+TrClV5IuGzMu4YidMXMW5qYWci75J"
    "VB4709YgVewc4C4KltV2ZQc8mYUiqxoLhlzHAI4Wo9UGqwngMwUjx2CaeZtE9r+EJiFS1ybxehKzyjIQ8ojvPth2FxbfCLIX"
    "Zmu8sdDGjkC2YhrXy92hbK3/AJBRNZg4zFAyllQxvVKqskuqnDzKqXmvEq0zcukjA1LuDe7VOKNxc0jYE2ZENmXNT62nlFrp"
    "YgDq3BfBBMD0xCo1HAbdEWHl49DODEr2+dRm8wr0E50C9cLzmK8pZAIZW7e8hcsYpNostRiytmUXV/WIpbaBmZ5SnwsQm4bs"
    "8mO2M6+Fd5fab4iVrw4lro55Ra2DiXSVUjtMItpoJ8wuCB7Q1uuYIoVhsdql7xwg5dJRgHJQjryherq4ILU1i4mHlB9IY8C1"
    "/AK5OpPbyEWS5LANZRSF24I+YpSrajlVVVjHllOgqQDSNdzrM76KMjytgoW1JdtWmou34EF4FF9DUsK3uDy5IvFhAHe/KUNF"
    "5rxbFd4KyJqmsF4dIXJR17uoJyOET2HATt3MIggZl4F0QroGWKkg9XiAuTPESFjxzuR4gEt8CSlSzMDw6HJJQ9O0rcLpBOLz"
    "ZxDLgPSoaxBe2XttqOKjvdBti1TbcxWauFXdmrJYTTEQ5iDS7M1cLzvwIMCJzGsfG/P/AL/oXnF+DuUR65EQBmyXRvAIpwJ5"
    "4jIFJF743dR8ABTLuoRBy2bOJVnDoUUJx3l5xcoPAnVS0SZyBx0g2oDFQ0vqwOQQHQjQbPXZGa4ftbxKE5aZvOIjK5Tbq+kD"
    "JNLhCtd5r0WA8lOsLUFWhpuU+vbZaVcUUGe4tOrcQExy0m4scXKx6sNKOo7gwzFrhqOzwLwpBUSlaDxEFhbtzeBlKczU31B5"
    "hV1pMjskI16r0HiWfOVhfsRmCs/BnLHp7Xkb195t/qMfpHeyVoONdJRjoyjlr6o7/DPwJQYZNsCFigcrdTA9YbnOZXALlVk4"
    "nAhlSYmMQDwSzwqyICdCmFK6Gs5lr7gZITgm6znpC3c2nE9ZvP0XE3hhlhQuA5LUcZ0RZPpE9QPSIhgQLmXjwd+I58O9giat"
    "UdlFYoDHgBQUoVWi18dAyo6XCiqAjqGaYc2OISiqZkqU3EMn/wAJlYN0PLtA1Wy781VCqQ99JPeMQNWFudTdN4fIrEEqUYo3"
    "UYxZXcDYGYeYSMXdcwHjdMs7Vyx0HGthzQxURzaDzDesovEa2FWuKgU5Kc89TchjTzOhzuNj2DS5/KMaVtaLWdG4707uTdSj"
    "Ate3pGmPBkmA7RqtlhfTI+P5/wDf+f5piXBk8C5msQLBY7hwS+URzVWhdwCUNx14biUvEzc4TiVz9JzfMGioITFfmG7+8RR5"
    "JrHt67mEMYnAcE1b1i0NTHXEEMsczrM1NwENRQqo1cHgIzddtQMveWGc1LXbucIPCbeUHEpLVTqC0dtQwjq3Athjwm/CNwxB"
    "jzmbfMzYcDGIFlYwRqovSp+5waic950eSGFesq8Tr3lWVVl3NNx2ptwwA0TvzBjxTMydIF9JWcFw6CkYzvUNKZYy91GOZVDO"
    "TzqZOeNRLZ5NagaOBsi23zArWM3LoFoFn0QToZoUECgcEwg5Iq+vgYHvHBuUZ77l77xKO0rrMntNA45ivGxs1DZ712+P5/8A"
    "f+f4kdRLiYiXEuWMIPXKGTfMQibr0iAXreoOM2y4aYwdIw3GjUEiyWOJrwHwK6zuBAqLHgneCGGLnUOXhTcy8S9yxKeZbxCi"
    "KvwMMpGnPh5vAcxe8vvDBdyj3lC7mHN+FOWV6xY3LiXzBqVUu4lxwKw2gchM27SmoGb5vkRILZ2SNGxx2lGUcX955iV3GZ6k"
    "rsn0SzqRrqSjqShzCpS8MtcN2CJ5TzIpjU1A9VCOhAOWRScS3SI1B3L58AzmETErtEZbpAS2Ti+ImDVDT1YGJWfKLYEIxGuJ"
    "UaHWYjpieYhVXogHl6y23lklx+O/P/v/AD/GaY7juO4+AqoNNMVleXRMrjo6imFTCnMDbgmkZ5SPbNSddy44QrzM0Vq2pcMw"
    "JfWU9koInhcT1QA4gDBZQKzds2FZexr24Bitgi3BFdS/md7wuozuR68Fdx8UL+87LKOpO9MG534PzK+Y9aFO4nE81OwsRYIy"
    "7Av1h3Rdb3lRbf2vBRtMdifCTofYnxs+Pj/86fET4iV+lBGt5QD05xPtQNTOoQIbl3TKm+YSYCCYX8B5GNO3nBlEx1JflN9P"
    "BXeUdZR1mOsvyidKleU+0x1loFXSeUp7yuoPOaBIamaK2vlnVhEPF35/9/0PiRLglXEg8HHEciswbfiIOzMw1KOWJ3a9vFCX"
    "Ervgd87EAOLneooYJ5yuF/gcvBbbfWNvUgwhRqAcFNwQcwQtWZv+8DY+/K//AGi3rS3f3pl/7QBoPrK/ykkl7zPeZ7zPeZ7j"
    "PLfX+eyWXdKrJbf3pXnN5wN2Q4h9YSs0qP4RVT/Gi7us/lE1EVHhcQrq57UJo6JcqHfUURvjBQKqs1LtEhTZb/6S/wD9Jf8A"
    "+0v0Pr4EA/wEROz/AMSIiYiJjwCdo+keQkfymI7WwJVFQjhWHEaMGVVAC1UAVCN1WmViDRyNZONCfE35/wDf+aiZYZpCcYWC"
    "3l2ckmNaLEdI+IiRLiXGARtGMmMUxHpM9JV7IgcQDpKdJRLNqeUo4WF8091z23KHn957rl+qX6/Fb9cv1S/XL9Uv1Tz55086"
    "efL9ct1S/VLdUt1S3VLdcv1S/VLdct1y/VLdUt1S3VLdct1S/VLdUt1y3VL9Ut1S3VL9c8yeZPMl3lL9Uv1S/VL9Uv1S/VL9"
    "Uv1S/VL9Uv1S/VL9Uv1S/VL9Uv1S/VDI/YdKQwiKIxY/tLNGwVQK5pAl/ictKuTBXWBgLtt/pqlSpUqVKlSpUqVKlSpUqVBx"
    "Vf8AB25VqxeDlxvc2KWyxULdwwkKShJIqBrBFLLpYoN3TfgWQi1adi+fDvw/P/v/ACCARsdJ4LrTmnDCKC6Au8EXNXddRwGs"
    "WKN2oCP8/mqiuksrPAsr/wCgTXRRRRVVRRRV11xSnP8A98UKFChQoJEiRIkQJEmSJkyZMkSJkyRMmTJkyRMmTJEyZIlDIkiU"
    "KFChQoUKFChQoUKFChQoUKFdv8PoBaFoA8g/rKlSpUqVKlSpUqVKlSpUqVKlSqn+Bs2bNbb/AC6qtAC1XzWBUWRsNUGti15W"
    "VVNrsxFmFqMEijnY+KFDYqk8/wDJVrvC/wDP96ydTVHn/lUTmtnUl0d1s5P94x1IltUMB/msSXaYwu/TPZ8J7PhPZ8J7PhPZ"
    "8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7Ph"
    "PZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7"
    "PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8"
    "J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhPZ8J7PhP"
    "Z8J7PhPZ8IFR963Lsy//ABYFAFroJn8PXCfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6"
    "T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0"
    "nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvp"
    "PkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfS"
    "fIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k"
    "+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6QKwPOiMBRwn+beE9YE1k2v94p7rSbIzzcj1P8oizvpx43GyuqxbQy4F"
    "+k4iPBy3qmtc1sq8zmI8HDW6b3xem6xKjZXV4stZMI/X/dDxrC8M/wCUCBQFHhWD28UtugUBatYBY6KILHaUvdgUGijba+Bo"
    "oAtctC92LaabdNJeD2sQpqwFRSNZEfH9h2MDb5fQzj6EM/Hze9x/0+WdnLKravb/AJfu2DdzN0u92ljbUMYtaxQWLXYCn0OG"
    "FXsvO+df7ARBQIPv/m9k33nntPJ34NSIeYFQJwZuWaYCoClEtpoO277Ph7JvvPPYebrxRuoEXzl1kutm4tmtqRNHrGcw+j7F"
    "e/JQJyrgbIAcKKsEEVgGsOi/m3qcoIN2bRbV3HPWquNnA1Fqwg0lgN45V2DTYWuDUkuNLoUYIk3UhVQbI8SfqZr2lM9yEWca"
    "Q2DFKbirvMGA8tv7WFF5AJ6wf54JXB1KABYAFu/DfV8M+TLCs1a8wYFbTudZB4q76VYAh7z2iAByxM3yCyi5ryApwivEVA28"
    "Ba0ZTzIcvhhpAEY8iwQ6IRiDEYfIDIWgx3RcMZsAWtaQeqifEWMDplctCkQohurgoFyqLRvAvQYYOjMhgDIooTDNNzkyi25w"
    "SpHo9cf6z+f/AH/mvu4jcGK9Zfs9zwVme67mfRo212UbM6GqnkFOdNqwCyu7iNyYr1h+72f4YPcuksQARALjksTYZ7pEgmtu"
    "FKaBVq674uum1hOloL6kuUJga1QYtqAS4Ni7CpAVOZqrq6W5ON7l2aBfcINS9jR84sfl4SNHu1kqL5Cw4wNos6DwYByqq+B7"
    "wWEBp1plxKpiiB8asKvyKFHVETW0lEwBdi3k++9KkFjjsqC+W1ys5lCbKi0DyklGWs04uc0Bo0rZVKTuE7XyAd2+YxCV4Aor"
    "qOXr2xVrm7+tgqyuickqAYIpbFFNiWAZRCEoqhbY4bpF4ZKVgC8yp6l0f6y/P/v/ADPVq7Ab0egOTCc0Hg+K7JcwFaLFcfcP"
    "M3Q3RxabuvA6t1YDeh0Ay4Di08HkiBWp0BD/AAiTC0qANAKreoUB2BJbtjTWS4yMOnRVLkVVu22NMOBIRLnAcqsOkWMzICtn"
    "kOWzMwkOLbQwoOglLqwmkicmwQWDwz61JZDzDSwuGcTqzeZ+VrnD7jMVRGQlmAoKBgHEYpZhJVlyjRg6EsZbwtLXBsuavL/D"
    "d7FCLsU0rVGIbnaYZsCZEc2QP/ieLoUAW0Dy9YD6aKXRaP2gBZ18iDgUYLGXWZacJxVK1b2n2CJOEtEpZlHsLtdWatoUYziY"
    "jFDB/TFKhXZVCMVWBa3Q/wCkfn/3/nFBwVhRQqAUlNIOGhHGwZFrvVODi6xeyc7BkUqt05OavNaYKDgqCmxFEsKLUMtLxe/9"
    "MIHkD5nmpVusiryUC23yoYUqyXbhlqYm+VooIcPgARR2i1h9jQEwIMEBXMSp8VcCd7qVdgCZDxL5CgSmGHFGZFj2oGVujEbx"
    "HgAKSxzTKMZnxPFIhcLvFRhxRmRY9qBlboxCqXgZ2LSwiDdDNiR8AfNjawgAJs2WEqySblljINDsu2sQaSPRLsco1I8fW+DQ"
    "yALREGBYAmaKoGiJgCokojR1QHW6sgpScbbgpSPP/VRSk2rKuTYQ/Hl4VBto4OWjmI8O0WYHYBlWncRindTB1mDaMc3iXMFt"
    "B2w5XpG+mjCiSmkiV68cHJcQArtuHmi0ZoeQY+UZyCqkAC1iXeKcsBkIwCbUuTqqgFY6yTF1ck3pVM0hw7H0cIu2A6sBp9aW"
    "iK5whKwS2xjda4gVWgS1qiIE5NE0mWkFqYtoGyEegraADk4qEzRFgFt4DoLYJQKyCpiL+/QAL0OUIbJRjM+J4pELhd4qYKkB"
    "zW1tBSVnlIRluh5GtxPOrJSI7+e0NS0LVzCiSmkiV68cHJcQA4aqjqcoLMlKoG4JdqCl1SXm6JbcPUALZ2IqCvdhpqWttMD8"
    "clmja0WsxcuQsIHAyZchWVdTa0V7DALgyBct2iaPWQk6oqm0w/6J+f8A3/V7mX6yDV8ahSa9UXg2x0IsSnF6GiEzpQyxqmq5"
    "1L3/AHCF0oKQpaIDLy3E7GslSsGgAJ+WFN7Byx2Vd51CvOKVNFlVOCDToYPvIsk8JimTsgcn2bsGpdNXW6g0vicIDf0Ki64g"
    "+8iyTwmKZOyByKk1zWhJguKayrKxoEja1jsDYLRBqJn0WB2gCA5KhnWItpIlAbiL68NSuP0FijkkUWOrwhlwQ8pvFZ3eeBWS"
    "0x0kCy+kFtFOeiWlpuGiTbtrFi2RNcBsAE8pHnHB4PhtZTzLR4MxgrkK0nC220rQXUcm5pC0Y4CAp4pSHlDpW8aZO1F+RZVN"
    "30zc3U5A2gWmTGbNGzW0wM7DKaMIAJUaSNOxoOgS9OGzeVQPqG+iJo+RDz3CZFcVNlrXvyIEFLMEyri1biNKbAFU/gKKqlm4"
    "e+zQ6TTIstolJlFm5grQ60qrURbLyPkXkJBoMnqFNUUUrXBntI4M0pAA2KsGl8ThAb+hUXXEB7KGhQFyiwoF5zGfTVVMWl5q"
    "63meT8lhy+xqLriHlDpW8aZO1F+RZSRkQiQwV3RYLIjMrHgWOaBAAAAHDcW9MtHaY5OvoFQNa/qL1/JHiIBm4U49lwaaQoMF"
    "zKKvF8Vbjkgt4RdwuAnFTqXlzVf6J+f/AH/mNMpoDYnJV1svqi8ljlja5BdU7oyWv+eGmnnnkkkkkklnnBJzfwmmmklaEkaC"
    "OXPLKT/juquuing654b7nhvOJ4HGN4mmmDhsmD8DXP4Tj3XH8Hvnvq5ws6wkQhbCpr1Ygt8RrgmQPT/6wxsjo5TEdUJtq5PV"
    "g4sSfyunnjvkPHrrkfDy9/E9pszQ8DjQY74br7/B3UoqH8JLLrLg44iWxX4JI5fQ8BJf8tPPPPJJJJJJJJNv+GeeeeaSaae1"
    "1YL0CIwhhY9WW6VaUNZLCYLKHIeH5/8Af+ZSiBTmJc3k0CwcuYsuXLfEf5gBly5hC0uXfgOczDlxFiVvSIo4oi3VEAcnEsq+"
    "PDT+Rrw08OUupVwIFSr8KuBUC/5B8zNksPOVTq+8VC3LKMKVcEFG5ly29SjgBKjFV/A+BjuJEuGPBRajFjiLUuLFvwUGLUrL"
    "ZYILVXO4RDPA6xCYzFl14Li1/kgDcvxBOsuX4MlpgF0pLBtRuixrHh+f/f8AR5d+DbiDFzGk2ly5cPAuDLzBqDfga8Dr5Rir"
    "Z2TBBzzULaCK2AIKe2YNlw14hfht4G4Y8AqBfgHgFQPFVQJR4BfgvWgH11Vd5gqJcvLGlTMusJk11GXeBw9Y5eCmJfiTHhtH"
    "USOvB34O4vA7R1GneL0ixjzRdQ2IRcCOWQEqMFumiYECsUsXXMbR8B7v4hcuXLIOf4DzS5cIGDUy/jj+f/f+ZVM7Y4i2y6jP"
    "miy75rz5io6+sEsWXaCyrC0zXd94QQYQgbgwan3dokbKg0LIxGl9I90oxKm7NxIx0zma/i28Bm/AIFwKgVDLAqBcDwC4FSvE"
    "F+BDRBvrCLDeSVQsqviNqMq1MNB1JVGbTmDDj0m1cREx4JcSV4HUS5UdwRKjuOo4iixW6IttXSbl29OixtriIrdece6zqQNP"
    "ziKmVAbS/KyVyXZM47S4LW9SggctMPkvSJcfm9J7y9J7q9Jn9T0j8hPdH0nuj6T3RntKewp7CnsKewoJ0fVOJb6p7C9IfPek"
    "9tekPnvSFgI9xD8wQI2ORJd+C8Y/n/3/AJlXvM5dkWXiLAixhVC6utxSUl0MpPrgy/5Es+behHfvwQGlFLaynlDwCAXBvwDB"
    "rMahmL0FI13jm8x8eTCd7XpLxiw078DfhtHcNeBrw14AuBAqB4BUC4EDwtCCngAqjLKLzjvA2kdhYiCXCjqnWPRqW8R4JKjq"
    "O4JIFqtASg2ANHa3F/WDIELEbE8DuaRVFGFuwiDRbyxGw9hYNq93MuzYe7UI1HCV+o8YjhhIr8n4n02oR6Fika1HdMPMUA4z"
    "liX/AL5Pf8409n/ZT2es93ynu+UQ2PsesH9wKzYjyh0HgJpPAQR2cHIGQ3o+ZRZlJ3CFGv2/+ynt9ZT2esC+z/s93ylLCvRp"
    "M0gZZAJLBZKfwh/P/v8Aze6dcsixhgViuFY5qMA+gtgCnUW+z2nSrwoeuJrHoRqZwl8tfqVd0bc4Vhv3haQfP+srZQE4bINg"
    "3cGKJhBCyQyYwxUeeJiRdw8LpiQ7gxqBnw28RvxMECGWECoEMvgFMCoPCrgSrIEpXEIoleFTySiV4GEo8EvwOolxzLA7oIc4"
    "8rJZhVUXZ9HH5iGoKs1TXDHSU4gFB+SymNoxGUu3ydfeBAGq00drcShIJkTSRQCjr03FTVOwMvaX2vJ6a7G4PgMM/UogrowZ"
    "vzHYpOgfSaoZurMORzw0/WV1fypCLBB5QAEw5gAHfWGc4RB94COHEpRgEYs2C9/KbEzTD+CJUW04fXUOF/8AmC6gX1RD7Rwy"
    "ejVAuQdD9JVoH0+k6lTsfSd19vpPO/T6Sx0fM+kxnldDMfqlcQq1HLog4OUtwjVRORKhbWRDYaYZThMfsiU4yB+/+wbiweJ/"
    "P/v/ADL2HOWeC1LYqUukzLSMtPqFg6UaLJyStwrRdvaGrCmD1ZjQQF2uxGnBuKnU4NR+qg4ThIGMOaaYxC2wwyOb14CQWEGJ"
    "rMsqqhaDVeJ2h0Q3/AFQ34bQz4BDUMsCVAxB4hcCoHiCoF+AWxBtzKlDzKjhmUiVKiRPARx4EjjwWLflKX1My8vcF0f9/Msx"
    "zRs/5+Y4KxTdX3JViQFAf+7i9MgYXke0H00Zz2Ak7LMSBt9FxVTNkpO4JS5RzLTP56zm6+aXnL7HUC3IyL+JiKZjW+cUHnhO"
    "e8A0rTeY6Xz/AExtFVMHXtFawr2INEd4BXNRkNlIyct943Y+zax6Ntyuj+yfQleOZUqorW6IPAXDA9EP+5bgDpVfZj7tnchh"
    "f4l1Uf2v6Z+d/R4Pxz+f/f8Amde4ziniDCvDiOriCqk19HqjkLjkyuY0umXtpAWqYhS2a7zqhTwRw7go5aHgnY2/hMOxx28A"
    "8AoOf4Ob8NJt4GvE34gYE34DwDMC/AJTAqFoUgSmZQ8BG1p2iZKK7xLLsEsL0wLmI6ZRKjWNo0jAalUwR5mzHHg3ZpMr4rMW"
    "3NQxS+u5fSpQABLUy2XVCrvOocxVRdn0cfmJ+iq0FNYpjpzKeyqC762Ur5sQK9pm/Mqj6srsFsNfa3F+TCYBWBsSPUyuLhWo"
    "YCAtdTpFR0fZP3P0waI1isVmtSzjyFyqzsch6NsTAnYJdJw8Sj6y73/guX0gNoYeWYC6wfuMwC8n0ixmNUAOStR35D9Mdezo"
    "h/BT+f8A3/m9o65U2ifvqL+NwZyb85I4RjZ57wVWbiZH1odzNZQylaiHRWCssxJIyr5hFZFrado81CaqukeOltiBUMlKaL0j"
    "hLzvBcUUtBgxMG/Ab8W3gahvwN+IuioBIMot6lzN8koKbxA5Mz8TKB41fgEA3x9Y9TzLKKFeA8oMuhw8oxF9kA/MWx9LWw+c"
    "0mz7zsgq7fuG8q7p6x8ruq/6Y6NgEeUrRIXaixgTruGsWL0CNFrJxe2U7EXY8QVljqJEpjuCaEcekaNVd8xVzqMpTRWiUk8k"
    "EOGOHBTQGMTFXrojhyOuIRjOnJDVbcQrEoWoJ3QXxy3xUYWcvqadGK7zLbYc0ARSCCCXcaWcbIwUSix9bKV82OoqMrVvk1Rv"
    "mUpkBp9rcX5MwmQFywQARwOGM7OifcqLzQY6pZTEUVZQfvcAApVdJFtppjtAhnh94WMC1+Zf16CY5avni+kLbJQwn+Gptzm8"
    "RqjCoAQXqnDBt7o/c6MfsdCEXij8/wDv/Mq9lnGFLDyzFAMsPvNC2Nu/WICBkDD28APDziXWL1xCEOXvLvfRZs6SqyDNrMDF"
    "AMQF+cCGhQIfVhyXaC68MAmDmZEsU3aXZg14AxZg58BubeBubeBqG/DbwDUUFocrMqibHMYygyjjPYCCNpXL0lbri6OJV5eI"
    "KfA1AhBBlXMqs6lrsAFrUHncDqRbjyXiJN9ozJ3vEWKDdMPsYitrk85MslWB5R6JbrBVtl4084DYJyYgp7nMn2ZXYMPivaoQ"
    "UNgD2igBSll7Eg0McL6xKhjHU0JVV5QQjoDBgMuoKDGTfSaD5xsIUgWv/UtuzfeMRtnTCUppbghuUpgHTb84qUzfYPpOQatv"
    "AXOiRsKcYZ6S6saovuMOfzLqQqi7Po4/MwdKrAU1imOnMuydA9rGQU35rvwFERpOSVzAPzKkLRp6Nw7MRCaKxiVsYUxA4dhm"
    "GGRdOgJT2yVcAawUwPDMD9gK86hlrlg2XxqBevOXi+JqONziWeDLzfDL+yfMOIMANv1Edn20x17miEK/HH5/9/5nXss4uIph"
    "SFpn2BuW3Kk6KTX1z4JhoqsvEs7rNwi7hn0qm2NBWrUN3EwtMWIwsloGWpjjAcWvAw3wkysWH6EDXtFuKHENwcQbmk2gw3Nv"
    "A1Dfi2mgmYKXbMrMBuABVXMuPRhLCK1ZZG1TUSBZYBCBUENQIFaIqMkLJeVNbh34Uh7CR27KXQd2J8hlLfvLly3+Ny+l14DT"
    "iIFaxaHzI9q8Vi7pWzloBoxRGuPDziWK4zQPSCCcpoQCMHPSIFBoIpwoOI6csBKYXQ13iusUhUq8Qks5Deo3ZSnzhi7bWA3Y"
    "3RFZoJR5xfWUDcW1m6ft44a8LoUyIMmjnWPEXpbOQijjd9ZX15XPguDzuY5tWiwMGReWoYZvCh1gyRKM4cy7JuOeTrFgFhah"
    "lD2bLmxB/Yu2LxL8Gk4CbBVcDmRodOkukDvN/wAL+cL+0A8pNEeHtzF7HQ8D8cfn/wB/5veuuLwYPOOnWh+2JGdqCclTDk0Y"
    "8NscPN3luVQZv1zBNlZBT7x5KoV1jAuSpVwdn1Vj6xqpNW5PN+AfgwNHZbflE03DWPArnDwG4NeDaG5t4Gob8C5t2g/hiM2o"
    "4hpqxddY1xunSALjh2ldYKa5hUr6kVDKYzAuDwAb+rrDmjF4zK9Bsmg7yyqiPKdOuNpFUlvtMHZ2RXk+3+S2LvHzSI9MBF6U"
    "peOyVJALW31DEcBccmNeD9YBmhywYarg5gw0OCXCIAqUSwn9RpWm8+GbcgPLUQAypqy+JQFdXVvWCEAtvCEdgEYB1iggSrGI"
    "lAchl6+Fbz5RivEoFFgtC3WUO8FRoYjq3Vi1QYDiW8MTtOL8LsDPnEdQoVBZmYYw1H3PNpeA/wCw4xdBfUjFF45jM2gLAPHC"
    "WBYVefR6QuST0F8waeFo2ncFAvgbNXcIQNGAQLCyxbxNOfx4tS96nA5KVNJ4IGovFH5/9/5vauuKOKpdihX5hbUz65KoTpnz"
    "8BUiIwvZDWgC2WlwchPrAX1XBF0iwOSu8zoXOVYhMWofFvgmHFRH9jd0iycpFZfMU4RQYb8NJtDc28DUN+LlHUlX2COHcqOm"
    "JDTPDkjR4OUUXxhjtDJOGWrYIQNQNFgu+kL6ZZoCKFM1T2HaK1ky7uNtvGr/AKKIFHITGZWhVy14+kGKQWsPZl82HsgmUVHK"
    "FSz2EwYWpjTa2joFIjGrxPxKuOLtjiaOVCWj1mNqd0vIuAEFirLeyAGqhdbjMbtEpMPhQ8JVQdsp9mERWjP6n5jrMYbTBwnu"
    "PDKcPWEV6ILKhVYWph+kZKyXarESaB3RAZUiR8Z3UMA1y3ooOLqENV+Co79pQchwPDKPfq404Zf4ok7AjDXcM8t6l1LKBArq"
    "1TPZqA0PhmWbe0Aw+1h6tx2fOLPwB4q/P/v/ADexdU5eHSD1jlHOsGLycjR5eAFG2sXBBgyKFeItRlcVMayZzEYpxaJhArg8"
    "I2SRdtp34Os5tCjcJwFtWzqecWFR+Mb8GkNQwzb+G3gZ8DiGCYBofVMzlpRuPsGwSYRRQYoPDCrmoB2TRxg1CYEC5lIEtTSP"
    "05QOYGW8wuYOK/oto82XTVRpzskeIuDTCFFx0QZus1nzmsJQCsNwi1JaXKpEAZEMr7cg7QwY2EHZ5gCdK1MudF0j5wMI6/8A"
    "rF7Hsxv2lYMgdoDCovtTTLTErNDjMNqLTPSVadXuuk4tFu+Vkrl1n9SwyZmVFoisYAHlgLzUlhle0oqzFoAIVDH0VF2YZdww"
    "SgYRw0kRqqoI0d4ZOMXs8xzAGv8AViL9K1ceQiyCkKL7OUScmVP3dYlZXklmIRaFHapTM+BQ6DFavLcWBXAViVeIUuXNRKLs"
    "2Cr0mZTkO80+LK8Qfn/3/mde6zmj4X8TfMtmDduWOAy6XvNvWU63MiHBsCJ19YrEFD9dAJ+0yBg1upPT7xJ1AYP0irG7ctWr"
    "1ucL0gIYe5oOsJoqq04KefgHB4nDwaQ14D4GpuG5zArwHg2B9HeCWg7IIikUxkG1cVCYEG6ZTjHAEs2QKh+ekDQblUhQRApK"
    "3kbbl5w/WJXl/TK6x7L1g5HrLVdsMmGlrfbpBiVHCyEWU0w1GVy4OIgq9T2ihxyj2hW37eFs+tx+/aUJmzyi3tt9QPGVIAC3"
    "K1woNhfQOfxKUhg655ULq7/Utd7iDvUaNqbt7QAt43V1R6ODS8TS81gmNVR5yuNnDLbjn6ahy3ljWMvdG13qcJp2lHTU13le"
    "bwFzuewjxyksXpHSELxXWFXq4DBKhBsHDNHnN8aENeMPz/7/AM3tXXHXjdUXMqxNnlFAgKOp184mhCA0Nko1OZzNwXiNOOX5"
    "7RV40pfkr/sEsV2Fdj25lKtXgetTdnDwm5whuaQ14GvA1DUNznwNzaG4Le5b1huDwFu+J+0IDjFdIVaW5ymqinAu10vBNS8v"
    "Xt4X/SMys78FTDYNXbMJtRCcknELMvpNYWnFwqEwmYkFbLBkOWt/xczm5WcjbAla1FUMj5cxgzC7dXnzjIlqObU/Y/UrMes1"
    "pI31VjrEvHgt6kwH+Wi2WQCjQBavaOMk02ty6R8KiOVhqg7/AKzR5+Gafwy/P/v/ADe1dcdeARgpumpf9ZadYDQ9tQy+FnLq"
    "VTpe9Yn0+04lNXV+UcHNda1EQdx0ebP6mfqgNdT/AOI7pC3kd2x51oo7eHh4QnDw0hrwNEMw1B/gbm02huHg5TSCVp8FxTRM"
    "LR1lhvGVjuRsR1/Wu46z0zOCuIx2JkM42S4+8w7hxFTDTrUcjIHFtwo/AnLDKFWh/wBSxcI+XEcazAvafeNDV/WpTV0h1qAM"
    "VEcV3eIwCVVmX1eUl1oateXSDsNDP3f14rTN1nrAS8p1KEQLdA+UKkEOEySk2k73jw+jL8E7/iWecv6TJX5lc77RmxU5g7vS"
    "Z24MdaPXtEeopTQOhDDscbCiYBNHn/LGX5/9/wCb2rrjrwCDMNQ4rhwwTLsFzCAKaHQecFL/AOedLg9C5rGQovnVH3C2CfXX"
    "K3z5nTBEKdxuEBdDWaqMDd3WqioMQeIanDw0mnga8BqG/wCBubTaG5y8TScIam8GYjY0q6R4DgXOWVjWY/1yBXziOoMI84fU"
    "QxtGWWuDXgOIZkYVXWCVKPyTUeqjbzj6S/HILB85QUOiubc31MQBG0A+kqOhAN8sSbSr7nZ5G22r1uGUKT9n9SoJpMn0jQBw"
    "XxE5Nd4LErPKc67zJndOfWDi00EMcsHyolyZdSeSeD3apdcTrD9q2lM87F9iX4M6gj4QUDkdzmLt+75WcAWnF7gKKghx8/DN"
    "P4Zfn/3/AJvauuOo6iQQRK3HoxEV0eWDgIwAPSHMJ3VHuHvbmCtdEPugWWn0iG9wQQQQ4hvw0hqGoahuDDfgNw14HEOIbgw1"
    "4nCZcXHheFyO8x0q13Ftax0JbLv+wtMa7DAWVNXdcw5ms7/AAXDRqVzRSxwRycwLbOuLK9Ye+X9xxcM1hyiIu9QgQHWfv/r+"
    "ETwEuAuHaxv7wYpwmawHcYvwpFeh+qDuPLGS6zP5o0Zt88v2x3TDypAaB6hMCoOEuAzj6IIEFTT4Ir+GX5/9/wCb3rriVNol"
    "TKJiMWSxERXTErzvvNL46kKx0eJkutcw4vcCaIMwKghuG4FzSGp9yXR36SmyumyZFw4esvPbrOXgMwyEOIb8B8GE2gprcMJ4"
    "yMYNuydIt1nRX9q6SM6jKFXbKARsSj1GGHENQQwPSXHgdivKPBuFsZvzh90oC9FyrUHIXU0DwyxuYPDKZ8piCCJcETFOYl7i"
    "RL4J5vpKBnEC84ogYRiYzwyjrLQV5xCATRTDoKhAwQJULm4G38Tr8/8Av/N711x14EuJEj4Bt8o5S/nzm7qIIX9piKuq4YvB"
    "AgQIIEN+DSGoU4cBzLKs0xheIhxCsM2Ib9HkgJTB0kQlHHgyIahxDcNw34DwFWXLdaU/EZC7J9f7aWxhsrw6XBywdf08Aw4h"
    "g8LB4AzHBYQsvmgjem6lINU5m3YDMTXo8pgrPEXSCvHDE0dHNYYv/wDLH4rF/wD5Yv8A/EfgT2iPV/ErN58o5s3KMw6rJSg5"
    "5iCBY95UjBqYLkcyzLDO9O/+IdT8Qt3+IfEh8BiBdnsEHnVoOkIhx/F1+f8A3/m9q65wxI4xGOomIlyu0rbpZQvz1gjzmNio"
    "8VpCpYvMRLXXgBggQQKhrxBrwJzAjhx0LM4DXiFS0pGHNEN2bqLDdMXMDO3fgwKhBxBhxDcWYanCC9ynGnM6sVFtS/n+2bis"
    "3KP3M9WjGsHhME2eICI26x8b6iC6gXLM/JLxDtUBuLUJhprLExZAHrCLbt2Rp7v+xp7P++Aa/wDxH3kw/wDie+ori30lEVl9"
    "IBA0OtwDZ7Qv/wDUC9tum4VAxepKhYv9J7qnvqZ//E99Qt/8wLLadiA0PN6ys/xC/P8A7/ze1dfglxLjDCRIKi480aecvBxB"
    "eIA04jlcS71hkwCG4PAmngC4NTJvRLA3SZuDVW4ZkK+rrGyn6YGAodI3ed+Lo8QdQYeDhDiNYUQP9w2kzRyVEshhhgg34D4B"
    "FrLgngARfYhC095cRGm9wEoyBOKpqoKF7qVjES4whEzEj4BS4gF1LN4loJe4KVZYWjdMKNr5w2Llonm8AQIBAgV4Kv8AhF+f"
    "/f8AmNX7Nw8LcoUGUUCtExQcANIjpHjw28W3aJKtSWfaZ40wGMxRrmA2URecyjW2DMECBUN+GngGvB0Nckq0G/OGCRddY3AF"
    "VVmCqx1cen5CHfcNQ3BpihuG4aiiigxGnO7Nv7ZiaXKO5Ao8Ahhy/wAALJRqLRKjASj1GjaKusKdVgEt659EImkONQXXaXcS"
    "JEuVEzEjFTDiUfOYXu5k1TwjLW9RpCatQgJUDMrw1Avw2lca6w1IbLLWVm1Ba8Pz/wC/8ggEbHSeGLcxYqFgOA2VmMSa1JSC"
    "YQtgcr5Ra3waaz+DDTTK7nK8nrGq3tGv9+j2N42x36mPTV8MNX/FnrjLK5lf8E0kgoEOomvAm0DsC7Lv3FtlrxwKFIaIM9+A"
    "QB/yAy75aauMF4d0FC7gfC+ov5i3sfqe16E9r0J7XoT2vQntehPa9Ce16E9r0J7XoT2vQntehPa9Ce16E9r0J7XoT2vQnteh"
    "Pa9Ce16E9r0J7XoT2vQntehPa9Ce16E9r0J73oQD2P1O0mwPqr+YvGujKzlPDEFICsiyRb4BAtu0jsD7Ea5GvO4PBtF88MJU"
    "SQBS4Jgf4ZZZdfOv/i0ww01Cbk5ainMpqwmo70zj7CM4y1HxR/iaewwww0yy29Ro2KtUotqzw2KDdwVRVqUNBVFBVFeKFDYq"
    "k8/8lWu8L/z/AHrJ1NUef+VROa2dSXR3Wzk/3jHUiW1QwH+axJdpjC79M9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwn"
    "s+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9n"
    "wns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E"
    "9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns"
    "+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwns+E9nwgVH3rcuzL/APFg"
    "UAWugmfw9cJ8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfI"
    "vpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+R"
    "fSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i"
    "+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F"
    "9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL"
    "6T5F9J8i+k+RfSfIvpArA86IwFHCf5t4T1gTWTa/3inutJsjPNyPU/yiLO+nHjcbK6rFtDLgX6TiI8HLeqa1zWyrzOYjwcNb"
    "pvfF6brEqNldXiy1kwj9f90PGsLwz/lAgUBR4Vg9vFLboFAWrWAWOiiCx2lL3YFBoo22vgaKALXLQvdi2mm3TSXg9rEKasBU"
    "UjWRHxtGCbpvqygvigeLMy+2Y8xtfVNFitx8RSh2kHIiIj/gUu4ILv5yxAU2BtI619z4sQ2c6oFmqz/Ba8gNeJbcouxo4v8A"
    "2EiCgQff/N7JvvPPaeTvwONHVJTIK0Wux6Rut2JBV4zTSYUoZ8PZN9557DzdeDDzu2CrCjO8kqBhCujuR0zjtCJzVBfI2SlQ"
    "8MSvN3rvKeYczmxQirnDXRlbL5QSLqwrpVWu2IE1ANIEaMKORZDW/cKlmwrSMKDKTcALBm37TRCtW1sxEIUIFKDABcqgLUi9"
    "ISgXbABgwIuxCDwFoepI/QU1VeXllvB1Zqs5smSdkLQYFudEIItVkTTD1TS6AVBv12gQ1fWKAo1hYGo6wtrIltVZi+UiOcoa"
    "GVNb7rDtBB2qI9ZCBmxHhUrfvNeqtdi9XmFk7bhYfQwW3Ru9oxtNFbScWWnkiMkeoC0VVRq1igSrhsnuhgTSlFqbFpozCGj2"
    "ktkEXd1pEXDGbAFrWkHqonxFjA6ZXLQpEKIpaIjlsXMVuy/sUcytzTMiNNhuchD7evgXR1WXfDFJr9CdE8FYCigxH22rzcsE"
    "erK3RQ7jEFohLGBhs31jVVV3dKYMJKY083u8lIaayK3CPLm2tiTh2AESobRRd7d2ylrfyDgbmHhWti6bsK1P0FbEIPAWh6kj"
    "9BTVV5eWW8HVmqzmyOTmoDgHgBAIpMKbRIBgV4KLXRsb8Mfklkeo9QR+gqv8Gy5v2YYsRNTZDlEeqTOEzi9Ri5jYRHPCRLAX"
    "ZtGdbtgBavgBEhi12ZlouyF8kmeQXVBl2BoEtNwaBrfCpCgYiwFYHxhV7hZ23mUT6/gZQAUxVhoXQYOjMhgDIooTDNNzkyi2"
    "5wSpHo9cf+5+f/f+a+7iNwYr1l+z3PCho69XeotcPNDShmUgU23AX34Ebld3EbkxXrD93s+HaqurJ9whnPEAtLXFONLQSizi"
    "ysnLUABlYgMrBtQ1QE6cNFnCzw3jTywt+DkNDTuyxVrhkOabFbRDrcBAhRGqFggTzOnVhQU5jqzEllYcXtaDevRLF2C7MoON"
    "0qV3B1SyRSGbykAtC/SqIZ6CDou+IGKeuwd1U1WB4TRlorJVIj1q0q20Fhm2cu6nZSqKasKiENjpRGYFoEXoiukVwNtBzVXK"
    "7YJQPvCZvUo88HmXbmyCkL3hXUgjuIk0Pa0GsYStmMl52EqiBeHK0GARl0gR7rpiBV8tLLjEsRxUT3ARiErwBRXUcvXtirXN"
    "39bBVi/ExmBDFIzXlRQpNy9VnigMxKKpPDddFXl8e5rKo3xcpILTg0OS3SOmwJkxsD3VYgaQ4WTHFaYm0ASLGMHsDVUWIY2l"
    "qwQUaACKcIFF8p3BYJCVCFtNpAdS4pIBdVS75Tc1ZTRdToi74gYp67B3VTVYElgAeAtixQYUUCryqmgNYE2iAlAOy/BmYQRG"
    "LsgqRkUu8G/taxqqOouMr11CgiqrAgWmqouIlsJsmDwVSiixfH19v65RUjReIDmjwziMj2ygqEOxQXYZu5VAn3pkPHcDZQJY"
    "lnExgBB4MWQNlgaC1sqUWrOQsWAZRCEoqhbY4bpF4ZKVgC8yp6l0f+4/P/v/ADPVq7Ab0egOTCc0HhbiebwNgkqtWR4D9h7b"
    "9e2dqreIdW6sBvQ6AZcBxaeDyRArU6Aic+CV2l2A3iusHfxy2EVIZE3LSd3VEVoCuBfpEhizmK5tGSjGmpbZ9XbUYLaXW1eZ"
    "ryfdNVygA2ZDM323JsUCPlK6ZEN0FIQ+RUDzpaMLpKB67pZIyz01oIJaANjgDiZC+zr1dsaaMXxEPmXtHKry+NE8w70u5LQ0"
    "2DvwbpIlbaVYbAS8REio4RiUypwhtTK92Hn7XYC1SVkA26x0IW1OqBPpGczaK9QaoAWdfIg4FGCxl1mWnCcVStW9p9giThLR"
    "KWZR7C7XVhkgRKOxNg28TGq78azA0FAwBxK+NO6bYLaWmweITyhDC6KVDAN7dYmuy0aS1CqUw8xFOriS1IJfKs2v95V6llRa"
    "HKmSU0Q/WNse+ooylLWF2urBiAEYocAAACiaLgnAXBp5uziaJ9LMuckBscAcTIX2dertjTRi+IHV8ToEWQAobouDR4TqAFBo"
    "6tt1fAKDIAgIovIidRGWy4a+fTUy4esVPlvbtaNdoPXxqEBSwAKthdWPcUlAoDiCbLiGXmJFUyiqrlWWZgt1veMfiP8AhMlt"
    "quV7w4CFACBFPl8N+rZX7/8Avfn/AN/5xQcFYUUKgFJTSDhoRxsGRa71Tg4usXsnOwZFKrdOTmrzWmCg4KgpsRRLCi1DLS8X"
    "v/TEgAD2W1Co1hF8wal5u4WV3Mo24i7Qz3MDMMtbK1mJq2MPgYX1hpBdPm07aPSJ7jmo2PoSTr0WprcmQadYbggpADQXQF8E"
    "EPXK1EmkANEulqKhq87IowN2xKs3WWhgOIWk244rIHjGS9xUS+FXViKYMfNjkheORg65GgmKdgN1dWJiZxw0gAK2LaFQrFAt"
    "t8qGFKsl24YFOmYcwI0hS2gZwW0TUL+3I0U4A0tRhSm7Bg9zdUMVvcIvKolJ7a2uhzG8R4ACksc0xn9/o+pjIVFHKo1B9eSh"
    "KUKCg0siyJ+MCwWz0Xqmqc4lOSb0WsKILoBpgJ0kn8BVYuQmRImEpdWLWKtGjRbQjmfh3QAospQ4zh3VwAQwRbEES7MlW1ux"
    "7ADLqFVUayFSZH9rDQIDJXhTCAmsjD0TQEwIMEShEVpoMwwbRjm8QHg8DEwoGy0aszKBbb5UMKVZLtwwEmJMNSkcK4Ato4Du"
    "JGtDvrAch6pSStkGe1lAyyhJenbFxbOiUZa4i4hjZmhgu92tEGxvc25rAEDaCKMIkjJahtHECS6FjppqhlLuCySgVSsRPKNF"
    "wDJAJkiam58TMvNzbG1u8orSRVtylvZNAbgy1LMUeyqaF3dWEpG4CLmALa6D/Q/z/wC/6vcy/WQavjU+/wC/prl1dXR5RlQL"
    "VWG4AEjnhKZaVy4EbSUKi1aN5RzEGAFRKgEaXisiYs7yy9y1TdC4C5lh7Wls0oBkpa28CIJ6p4LF1hdF9CWe0Cgmsgli7Aq9"
    "yyuHe2EmCSrbq+ahdVusCs3AlLQYJ5esvljzFldV4/bIcFYNb6MopKfEdXXWCzd51Bmkg4H7XIQG2SPvgurpq4NZqtuo17UW"
    "c9wKMVoQrbG1QIVw9wtDGg7hLYRDTEpDQXZm2fC0QVq+UxJclhSDUJxj3Cs91GBLZqXToVcwqFWvbOQACtQo6vFazgwJ0AS6"
    "GXn8QpaI3QAEHDusIDFx5ARjJOAkygl2ZdU1q3cWwuo5N2yAWUN280S5DdYNdSqJLByqlXUWuFBRAQGiUkNBEquxKJWVg0AB"
    "ETZXpaxzO2lcF1LnIuOBMd0krFAXb74Lq6auDWarbqNztOFAqWl1ZCi8bqQ+IZ8SR5Gi8Z5nHTvDzndlYVnH5UT3cHLDZV3n"
    "UUy2/wC5KwPqW74qBtk4RS/GQLNFXBzWRP7sVslALi4r8RqSGiq6LBZEYql6EwQANrL0RqFzyYbC7ldqAurW+VLYWDQIIA1m"
    "bS3cc7iENxrkMCp9t1dqYF8tG/8AQ/z/AO/8yQKdhD1VVU6qwtby8ifUXy6+H8peOdRVPT+LMGDBgJZMoTDmb+xj504WGK5T"
    "9SCnCZLNJBhMkCVmOzwBVOC/bhzzI/8Ar2TBgwYMGDBgwYMGDBgwYMGDBgwYMGDBgwYMGDBgwYMGDBgwYMGDBgwYMGDBgwYM"
    "GDBgwYMGDBgwYMGDBgwYMGDBgwYMGDBgwYMGDBgwYMGDBgwxwJKh58X/AP8Aug7T1NF5B1qMOUatXqIBEbE8fz/7/wAwvGEW"
    "afmLvfifg/kr6bleRlAQvsSpgLgmw+0egV5Ttn2gMFfaAGyrity3+vO/pPyf4NrzxCh2BcjeFC6z4/n/AN/0Sx+D+SdeDqil"
    "lSpUrLAZnVC/9ed/Sfk/w912eP5/9/0Sx+D+SS5WJX8CXKxAqVmV47QHm/6uYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMY"
    "xjGMYxjGMYxjGMYxjGMYxjGMYxjGMYxjGMaxoD5N/wAHf0n5P8Pddnj+f/f9Esfg/poZCjsnsMr/APqe+z32e+z32e+z32e+"
    "z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z"
    "32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+z32e+xT"
    "yOs1g7fwd/Sfk/w912eP5/8Af9Esfg/wJa8C+JcvMpMwAZE7QswLkKtIctP/AGffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffezORjMEhFmgmHOGv8AxHf0n5P8Pddnj+f/AH/RLH4P4K5eIuJ+Ucu8uYYoNQe0QOcT6w4KZbgK"
    "G7wBSqYkFUmwHKhoApbCej/2Xd3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3dIQBjHqQQEixaoSDtgbA0QNqSFtWVH/AIjv"
    "6T8n+Huuzx/P/v8Aolj8H8EYalRUxD4AaXLHzQwJ3KJHCha7O2YTPoAjRCUpQJyVW2visBja5DAoKTNQFrQC0eYW1XH/ALQE"
    "xLj+AQQCqAXef/Ed/Sfk/wAPddnj+f8A3/RLH4P4J3NomdMVYRBRdhIy2cYXWISN2/hC2BcEdEoGgs3CnEooyfa84yqXtwgE"
    "MIWOs08Volyksef9Yud/Sfk/w912eP5/9/0Sx+D+CzdyiqYO9rMJ3tjlMZKS+UxKRuRPIlFMHnOCpYtQrH7EsxQgNnnrM8NT"
    "K22HfFzXv4uSCJKAoeYIUjuS8S/Cl1efC5ZV8Sy9yyXLlyyXLDwWpcvNSyr4l+Fyzws8BvxupZ4X4KEuXmvC7ly5cvwG/CyX"
    "LzX/AIF1lXeII6llfwd/Sfk/w912eP5/9/0iyiPh1jtmb6rf2hc4zpbYg1s7RhywjyDhgI40NUpLhe0HOwnEgHl5TRQ8DZOi"
    "kN+K0Q0zyvAZZTu5eI5U+6jOuXlgbZ+01MFgq4cVu/M6p3CDzpmIWZ7zC5jydX1o1DONd3SKLSi6PK4yL8E+e5dl+D9xmITa"
    "D61MQACxvp1nI/3m9KvcEbiCrdA7rKtLb5UbgoHsUHURjEzw7zC4KtUcn/2h75sN89ua7SFT7R/AYBfytmmb9w8mbhruPpca"
    "s1CE/MFlQFqqqLi+ymPTcze5FTySa8W/5AuHRKi4TrHllSNH0uM8anFdrgH8tbDzmDsIw3YuVpTdjOKiDWSI/uY8BdZDyGaM"
    "Y5gKQVVl5LhBfFVidY6OGqOXSrmWz4nmFhRzFH0xuHYEK163FYTSOL8wvQw5r/7TQW9PIMYNNGftB3fgB/DF9fQxX6yzTpX/"
    "ANpr5MU/RIa/vXaMftkqbTtENK9K9j3hZ0XmtH8Hf0n5P8Pddnj+f/f9EtxGIhshDKzCNOPAv6TeKAY+qEyRbFdusoaJhVei"
    "ZchVNm1+kVaiIbTtlg4Fg0kPraM04cSgBQAepSp11Ll4umCgvCpSp+5nOEMtVp9yWJRjAIVrqkvrbEo+XiJvq3BAc295ZbgV"
    "LYZBOsdMbQlXLNYgQe3lhkfKPyUtkTg1EWRt8eUdHlBF050RytLqNfFnZWTHeGZApU9rxBwLbUJipzYBqWYOTMxqNdEnA1Kp"
    "gmAl0VtO8e9nLcOaVxLkNBFwuhu6mBcO1ru/KPMix8yHETpFxC85nH9glqVGIj0EyiG10jCWUi93gdiDHGKvuQLPrLM6D0Jk"
    "DUpisVAl1atL5h4gayDowUHtChw4JmSUgq5lww0MbdJXgCVsr5qJ6TGylhncNHGhZinSptBkdQUMrsDUcW2c3GYlR7A7TVyz"
    "d7GDmAYQkZvmo4a9z9JdwKxNwhnpdQTbTLDGRN+UbCgkzlobuu8VGd1vDZmL9MEM1d7ibaUB55iMDqlkTLL3PWoXn9WoaESZ"
    "Y8rB5IGSKrmrGPOHaONAFi51FyDeAebg+kP71zr1mlzfaPRbGg9/OLh6wEd/4cntHfn/AMPddnj+f/f9ItgXvHado7hhmDcU"
    "Q6f1A7ojnQ8wqCAxmGNytzSZYVX29WDOZdWrb1OIkWcKhx23AKbAH0CC1yG/Hdv6QTYSvJR+8pHwHCDTbiuICWKDQDUdPySI"
    "pALpXYAMqHWV5Eas9MkeZc0WtsEf+zFKoKlbyxCUgwLBd3AQLQSzsgNkN9c4cecz4xDTpUIaoeIikCUMwoEZupqnNBY1ZKqu"
    "xotKHjcbIO82YM0rym1dMEClu8N8LmgW94P2MvwLaVzxV1TXZlYKicgi87msojFRtijpVYeeCS5EGQmDa0YiyisoBhet7l0b"
    "ixtuaVHH4PaD9yEwDf8AwShuWCknIxiFnWEWjevmahY2mQZsetwohVcUULH9gqqC5XynfPwCARiyZxlWovdRGOkFas/ah2Sg"
    "Wn7KbSY7wbuI6Ec0sReskFPKtEubQdKpdC6Rj1leJeXnDYBRf/sFLYZ46ZhdIUFivcoBcJdOvSFbcdoMNcy+jqxyHEQUPyGe"
    "z6RXvatc4VhONJZ/eu0hAC2ktvWtyN5YZQDvnvp/DFu03L2/w912eP5/9/0SxhuHgClzmbcwt2gH6lYOV0GnvAIFIWuZEFm3"
    "+tza3QoO2XYBmG0RaEytjswxCo2KgeHibb048bXrDWIan58LZqqu5UGgA4AHEqGAskVCVWEFvLSzxHgKW3VRGuiS65KiVq7X"
    "DUmbFZBXTyjp11oWQSt2M8WpYlcwxKo2UaoHlFb4VRPDRMah52lFuFWjsQDICBDQIQK/6VyUJfSUVPOCLqvOG0gACXxSpgkr"
    "cocKajJJsJmk0IRGfGpjrY3NGVXbSswQ57Ta1YneKbasF7pKvIFW9chmZo5V8shGS5Zokq7dIu8nIo6uJDANY0MlmqL6Qjij"
    "qFoU1BLh9ngiH1mergSF4JV/WZzNXZZRAM8KRRqKkmzhYy2EoYsju+g94ya6OzyFjvpekG2gKjlVStQEUamrrVC5gO6GK/mC"
    "zeRKmx65lKriLZ5DWajGrTQC7MVUGbYWGHNNRWBgksMKjE6JDnQ3ZuLTkPTnuor/AP8AeUpCAugr4QjIFQS55EHyg97T532q"
    "FPY3ewKJZ122h0vODgBl9hd2ywKP71wXBplpacg48zCaoDRryggK2b/gX06Ljqu/4e67PH8/+/6Rbl5QgyshebjLp+kejrzd"
    "HSUB3wnRi3YwkLWBB4zysbe1WzldxHDXFNyk60ew6P0lZ9IS7Vm4iWCp0sGKYwh6TO3Xilkpd4+0BPLpAz0YzcCGFW86HTw2"
    "bty7iqQSxTyWUiLrRVU8ElWl2rbkLZmmfwDJZFlQLpD0YlLo85WR29YkTEKefZGxTnzlUABCuCYjWesy3mbcHkQItBcp84VV"
    "x9p3tspVV9sQBTknZREFxh61uUWCUeLvdwJdIPUNQNKFyq5B+kRWbqpMzESXeZDsOQoGtwwAaiFvF8tRDsPqTXRK8AfSHhTb"
    "+4gzS5RdNJBrFX5QJoA7Rt0gTQEp9Ym3Be9Q3grrKJq/OHMC+UOAK/X/AIF1YlSv4YhzPy5/h7rs8fz/AO/6RY3GZwuYjIyJ"
    "FlddQHLiOYn7+aD1OkLnAOkbGDmRPKWsawxqDoMn0lCi0r5jG+GsLIPEDdlIT8JDYFEHSXtvxBeJU1aGAAVXU/8AhyejdFnj"
    "SbKl2cuRVgGilrkEMgwK8QcNuQFKbE4ZoUi8SrqkMiIiOpXAIOVty4ABVUAKoCzdtlzM0OSbwDmx/Cq/5gGBSv8AOsyr8KlH"
    "8K/wpco3UP5V/wCPcxZW1XMdb2xGv4e67PH8/wDv+kW2nJ4hl3JO+oagtRQKqRI+ZEvZFX7EsFfkp88j/wDeRmz6rhx45vH+"
    "YM0wOg7Eo6Q1um/RQKC0LXZ6TKiNq8qVygsEGFokMtAxQUBXFOCFBe7txXmTgdxXSgFLiPV0Co6BQKXbgFWtZ1d9xKpi22FA"
    "Zzd5yMk6qgAtVxys34UD4Oku5FhQFaMoFZrQSJBtouYaB/qa64sYlMtrtIlP8Xuuzx/P/v8ApFtpvtMALEgg8pBmxlePvU96"
    "nvULDtWCLArHKaXQ1bkPnCVmQVpdNJYNg1cTDUwGqcDSIVWW1tyKUAE+SoCUYwYMqAqt4AWtk0G7cMxqhEXYiqUAFo3lv+Aw"
    "mAtNVGhQuVWDQP8Aqd2zNB1lyl8qjc2vr4bfw912eP5/9/0i23hVwy2Yesb28GFFAvENYphflPqQwLTCgOVlbAuHOaFGOgKr"
    "WILWw6DVuDDwMCRwy4A3lLVb/wDp2ejNtDnY6blGY6+0F5F1rwNhurLbxlmDdWBdVlEuXLly5cuXLly5cuXLly5cuXLly5cu"
    "XLly5cuXLly5cuXLly5cuXLly5cuXLly5cuXLnYgY0oFrJaM0TcuXLly5cuXLly5cuXLly5cuXLly5cuXLly/B1Acf0hunKO"
    "fql+G38Pddnj+f8A3/SLDUslkvvKJVnCUYLyIoFoBah5pL/2KK1dnPhVpdiWZhiDbCC+y7oFZmxqC/tVSgQsZSgyryrwzhVT"
    "C9QFoWXKimNUCYsbKqoNgbWtV90GBNEusF6t6ynVD1hYTOZRYWVgtDhEzqIWdCm7FkqwKARPcQ9xD3EPcQ9xD3EPcQ9xD3EP"
    "cQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3EP"
    "cQ9xD3EPcQ9xD3EPcQ9xD3EPcQ9xD3ENUyy1axAAqKHSVaWuDFpMt8i24KuqI0p1RTqinVFOqKdUU6op1RTqinVFOqKdUU6o"
    "p1RTqinVFOqKdUU6op1RTqinVFOqKdUU6op1RTqinVFOqKm2d7mk1Akauk3yO8tdwxplksi5/h7rs8fz/wC/6xZcgKCxGGAr"
    "oLdioNo3UINzYMrDIrIu+dPykVFJwMg5zgs2JxgDkAAZS1Fu8FWmDyqqtABar5r/ACLumApiiEaS27SLbZ/7IiIiIiIiIiIi"
    "IiIiIiIiIiIiIiIiIiIiIiIiIiLgkgb2gBNxuweCsNngaSKUAYAAAP7vlH+dfw912eP5/wDf9YscJcwtrLirouLcFOeagi2F"
    "3h7S9jo/M5SrzVR7bYzmfmuesUcsPQiItqoV1ecKW70nBWe8dWfWGWueJt6VGh3OjPmTvsOk0ZT8mFtae8VzVlSEAVHBu5eq"
    "bxU5qfqZrVHVmLWxqo4vWO8wUNOvMcPDsRyLbHgl0W0cVEqzCF+cuVh485Vlmu/gb1jrDW/oxK6/iVxTG7z9yFK5rlfMLciH"
    "nDebfKEAdXFAtgXarOuq+krG/wDiG06PKcVn7QHdnoe9QFaM+RczQ1XI6eUpklHDdwzoDxeoNClNXTLALT+JgLQvmUljOIZx"
    "AbTHUQyNW52xEBt6GSC9Va95riolQHgo+aAgWc1E2b6IlJJjkhC7oMamXD3uKl6NXLzjJ1l5OscdYmQu6GNLgZPPMo4Ny8dM"
    "WXzNVixMv+RwF06nnAVA3MPLPZu5WQFd0M5NG5ZVuO0cNdev9T3XZ4/n/wB/1yyOn2WmhqLgR0kYa4g5Ca2D6sudgL/5lRFL"
    "u01NcD1cXH2QYsJjS06qIPRUcBcsEEyPpFrkvOt+RK+G2NsTAAOkQNIoG3mO0vcEQuVdomAiPRL7bO3JPOWAV0DH2g8SXes1"
    "MwO4Uw3ivAWjiKXzhBAQzpBSNlackqWLwdlAg3RlH3YHrMVgiOBHdbViK/aYmeDZUILQteeDMy+uGbDc1RFIt8kuy+I2Ob3U"
    "cAIuaqBGg61iZm16JZHZZOBHMGgG9BqL7egqwyyZdilmqrzED0WeS4rTzZZk7AGrFTGtB1KLvButw0q9Jp/Eo455saesMV9q"
    "JHFopc2oIZHtC19MX1RskCXg7pcRFbdd5eWumdEIKCcqMQxWPYnMEm2+JiyjelbZbijcEwELi+2IELz5sRbZHQVLNqBdQKl0"
    "5bVUspoK4f8A5KAeaMxYbD04alyA1RaPlFHgHWSXiAZoLuLAroOvvFCB2FkMEKxoXVQKUrgzuAlrDGVAW6wbDcMLZhazBJV2"
    "ETJIOlFI05eszfLgEwC1tTXeKyOjTcspik4MrEiPuMSmhcXX9R7rs8fz/wC/6xZcnaUyFn/FgWy/MDWYQ9MSkArFvgNB445l"
    "cpJLQ4g3iIoam517HT9KGgiFwq8zCs52nJFmqMwq5TmNYk60rkGOM6U2B1FBRsVN5SaHbcxoFyxOITYOmo+ayJT0NzIziLF6"
    "VBYJCQC6SNaVy1Rq7jBSu02uI7bH2gAFh2rdYMt4IApi48uWUAnUj/8AGSE5gUrPnjG4huvEV6GD0eAFDzcIx7Sh5qETRBpQ"
    "3Mot0pyVUzAkUUuxL2esn7Bh8SrYjsgJzeAZzHD37SxosUcqtxqXo/mgiGfloCPPWLmjC2HWArCtMWDka0jerSBhl0GF4lSa"
    "Wn3lNPRKoOwIGNCL6PxO4qrc4uCvVtwHLNFvE/SBTZdW/tOR2hQRiT4Dh6wrejQvksg/DN2VqIff7lwRkByQHNx0nEpKEFxY"
    "t0MeAdZgNkVCC1Gvomb4ULi6YYk75Bv8yyHrAHKxzMvOlLTkqdJzY/hDiNciI6QaLNVi/KZTAHThQcXJWzrGwGMpmxCy74tV"
    "xpIWvAHiEh7JmQ6vWWmHFVbzhazAqF6xsXAKAy0owaRKpu5aRTeYbr0FxzhA7Jj2g7maAYEDylMSRqPcRAWRq2vOY4xLgxjA"
    "jNWrcQrumVXrMFgrd3+kdiHq8JnnyWhc1AqEEvVn9T3XZ4/n/wB/1ixzKdjk8mWEHNbG2MXwFE4H/wBh9kAVqFQkagQqaDrM"
    "/wDUQoOjfMZlwqEDEMHQXMZDcW81EIDc2hm4RCpEJc15kzdjAGnUFAFCtWbgs1awGh7RHSze5vN3Bxlo+HFyhvT1g5xCNRmj"
    "KauI+wr5QYlGBrXntmZg2KWMnEY6gDe4NQe4QJbgYIDOAWOLBLQ+vMzmilcqRfVnA11bMnwQzeliGTLWUhDJGr4qq6x5jxOT"
    "Lq8g0DhKrDad63KPUlkZp6IGYz8qY9bizcekWe6yXXGmP/xoDG8wcFmMCYwcy3HDFgvVlO8zozzUCDUNwV/gjTlELGp1+tBX"
    "sAyX1IrkZvTEFriHQdusSmugMU5iTOcUDyWAKGttTtmOu5C6FuBbSAci5qGAFVyW3UWnKobtupWp9bKpjS63AXrVQQOQlDbx"
    "FeAIDVyCxv8AUQgKk4L9xLhvU3be4sYSy+C2C0ouN1kLTmXQR3EQelLlDP0ltUK1Mtkq5KiifvUmsxFoJXR9UJSrVuqzDZaB"
    "udQl1FBjnkXMpCgqu/5ZSfQHvwrBU6qo6WkSh+MyGiCs+sNsbm+tmPUiXUIjaabS2haqTbXeNAbBzSEdvpQNXiINcwFua2ir"
    "A8Fy/hHYhg1w+cyGpqS9JMW+e5bzKoeN1vshgSZUJfXUBqbesx/U912eP5/9/wCZBxfJ0EvJwefJDCALEbE/mGEBy8/kYOtb"
    "z0SgSkdYbyKs3CANk3HchHEBU0YzDFhabbL7S6S9GB82U11wMusOQwu0CN0ndl5qVU4zqM+TI0+bhm2ncmdROB/WWhopvrXN"
    "FoChuyvhzchDyLFTc5gXyoeKalkQeVDKu7OaYNw4WyDCOcAIXclM9V+1YOW7yNVDfn+n1haf95drhjW3B8wiVlKc3WoCOqWi"
    "pXW0Bp9yfMtbAWari/nwC1U4qCAszAfWWILD3RCbJU9sVRXZzJ3cdMnUesYlmM9doZ+ZrUjRFrXlHdW0XqdoXJUC+gI8tMzp"
    "5EHS008ovBcR9yPrXlItMcCL7swRuQbljknsO0hnVjyRVqATggPzoNh6y+61rUXyhKkmmgqGYE7BByF4jhSoNYY5ms24vtNr"
    "RWvZmL46NSl4Ld7PKCwLREqZMx0TY5N1wkEsKixFa0vJepIZgq5D1ZjvfoGDgncHFVUZesKmfvBOWSG3dgw7XJ82XDTAdDzl"
    "qEbt9ELN7hKBj+29iN0IbvdHiVJK3VbCk4G0QPtEVxC/kSf9CDTzQdFS4OHQniWmoM6otgCY+TvNJf8AV90KIoIl27sK2Vog"
    "f1LXLWbNY4di+fHj+f8A3/kEAjY6TwcsKycGAUC6Au8EUN2jLxwRKMKNo2VT/gsssssss8UbmYq0kUZvJliKI7kiqdmTX0yN"
    "q5gH01IUBUkAbM5LK6ZNXTJjTRgURczUmceOpF9zMczPe3hPM97SZB46kAbKvhFMVayYKmA68Esz3tqSq5gFGN3Isq5u/CAA"
    "YPYyBAAA4B8GlV9cmGtJMFTA5mkrJnuYDEUmA1i11WBUcXgOFwHExJRTXxnhi7czOvOQO63kBR3JM67PMgGjq54MAQ1RxjTV"
    "yyZTok/PuTK2MgF1W5EwNgkVE1kxtBIui6+EUqvrk1zFNzKJlh57k2dfhPA9TMAajcixXJkURbpJnuYpfW3IqtiWEzR0yd0S"
    "Z13ZA5i+4mS9+slCvP8Apyyyyyyyyyyyyyyyyyyys4Prhqg1sWvKysG0OohFBoYJFHOx8UKGxVJ5/wCSrXeF/wCf71k6mqPP"
    "/KonNbOpLo7rZyf7xjqRLaoYD/NYku0xhd+mez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T"
    "2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez"
    "4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fC"
    "ez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2"
    "fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4T2fCez4QKj71uXZl/+LAoAtdBM/h64T5F9J8i+k+Rf"
    "SfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+"
    "k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9"
    "J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6"
    "T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0"
    "nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSfIvpPkX0nyL6T5F9J8i+k+RfSBWB5"
    "0RgKOE/zbwnrAmsm1/vFPdaTZGebkep/lEWd9OPG42V1WLaGXAv0nER4OW9U1rmtlXmcxHg4a3Te+L03WJUbK6vFlrJhH6/7"
    "oeNYXhn/ACgQKAo8Kwe3ilt0CgLVrALHRRBY7Sl7sCg0UbbXwNFAFrloXuxbTTbppLwe1iFNWAqKRrIj45aARAYAvCWjZAww"
    "wyYdgKFAIsn99fk0qEEBa6DwUbw53gK4luV8v5J2iBli1S9zZz08VZ4uDhjaqAd4yCpgkjZCxGtnP+iEQUCD7/5vZN9557Ty"
    "d+DaCVwXtdgtXgGfQDbf9IN8+Hsm+889h5uvFLa9c0pgLXabJcLFUYFKM1qIEbJs0NIt0RoDGUMT/F1VzAqQ1piy1AgwNCoW"
    "YgADkAYX5ICm6FXbCnCQs1mSlEoq2G1cbYc/FrViaBbC8TMbFqgSyrGYD1ZbcQvzwApAKrxS7IH2NXM5KwLFo3ghlZ0Jxamr"
    "pSrirYlkszgVgsAF2yMVxX8ty0ADS4AVNUnAe9WKuBCCLCs0iseaQVtFJAUG1qoGV0vHF2lgdNBS7hB6WsAcd8ViCqYJUHRd"
    "HTd6Qc4SWVoG8t4oAsZHSDOAOU22sRTbdAspS1IWlhKkYJZLYgxxCanYUboaIMGhuFqhloH0QXmnKWKpaB0nDQJwieCloiOW"
    "xcxW7L+xAIHbioWxKSjY5KxhHVpt5DAAXVqU641VDYKKgrVKZK/O8DhU4DNYO/EAW5eUrGlZeBoAykxuhS4g3wuGh0XrO1wW"
    "g9tWFZbHnOVBuyi67GKvJxLRIMfp1KhUzbwTfhk44rgXK7UyRapOuJWhK5JbkIUwhIiFK+mwXb5S91ZBI5cBDVuDJmOHXwKp"
    "bG0WUecFBtEIXkDB0pVyKC6XduYGgK0CxXimCW3gB70GVuee/wDon5/9/wCa+7iNwYr1l+z3PBwxzIAVQgKVyX2CJYPTMK9a"
    "8l34V3cRuTFesP3ez4vbCAXmeZ+KPK/+MMBcKqkVvkg71dNLNSdMx/bkxihaipUmjQqMAJxbBYCAMlEDZUuHRhtXkZ3aNVcK"
    "kpsCnc0xTC/WedsDray8AS/OX8M6UNzm/KkHBUtam3yG15Rz4NKGoaUCiLZHtnTKnHZUrAvCgBzjtIuHpHZ5aorNJ0ZYgTHR"
    "DFGRSsQEtpLbH9QIvXkqKWpMLKm7AeHwIkKbBwKBikA8SiMZewbULRQwtvM1V1isEgMitQ8kbB76CQHdHUiqtRBpPCOdVCn7"
    "hJIToRt2Xi4jJu9BITEqDhkaUpTj2qbcSgNVV814L8TGYEMUjNeVFCk3L1WeKAzEoqkjFKqcBc/0faJ5qQx/mF3aC1CW4MBR"
    "lA0Tm8HeJotpevFtBRaMAeriHQXYbS4DFy8Yx2BIIjMI1jDkRDewI2PTXVNDcs6FKL08HS6DQGvsDf0gBMWIe6IZkhVrpwri"
    "pLhpGpyTzQZS1OQYoKjlUYyWl0WhKEzqIWrZtrOsZmAxTHeSGHGketf6H+f/AH/merV2A3o9AcmE5oPAdmjoAoDPSKzZYQXo"
    "GhnePA6t1YDeh0Ay4Di08ETOALV4AlYwLygxSFAaOCF5evOuRX7xyqJ4kocWqtGVWW69ulbeRX0jdH0QdLRqNmP6BVz0wyHe"
    "KPLvJX7s0zk5WX5sjbtJc2pVGsEQpuSnsmzZtW29xrysqCKPOssZesX+ZoKWXS09nMxx+fgFiFrIjp0hxDLE/dU1MuG9sBuB"
    "LLYONPJcS4thxZG1Kqubg9+JlaCXTSFNQ8TW4jJgsaFuNkCyrulq1DZxSINK112qC76hKbAMjfJAl0BjSwzUjSWKQNvW94EL"
    "nLS16MywS6Tw9AOLVUysOrNuDKOThuTCOd9K34K43oAlbbyeAqk4SVVHK4gjrrl1GgxWC1dZgvw5UpTJVbQK9GA64CygGExA"
    "A0HEOgFBu4BrVu8MxM+W9+qBPpFApYa+oavpAlxZ0EKNKhVKKlL/AH3iXnQq0ux0JfZtXbUYLaWG1eYtUE7r0tsaaMXwTNWT"
    "MdWrbAWZXqxcnCznT/7S8XlnfiCOuIyazU4H3HZILVNJhqWjvRJyZHsitayweEVKh9idqgoiNJEifcx6ZMXavCrYrozjBXGM"
    "fwZf6AygAtcJh8M2NHCxQW4MvMw871arAo0iWKQxc3BZQuMiJZjD4lOuuQ0AVexCGY0WI7qq/p4hEZAEAiJeRE6iP/q/n/3/"
    "AJxQcFYUUKgFJTSDhoRxsGRa71Tg4usXsnOwZFKrdOTmrzWmCg4KgpsRRLCi1DLS8T5qmBNgTSJdwgNUgRkMGrgM2lWw4ozI"
    "se1Ayt0YgeMZL3FRL4VdWIBpJuBLU0FJSeajFJ5QWI0sgtdNSuU1QgMsAdlwTFRxJkxDYyB00GJsj7d8aQllo2EvLBvBCLQl"
    "Do01CryV/VEl6WVgrkJwxOB0SlAAVgxUsFRoSBIly4pZ3Bsb3NuawBA2gijGx9CSdei1NbkyDTMbrFKkALQugL4IFCvgcjd7"
    "ftI6b02Szr9YwuYLaDthyvSN9NGJrLjlzYDYVr1SJrLjlzYDYVr1SMOKMyLHtQMrdGJRNcWUdwKQ1jIWSrS89HsVTAbo2rTd"
    "MaeECxuOFLMzM04VYALuXCJaoAZ4FTYpRZpIXZgNOGQyuS2AI87qF3kNsjYZzip0DAdpYYUUQywicPBqtA4NN3gq6ggWESbQ"
    "sN9qNhgLDVtMZQMACw4Ijq7XmE2BacDDO6Cboc+zKgKtM4G5jR8XJFSBtFWK5lthrb8pYVcL5oYptOsLsnzuyJwBjDEZr2Cg"
    "oENSs0FnyZdoNrChXwORu9v2kdN6bJZ1+sYXMFtB2w5XpG+mjE1lxy5sBsK16pE1lxy5sBsK16pGHFGZFj2oGVujEomuLKO4"
    "FIaxkLJVpeej2KpgN0bVpumNPCBY3HClmY0SWwXBYOSp3qVLrfCKCsPDqDO5CwGiyrbKltlMy41khDTKltWHCZrmnbAqhQuF"
    "al1HeonFXkgT5wlruoHPfBh4CgIbCKLuG5iZhQwiAaV1p7h1ibIOS6wZxKxvIJxNWAULqdK9hSkD+i2QjMAaCvBhlb6h7zd1"
    "seUuP/V/P/v+qUn7yXLQSzFmSKKutYC02WLajCQfeRZJ4TFMnZA5PL1l8seYsrqvAPZQ0KAuUWFAvOYyFvjBK2WgPIOKhIA9"
    "3A3NcNOXDVFZ113eeEzLQErVw6hz1gYzyqCoFBFdHgeQPKO7adKla5gkqgUC0BQgaqYY/wCFtqKt0gyGKxEE96uui0wU6tla"
    "4oNsnCKX4yBZoq5lh7Wls0oBkpa28f8AHWHkLrC6LrRNUbNoShhoKwgZq5YcKK1YbFmyq5vDk3NIWjHAQFPFKQWG21C0GmpF"
    "JLQYgsNtqFoNNSKSWgxB95FknhMUydkDkorbKWfA7UKZaqMKgzE0s4FpXo5I0I2MzVZrCmqtla4oXnbazVAKLG21uIxYi10K"
    "1Zb14dG4xh+LAgCJYIIhriPPstbWwVcQL5j6kfbm5vPLFaYb8WBrqh46KIxcpW6wVA40I5ERCQqR84pVoXkKAw5tgXShmoAC"
    "JjobZUe2btnKDZaDIM4Lg6GRRgUEAAvrFaoINILUNC5bM6hYFB79uzkahmr6SFz9eeAGqoJ4QMAiZtkzIslWGi5qrNUbNoSh"
    "hoKwgZq5YcKK1YbFmyq5vDk3NIWjHAQFPFKQWG21C0GmpFJLQYgsNtqFoNNSKSWgxB95FknhMUydkDkorbKWfA7UKZaqMKgz"
    "E0s4FpXo5Iyx7fNAFIl4C1rIYeBx7Au+1FWlt6hM9ODqxMHMZyWXc6Ne/A2lZPLW8xFLBVIbXuHY2wank/JYcvsai64lcocQ"
    "xcKNTOLy3GqGbpShZNyAqrhlfnpWAkCvVqoBty41sV46oB5gRJYQ0BoC2GlxxMvUcbQ3e6AKF1ljTPMvVQsHWF3hIcRoVi0C"
    "y0EeHj/1fz/7/wAyQKdhD1VVU6qwtby8ifUXy6/+N/8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/"
    "AP8A/wD/AP8A/wD/AP8A/wD/AP8A+6nJVlyU7Tj5V1I0yaqqd3EMPS7fpZ2IesLulwuVfIeH/pGk5dIAp5JHrIWgKHeao5kH"
    "O5YfUYg1oNZ69r2XfeYYAPi/I3qFfYT7eckPqoRLb0X5hV9IbWTgwwJacN4WUKDmdi+w1C+wzRexf4hppZ3nLED5jAI8xbHe"
    "ELvOsHcygorqDnd4ACnkkRW7cM2m75BCobWL+a0+hMoF7NccvZIlx3XJ/wBQI4cyroEVcBcVDofF2voIKoE1Zj3pG2+MaPdl"
    "9VLwfs1Y5zU+txeAzT90uzLvPqL+o03fKooM19kNXPpUwyLmFewEyGg7FY2G/hEs9GiPgD9ZHFwUBLi7JF/XxA9hZf1jR9mb"
    "0onb7Dt7Xkv6zv8A/wCN/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/AP8A/wD/"
    "AP8A/wD/AP7oO09TReQdajDlGrV6iARGxPH8/wDv/MLxhFmn5i73/wCN9L7S8MDyGEjgLJ3z5RKYqaCLye4o+sQaJ5BVeCc7"
    "TJ2Yk0F3tygu0YoXcwBgo4S4EVRfNY+0SVIZowsoYW5KWxitZStaiMIFjrqTDR0OkrQvoLDcUdwV6XA5uVQ0B9AqaKJTGlQC"
    "lQX8/eKlAnS8w4yBHKYxEDTgKXUrQKrioAlRcRJb5mcHpUydQcDqbgq++bmG0FOGpY/LpBQDm8Wa+0NAIYxzN2i7bgSpQAWw"
    "4HrAAlF2pl8ukpaKDnmAVQBeZXBacM4MV/47a88QodgXI3hQus+P5/8Af/qljF9/8V9iXKev8nMHc4r+GGk4D/FvEZ5anlr/"
    "AMn3XZ4/n/3/AKyW4r/y/ddnj+f/AH/+CFvddnj+f/f+8ltVYxHMwsMUXsXV6me+rJdjSJSVXFDGPA1wAdLDFMuzb/g912eP"
    "5/8Af+7lunSAp0riaTJsbk69IC3SuZpMOhqzowQwCB0VoAIaWL06QFO18TaZNpc/xe67PH8/+/8AdSyI2PUyidlYGlFGxOvS"
    "At0rmaTDoasz315LobRKC74hI3VjMcTKgzVWxV3v/J7rs8fz/wC/9zLY76sE2NKNpVc0MKvNNa1pVFAOi0uplzumkjQ0FqCE"
    "qxgHdWMxxMqDNVbFXe/83uuzx/P/AL/pltSJIlFkV3kWno/69LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLL"
    "LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLmqfAa6YlijYo1YzVWMTzMLXFF6N1WoCqrE8U2Q0QJUdowcs69O"
    "1lwVlFsg/wAcsssssssssrB3lLsUhBmwTDnDX8Pddnj+f/f9ItibgCzFnQAVgXptM+gRoLWRwgBa0oDwEWpcIGFzOOsDcV85"
    "drLt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59"
    "vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59vPt59v"
    "Pt59vPt59vPt59vPt5iXSUNWhUAFV1NMbr0gKztBRlh1UvORgnFUCtAuOBmvHiBkN8KEZBlYqdvPt59vPt59vPt5p8fDIuUp"
    "3i1hLQ6+BE+g3c2doJCRYtUJA+QtuboO2gBbVxR/D3XZ4/n/AN/0i3MGLAODoJvV3isQ8DSRSgDAAABLDfOo5VGyA6sIKChO"
    "A8pV7lEpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkp"
    "KSkpKSkpKSkpKSkpKSkrORknQWgFqGeUiixhGEBQDiNAEwKVHyQBPyrYG+FlBORgyfr3CFWQo2lSXaLTUWGtqs0NFGJSUlJS"
    "UlJTwU9YGIEmbC3aMUmOpqNju/KLnEcQ+QGx/Bo0AqgF8/w912eP5/8Af9QsrzVWuoyMp0lWgrgEAO3+p1nIyToLQC1DPKTg"
    "YMH79grXgglJvCjIRckJTAspXM4r8RwoNwWiFbDKysrtFpqLDW1WaGijH+XiKYX3TpBeoWKO4fxx912eP5/9/wBMstqckrUU"
    "GhgK1r/USvU42CNja3mlRWgZnp2qlUEEsRpBllPSvSVPbiAKrAwbwDjAVSPkFkRyG8KMhFyQlMCylcz3MX8Sx0YUU9a9IW9u"
    "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"
    "3enHaGg2hJ6rmWhLbaPWpYjK7ulVAurXPy3DYVLYPWohPIlbshFz4Y76hoaRpodJoIwgf3n1jXAiov6RAOcAGW0gnPNG/FCL"
    "EH7YBHxG1zOq4imF4qPqHUoQAKAorpU188BfcbY8D5MeRXULCstvXtMlrCm9VNsF8AF0H/qO/pPzv4T3XZ4/n/3/AESx+Cfg"
    "f5EueesKc5gSIdlxDxNpQmKqx0iVu/KUcxKj0lEf3BTm15iVGskpduZmRA/YtmZWWKoPlCljQoOgGoElLvmKzywwetM98f8A"
    "ID7z8TbCxYfsSkoI27QpXL1lC3WesocRH/2abgBgJUo6QBoJUp01KhRg6Q7mHEKhYfcl+gOP/hFPYfiBkvrR50eCpRfjR0lQ"
    "K/8AUd/Sfnfwnuuzx/P/AL/olj8E/A/51eB4szXVlZ6wK/1Z39J+d/Ce67PH8/8Av+iWPwRC8tJslNKTLMUjr/bXf0nZ51W8"
    "FrBeSxN/w912eP5/9/0Sx+CVgmeSFTkEG0E3XT3eXlJICzKAJUYI+Nffm/2T777777777777777777777777777777777777"
    "7777777777777777777777777777777777777777777777777777777777777773uP48+++8x4C7CioBUIOkrRYuUXBr3C2w"
    "KuSiNPH3XZ4/n/3/AESx+DwWehAQaeAJYopimCPhutZSxId/9jYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"
    "YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYboZVk+X8Hd1OOwVnQLBG7cAqhsMK3TVAYAAANfw912eP"
    "5/8Af9EsfglJfqamXUUDaLItZFHQDGERYKVZVUsD/Z77773333z333333333333333333333333333333333333333333333"
    "3333333333333333333333333333333333333333333333337v4/fffe7JI0tgCwuwAawMM6NhYpDAAir5J/g912eP5/9/0S"
    "x+CWn5WRWYlXIG2hjQb5IUXBhC2jOgAAADwq5XeBUWmXFpzLvyjRzNP9cT7/AMabBBwNjZkRBBSIIiDH1BJDhVREiwAmkw/w"
    "912eP5/9/wBEsfgn4H+RItiZ+kyIeU/aagvP1o/V902ZYzfcx/sU7+k/O/hPddnj+f8A3/RLH4J+B8DLjlujrFpguGRqsrP0"
    "NowhXgveo9SbbEdCIqzd23rNcvnJ9zKNq3gfQMqxIHrol6wOaWNY/UmwX9o1tcdvFZLqW1xfeKHCc8fuCt5zFjSZrjnylxLu"
    "sf1LAyUmEheK3dL73LUXS9v40A2dgltKTzPAw7AVUH1h4v6QH3P50ar6xRwXidKlkvN21uWgYYajdNbjhZfCiNRtx+YoCtQ1"
    "/wCK7+k/O/hPddnj+f8A3/RLH4J+B8Cym5Y51cq3SQ2J+2SnmIsPoNT6SdR6AQE9XuTymaOeSCDoqstW6NEOEK7JDOI+Amrt"
    "P1EziP8A4Fsrq6EPgh6wJdz7yt8+OC39Irpy5Kb7RaNCVKhrLN4d3RfMBJuuLrwe0pOQuvnzFqAev2lkQNZ+hAPX6kuUOv2Z"
    "Tv8AaWMr3+zBuLUpfP2ZQ6/aXK9/swD1+pN+CWxwq/CL2ucQtXllVKSNUlwogGO0pXP2gEvP1Kle/wBmU7/aJYVGD+Uw5FlE"
    "L5qBev1GGgl/X1Zm73RvfLzLA3fky4B6/aU7/QgHr9vBTFWPTRdQ1MC7A1dXHCy2FjtbLKWGMOrHMN4tOe0GjH1KIR3NIQl0"
    "XgiRFPosK02/+M7+k/O/hPddnj+f/f8ARLH4J+B8GEMJYVWpy2gPzLdvVbHcHRiW2802sQWjXocWSoWwzHYSqY+yF8HdDo6l"
    "Lo0kIoWDvMqCVKWtZhB66a85VyuePJbouIr6fTxdS4Ixl+Z0LZAoi28obM9rwZa87JqQSoVQIMFVmgW/qEjSq4NWekpcUFY6"
    "h0ikG+GzSDeDDmO4CmpmkdgpSBQ0oRGw53GcO0Lx29ktwJIJASikzEfk0UpZTqWqJpqm9lpGXHlsQ1Gcydw9UaXuMbOSxepi"
    "DU+7KQvMsKrahYwfeGsWRQaUN1L1R1UtXjDBLBk8JFxjMzjGXwlgoODwdkoyua4V1iKzICuN5ip2tf8AAA3Lak33wVRiWrUb"
    "nwWp0gazrelq6wVDajBO1mo8mTJciAMPBU9ppSgzAXsRAILKekJT/QCYsCY61KWgjPq6CCBjMYCMgeYvi4dI1QcUw+HEZOdj"
    "mo2aCI5qyMdHoj7oNLzCbrgwoAaiqqhx1BIbYL5Zk38GP7kWqa5dNaaix6ALN6ocTN9k+pgYzERlp3IN1dqD5f8AjO/pPzv4"
    "T3XZ4/n/AN/0Sx+CfgfBpjcAF03GS5e/mJcCUnBXEfyYKbt7R8lWzyD3mZeYGl7kaFKvWNWrqc9DXV5zGn7bIs4IFFQVjWy5"
    "bFar6kiA98kVaRyo0Lw0r5xaMzcdMWyatc7nGI5yBy+kyCYU06ZZMNns9fmB6Ct3LpBjNue4plbnZDsHpOhT2wrjmPlK+KRq"
    "M5sNAXlKRvlikgbBgYndmhZzdagEFAXLV0Q9gaKfGejylh98yir0DiEwpOVyCp5dagTiNc+sKTX5QrvBRV21GHdLMlG4iWiI"
    "l2eJaMkOF1erK9jjoaNxsez9RLQg2jRA+0wg3jwA4YVZZ4+WoGy7BFnk4jbAsIuyzEbXYmzPBMRrAtDvxGNZqX64JK2tmsPN"
    "D/8AUBHRbKxrMFhsCNSlQtucQVhsSJK+Gooo3AcWBF0c94r7BjBR8VzQIW6MGMXQPudJoZ8YdhKJ6i3mi3lzQqyR6CAMjKrC"
    "Zy4iCqgWnnE7L66FmKbssETlbK7AUYB2xKpUNv0sIgCqH+0KgAvSgho/vDhHYcxpfblhaqphVw0LBvxd/Sfnfwnuuzx/P/v+"
    "iWPwT8D4inhm97R7yvshdSku/RyuKPN7pvXbFRE2No785TdM6DyMoeagAEFjvQMOoe77d3dfsS0mUd09u0Qz0JfMpdbI077I"
    "vBtCtuoLR1fAWiMKg4db7ENnsDahyd4aRsNx3daqK7phdBnBKTt96XS8neN2AWOLkD6RzKg9OAqgMXs8+wYoYt0jBwNDTEC9"
    "6wDRbVQsE3Rwt4DOEAnE3TkIphBVllugj7wPVgu68oFNG2gpMA465idyABZ3jVwbSFgUvve0qsfqg4bOq7RRln222Q/WGVaX"
    "Y7Q6Fg/gwD997jAOSKB8OTMlHEpfO5NnOCLoDUSe+JadOjrFNsLh5gGrGNQhlkYzyowyydCJX5NRm4rHEz3JZ0UjOhXMGptZ"
    "dyDjiEsMQRrKuZXnMS5W1sjOqlJF32IVCBcDGbKmHDNCPWrJf+fBPTiZW9Oyya1B5dCju0WA9hYaERJbC1R1Q0TCFCSDrCWc"
    "dvFzlH+UjKFvEJwX2xSqLICEzeg35mWHLKEtH0ghOAN2ECty3FbxuvJCfDhbwKO8uTtcjfYhWgRkDsuGj+6paMvwcoQyyOEi"
    "0BwEftXh1gV4u/pPzv4T3XZ4/n/3/RLH4J+B8HDrNn7PviVFnJ9248l5iXYJF5lshYNtG46y/O35LJwdjKvmOZpbaOhnBt3/"
    "AJktTZKHcWrBgItNppHykDRCckNRLKYaK9BH2YwfdjL9CNXGaxn7QACcMn0Y7ZshD9on6ag+1o8Vm6cPZqyUc02S+tnF/fyX"
    "PukffwSjeY0Rm/z4lwRTdV+kCasOC3mMt7UK0/UIkvG6DbrdTC05VDA0rV2TyM/WHn5CFl11y5hd3YFXMqi+ryXMX7puPuCA"
    "CqhU60E/MZLrhbz7QGiCdJY6m1u3nUD1NAo7WYnaSmP2IgJiPkhv8jJcwlij9UEEHHMB94gXW0IfoRt/9guSbE8MPkali2O8"
    "p9pujAZPozSkQ08kJRqPtS1H8GWGhv8APgXKREAEpHJGghtVV/EUsF7ejDodon2CKlZYRMMHGTdsr3xmB+ORL6EpWqluLdu+"
    "6TU2wwPI/vVbmICjyJvksicMAp6wQNBR/B39J+d/Ce67PH8/+/6JY27zHyPgyqPSSgN4zLdwDoiKhemVwGJKiEkWaGHO4mQ5"
    "D7aGBK3EFPnuWS2TsM15+czJ3qulAfpsGZUIp1MGZwMQYJthxgh3YrwdQbllwTrNJRqpsZuXbeXUHvnvK6Q87l3DU8ohzubl"
    "Y7xtxg9CctytH7wyOAqqKOPvMX3jjPEKDGoPPHhqXAdbl56S4oO/pOf+QEpdcwtAOm5TGdwd9pcQOYZzcB8F7xaJcuUz2lW9"
    "ZjtLz2/8Bu+0Dggx1xVqEtMNuTRgEtJSsfwd/Sfnfwnuuzx/P/v+iW5Cfh4JkVM1y93RG82dFKq/9jrz1B1S5f8Ae8bifk7l"
    "ALC5UgbHWI0IVdCcWegMDGINwXk5b4gB5Jwy0ch4hq42wLa+80KQCU70cEP3QNuaVFxCsy/FRSIR9Lt/5NlHEILIbZQWpoJx"
    "GSB7LO9aVsO8Z3nWQfKI5hbNU6wgQxbXaD6UTDAQLaGHtTuWHM2rboT6Uy7aMw1qNtlV1ha6RO0om6qLaJVHMKKnMaDn7y7O"
    "K5qGr30lIApObg3rrEV7eDGVGFAxqLayT2ILGGq56zbcWm8B1hqxFFFH4jUScsYTVVyS9ktRaPY5lFNjFjyi2ANxqn83DO1k"
    "FYx9Jz4N3wHWX+hgaQDTWWOIJIMeqmq88yzZkejMH14idiGnB98Y3Wjn+/RnDKeRcM4QhRAsAjwy2RtwWK8Xb5TLz/4X3XZ4"
    "/n/3/RLVmOm6Wim6pZolW7j53kinygXqtmzkxKVd1NEUV8uIrACMSUNEADToJyUrHkj3aNmAw8TO0VFig0QM0V0g605jc5t2"
    "OzGu3A/sWp+cdoxk2asLZc/UNe0+SVFwsYpy+8PV4OoxMpKk3EUjaMsMq7qBjmLMvCwl2tUGs1FN0dboQ9YZkEGF+UwLiXo8"
    "xFUtbwi4qQaQvPtCzkBFMzSruYpGDzAF9GZ5Jysd+Zh5a1L+sB00Vvy1Ay+bJbrBDpQPKYtWa3TuIVtSbA5aFR5JGOsVALWD"
    "EKMkDLHZ5gszxlU8oWd146eDoKQXVxPKsLziZRp4tiY0zYBrEU63GOXzUzIpq2iyyaAsmbISruQ9juerTL398anQ9IfRIAED"
    "G6gSnjEXp8Q1PIlnWla/vqHyqKtnaDfATo6SiDbWR8HOI7dBbdVcahEEDaukXYG6JXo2VzTyhRCUR7FcRgn71kpPlgRjcA99"
    "b8P76XzKzcqIZRKrx7UQmGl/hfddnj+f/f8ARLOvBUackWaY7RzS3hHeWNXFTjA89JnALSrzAtC0xa334hxyNjo08wtkQGnX"
    "mcK6LVtr1A44mkHrE/Mrei78Q+GaZq6LZYnCryzGFOFlSu3ja1NQgarC6zCaDoDDoyw0AT77j0YUMK2SVdxj/qKVWIBt6HcL"
    "imBE4KyosqDsVANLVrN95U90GK85lr2kQXvH/JLAHdd5pnYJl9P3GSICreyDFjIF1AkpZHEsbWDR00HOEcylfg4NxikG64iY"
    "EMuAquC0OVmmC12RA231qC/oyaZGFscf0I4kLYC1PWAgvAV94o1bCj6ULwvHJlxMKYnpiKE2BYhBUnLZuO0q5s2EUcDcqtZ6"
    "xeQBU/7GIoERRYkmXPv3KT7E2+8s4S5dJ0VW3XlCGtlWXCZi4sRTt3gSDIgEwuJU3gxo84SXyLgfBVDIsJXUuZJOBlabhabi"
    "IDwRXdmg0MqEom1w4hHJ2C5lUhRS/OYKvreun/j92fxS+67PH8/+/wCmWXZzDPlKIcmgc95siOTjo9L4mEWT3BlNPnLVQ4Nh"
    "36Mq0c2Ye8vCD2FdYQGRbOBjmNJdW2md2Lgux3dxqYCCFfMI5TpCBAAAMAcAdJzsp8baWdWWCr8p6hDXsEab6xDd92XXmDhW"
    "gWL6LqLdI8oQNdrNAIvLPQ7YhQLo3Ck0aKuAHKZFjB9ZF50UxZLKPrLiBXLlhyr11GhtozHeh1ikbJMxOQBxrwcN8CMRC8CM"
    "W4wLRC3HtKksd0emYDwZZYdzFxccrepEC5ViHYjDbP0G49IwfNpmZ06WJUDXDL6NmXCNY7iMdUb5NdYwt3YeL4hdwHTeo9KQ"
    "XaZ5mKg7rIxQtrSInG7Vkt0Zmdqgy+c2AfFEUZEtt5yyPyLj7KUmnnNz5V585udk+sGHM2ykOXDhXa58cizR2pcvHazF+kOX"
    "FDIRqudnQ9ZvP8o3Fk/VyJ7ykyvMVPGeu84WpOl/8V1HWGqlI2EFfwD7rs8fz/7/AKZZCohkl6NEupf3iW1XiouBp61DIhRR"
    "eTFDEsn6owRcoZcn16JZmmLWOhUS0+sO2UTgeRLKukK47lHkjW0ousLZ/cNX4pZLJV+feAifQT+Zk41pUKqKKUBxMxwb5lMd"
    "tRLcvXGII229aiXuk6RKVX2iV26CGAyCkqvhEo32UPZupRV8BGMu0+Qa6fWgdLCdOaJOxdCC2ZCNDtz3SuzArlelhNSDpvJh"
    "gcFbQHKsoRCq6ShShHY8xij7xLM/oBtdoXE5wwTa2n6SxRjeGoupcVqwzJebzy11yulSlR1n4+cp4GAGuZpTmV6A9agRUwu4"
    "5XiLbv07Q6Q3uFRA33mDJjvDYCK+fnOVl+UKJnQ585ivq40PQHaFXQwyDm+Y22X/AMgDBUripXMq67SjRr/xV6ZguEtzLyXK"
    "/wAcfddnj+f/AH/TLIM3phJMwTfcQRMajgl4iW96xKGAVbIiKH3QKZbDtdwi0UvaUrGfKKQLgVz/AJBPjKmQCwEMgG7uhTrr"
    "RD2bqUVfAQmYrBbmRVMpojC7mHovBNXYqwu2Y8RqpCiwdUL2SqKsnhWfA/0TtNs8kQpj+WPuuzx/P/v+oWXGCWZ5hpjNsCcM"
    "V1gDvFPOEUErMA0K8o5dSpqXL8bly5cuab5iOXsMCMReWpl6KyXd0ApKpioOrlqs36ovMVJVmHovBNXYqwu2YguXLly5cuXL"
    "ly5cuXL8Lly5cuXLly/4XLly5cuXLly5cuXLly5cuXLly5cuXLly5cuXLly5cuXLly5cuXLly5b0l57xa2hFN0okF6fzx912"
    "eP5/9/1CyPOod5o1FDUcS9V3lE5VL7Q+DO9+0737TvftO9+0737TvftO9+0737TvftO9+0737TvftO9+0737ShAQxTrXEUmb"
    "huRbiAtBC7lgvasU0AgtDLd75i0xUlWZk4Sat+iLzNyXJ3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/a"
    "d79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2n"
    "e/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3v2ne/ad79p3"
    "v2ne/ad79p3v2ne/ad79p3v2ne/ad79oqVf7TTL9ERH80cyjtDBaX1QOXf8APH3XZ4/n/wB/1yy8sTHSEq3FyzpLOks6SzpL"
    "Oks6SzpLOks6SzpLOks6TplgKdK4mkybG5OryAt1rmaTDoasz315LobRKC74hIDEMEbFEEL3cyWlyzpLOks6SzpLOks6SzpL"
    "Oks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpL"
    "Oks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOks6SzpLOkXGN/5M"
    "fddnj+f/AH/SLDWEnBiReDAXl42F/kp4GnDkRERpERBH+GOWjrFouxcVuNCtorAykA5Pt4KG2jyil1b9pSrWfNqPFaen8sd9"
    "WCbGlG0quaGFXmmta0qigHRaXUwpHgZEqwIgDZwUSbqxmOJlQZqrYq7346GiMPWUd4Zs4Hz/ALGbNnk6QpU2ujGIlCtXsXOR"
    "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"
    "ad4bMQ7ltNMTpEt/3XXgGqdu2ChPh/am2I0KJYAjqpEdsQyhbXw6MZ29p0L1mE+pYrtGq9tKz8RS4maXqSlB6C2B9CAUBFum"
    "LCkP/ZSPXcLdgI8REapw58Pz/wC/8zM1Y7mQNWkUGtnEsnAPoEOnCuQUrtQ/zmmmmmmmW8GGy+PDTGkzyOFtu+swDz8/DTXG"
    "kSxThFirWbBWkwBUYJvKYI8GmW6PKYDxYYYA9cylQlowUZbwaZSTZoy0YA/6mzRgrjBeDTLXfVN+BUwSDCbUjBFf4Wmmmmmm"
    "mmwV4MNijLEbAHD/ANjTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTVcVN6rVKIK21XJdYupNYNv"
    "rGAjQocLvw/P/v8AyZWtCnN5/wB6J3he9xVKtrtf8i1LLZ07wQCNjpP952SG65f8yTWTamrq+5PcU9xT3FPcU9xT3FPcU9xT"
    "3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT"
    "3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT"
    "3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT3FPcU9xT"
    "3FBdn2K/cHw7Zz5//izlqGfsMBwN/QWWWq1VigqkTKi7KaV4+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s"
    "+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s"
    "+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s"
    "+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s+G+s"
    "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"
)
CONNECT_GUIDE_EMBEDDED_JPEG = base64.b64decode(CONNECT_GUIDE_EMBEDDED_JPEG_BASE64)


async def send_menu_photo(chat_id, photo_path, caption, keyboard):
    """Upload a menu image from memory and retry one transient network failure."""
    photo_filename = os.path.basename(photo_path)
    try:
        with open(photo_path, "rb") as photo_file:
            photo_bytes = photo_file.read()
    except OSError as error:
        logger.info(
            "Фото меню не найдено по пути %s (%r); использую встроенную копию",
            photo_path,
            error
        )
        photo_bytes = CONNECT_GUIDE_EMBEDDED_JPEG
        photo_filename = "business_connect_guide.jpg"

    if not photo_bytes:
        logger.info("Файл фото меню пустой: %s; использую встроенную копию", photo_path)
        photo_bytes = CONNECT_GUIDE_EMBEDDED_JPEG
        photo_filename = "business_connect_guide.jpg"

    for attempt in range(2):
        try:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(
                    photo_bytes,
                    filename=photo_filename
                ),
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except TelegramNetworkError as error:
            if attempt == 0:
                logger.warning(
                    "Сбой загрузки фото меню (%r); повтор через 1 секунду",
                    error
                )
                await asyncio.sleep(1)
            else:
                logger.exception(
                    "Повторная отправка фото меню не удалась: path=%s size=%s",
                    photo_path,
                    len(photo_bytes)
                )
        except Exception:
            logger.exception("Telegram отклонил фото меню")
            return None

    return None


async def replace_menu(
    callback: CallbackQuery,
    text,
    keyboard,
    photo=None
):

    owner_id = callback.from_user.id

    old_chat_id = callback.message.chat.id
    old_message_id = callback.message.message_id

    # Сначала отправляем новый экран, чтобы при сбое старый не исчез.

    try:
        if photo:
            new_message = await send_menu_photo(
                chat_id=old_chat_id,
                photo_path=photo,
                caption=text,
                keyboard=keyboard
            )
            if new_message is None:
                logger.warning("Фото меню не отправлено; пробую отправить текст")
                new_message = await bot.send_message(
                    chat_id=old_chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
        else:
            new_message = await bot.send_message(
                chat_id=old_chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )

    except TelegramNetworkError:
        logger.exception(
            "Не удалось отправить экран меню в Telegram: chat_id=%s photo=%s",
            old_chat_id,
            bool(photo)
        )
        try:
            await callback.answer("Не получилось открыть меню. Попробуй ещё раз.")
        except Exception:
            pass
        return None
    except Exception:
        logger.exception(
            "Ошибка при открытии экрана меню: chat_id=%s photo=%s",
            old_chat_id,
            bool(photo)
        )
        try:
            await callback.answer("Не получилось открыть меню. Ошибка записана в лог.")
        except Exception:
            pass
        return None

    # Удаляем старый экран только после успешной отправки нового.
    try:
        await bot.delete_message(
            chat_id=old_chat_id,
            message_id=old_message_id
        )
    except Exception as error:
        logger.warning("Не удалось удалить старый экран меню: %r", error)

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

    previous_connection = cursor.execute(
        "SELECT enabled FROM business_connections WHERE connection_id = ?",
        (connection.id,)
    ).fetchone()
    was_enabled = bool(previous_connection and previous_connection[0])

    save_connection(
        connection
    )

    logger.info(
        "Business connection updated: owner_id=%s enabled=%s",
        connection.user.id,
        connection.is_enabled
    )
    logger.debug("Business connection id: %s", connection.id)

    if connection.is_enabled and not was_enabled:
        try:
            await bot.send_message(
                chat_id=connection.user_chat_id,
                text=(
                    "✅ <b>Business-аккаунт успешно подключён!</b>\n\n"
                    "Теперь бот может архивировать переписку согласно вашим настройкам. "
                    "Управление доступно в меню бота."
                ),
                parse_mode="HTML"
            )
            logger.info(
                "Business connection success notice sent: owner_id=%s connection_id=%s",
                connection.user.id,
                connection.id
            )
        except Exception:
            logger.exception(
                "Could not send Business connection success notice: owner_id=%s connection_id=%s",
                connection.user.id,
                connection.id
            )


# =========================================================
# =========================================================
# AUTO-ARCHIVE REPLIES — возврат медиа автору ответа
# =========================================================

# Укажи свой Telegram ID через переменную окружения ADMIN_ID.
# Например на хостинге:
# ADMIN_ID=123456789
#
# Локально можно временно указать число прямо здесь.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))


def reply_archive_recipient_id(source_message):
    if getattr(getattr(source_message, "chat", None), "type", None) != "private":
        return None
    if not getattr(source_message, "business_connection_id", None):
        return None
    # Отправляем в личный диалог автора ответа с ботом, не через Business.
    replier_id = getattr(getattr(source_message, "from_user", None), "id", None)
    if not replier_id:
        logger.warning(
            "Cannot identify Business reply author; skip Bot DM: chat_id=%s message_id=%s",
            source_message.chat.id,
            getattr(source_message, "message_id", None)
        )
        return None

    connection = cursor.execute("""
        SELECT owner_id, user_chat_id
        FROM business_connections
        WHERE connection_id = ?
    """, (source_message.business_connection_id,)).fetchone()
    if connection and replier_id == connection[0]:
        # Для владельца используем выделенный Telegram ID чата бота из BusinessConnection.
        return connection[1]
    return replier_id


async def send_photo_to_chat(chat_id, photo_file_id, caption):
    """Пытается переслать фото по file_id, а self-destructing фото перезагружает."""

    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file_id,
            caption=caption,
            parse_mode="HTML"
        )
        return
    except TelegramBadRequest as error:
        if "SelfDestructingPhoto" not in str(error):
            raise

    logger.warning(
        "Telegram отклонил SelfDestructingPhoto по file_id; "
        "пробую скачать и загрузить заново"
    )
    telegram_file = await bot.get_file(photo_file_id)
    downloaded = await bot.download_file(telegram_file.file_path)

    if downloaded is None:
        raise RuntimeError("Telegram не вернул файл для повторной загрузки")

    if hasattr(downloaded, "getvalue"):
        photo_bytes = downloaded.getvalue()
    else:
        if hasattr(downloaded, "seek"):
            downloaded.seek(0)
        photo_bytes = downloaded.read()

    if not photo_bytes:
        raise RuntimeError("Скачанный SelfDestructingPhoto оказался пустым")

    await bot.send_photo(
        chat_id=chat_id,
        photo=BufferedInputFile(photo_bytes, filename="saved_photo.jpg"),
        caption=caption,
        parse_mode="HTML"
    )


async def send_replied_message_to_replier(
    source_message: types.Message,
    reply: types.Message
) -> bool:
    """Отправляет сохранённое медиа автору ответа в его личный чат с ботом."""

    if getattr(reply, "sticker", None):
        logger.debug("Sticker reply source skipped: chat_id=%s", source_message.chat.id)
        return False

    recipient_id = reply_archive_recipient_id(source_message)
    if not recipient_id:
        logger.error(
            "Reply author chat is unavailable: connection_id=%s",
            getattr(source_message, "business_connection_id", None)
        )
        return False

    connection_id = source_message.business_connection_id

    sender = getattr(reply, "from_user", None)
    if sender is None:
        origin = getattr(reply, "origin", None)
        sender = getattr(origin, "sender_user", None)

    sender_id = getattr(sender, "id", None) or "неизвестно"
    sender_name = (
        get_user_name(
            getattr(sender, "first_name", None),
            getattr(sender, "last_name", None),
            getattr(sender, "username", None)
        )[0]
        if sender
        else "Неизвестный пользователь"
    )
    reply_chat = getattr(reply, "chat", None)
    reply_chat_id = getattr(reply_chat, "id", None) or source_message.chat.id
    reply_message_id = getattr(reply, "message_id", None) or "неизвестно"

    header = (
        "💾 <b>СОХРАНЁННОЕ СООБЩЕНИЕ</b>\n\n"
        f"👤 <b>Отправитель:</b> {safe(sender_name)}\n"
        f"🆔 <b>User ID:</b> <code>{sender_id}</code>\n"
        f"💬 <b>Chat ID:</b> <code>{reply_chat_id}</code>\n"
        f"📨 <b>Message ID:</b> <code>{reply_message_id}</code>\n"
        f"🧾 <b>Источник:</b> <code>{safe(connection_id)}</code>\n"
    )

    text = getattr(reply, "text", None) or getattr(reply, "caption", None) or ""

    try:

        # -----------------------------------------------------
        # PHOTO
        # -----------------------------------------------------

        if reply.photo:

            await send_photo_to_chat(
                chat_id=recipient_id,
                photo_file_id=reply.photo[-1].file_id,
                caption=(
                    header +
                    (
                        f"\n💬 <b>Текст:</b>\n"
                        f"<blockquote>{safe(text or '—')}</blockquote>"
                    )
                )
            )

            return True

        # -----------------------------------------------------
        # VIDEO
        # -----------------------------------------------------

        if reply.video:

            await bot.send_video(
                chat_id=recipient_id,
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

            return True

        # -----------------------------------------------------
        # DOCUMENT
        # -----------------------------------------------------

        if reply.document:

            await bot.send_document(
                chat_id=recipient_id,
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

            return True

        # -----------------------------------------------------
        # AUDIO
        # -----------------------------------------------------

        if reply.audio:

            await bot.send_audio(
                chat_id=recipient_id,
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

            return True

        # -----------------------------------------------------
        # VOICE
        # -----------------------------------------------------

        if reply.voice:

            await bot.send_voice(
                chat_id=recipient_id,
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

            return True

        # -----------------------------------------------------
        # VIDEO NOTE
        # -----------------------------------------------------

        if reply.video_note:

            await bot.send_message(
                chat_id=recipient_id,
                text=header + "\n🎬 <b>Тип:</b> Кружок",
                parse_mode="HTML"
            )

            await bot.send_video_note(
                chat_id=recipient_id,
                video_note=reply.video_note.file_id
            )

            return True

        # -----------------------------------------------------
        # ANIMATION / GIF
        # -----------------------------------------------------

        if reply.animation:

            await bot.send_animation(
                chat_id=recipient_id,
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

            return True

        # -----------------------------------------------------
        # STICKER
        # -----------------------------------------------------

        if reply.sticker:

            await bot.send_message(
                chat_id=recipient_id,
                text=header + "\n🧩 <b>Тип:</b> Стикер",
                parse_mode="HTML"
            )

            await bot.send_sticker(
                chat_id=recipient_id,
                sticker=reply.sticker.file_id
            )

            return True

        # -----------------------------------------------------
        # TEXT
        # -----------------------------------------------------

        if text:

            await bot.send_message(
                chat_id=recipient_id,
                text=(
                    header +
                    "\n💬 <b>Текст:</b>\n"
                    f"<blockquote>{safe(text)}</blockquote>"
                ),
                parse_mode="HTML"
            )

            return True

        # -----------------------------------------------------
        # UNKNOWN
        # -----------------------------------------------------

        await bot.send_message(
            chat_id=recipient_id,
            text=(
                header +
                "\n⚠️ <b>Неизвестный тип сообщения.</b>"
            ),
            parse_mode="HTML"
        )
        return True

    except Exception as error:

        logger.exception(
            "Не удалось отправить архивное сообщение в личный чат бота: "
            "replier_id=%s. Возможно, пользователь ещё не нажал Start.",
            recipient_id
        )

        try:

            await bot.send_message(
                chat_id=recipient_id,
                text=(
                    "❌ <b>Ошибка пересылки сообщения</b>\n\n"
                    f"<code>{safe(error)}</code>"
                ),
                parse_mode="HTML"
            )

        except Exception:
            logger.exception("Не удалось отправить сообщение об ошибке в личный чат бота")

        return False


def archived_row_as_message(row, chat_id):
    """Преобразует запись архива в объект с полями Telegram Message."""

    (
        message_id,
        user_id,
        username,
        first_name,
        last_name,
        text,
        media_type,
        file_id
    ) = row

    sender = None
    if user_id is not None or username or first_name or last_name:
        sender = SimpleNamespace(
            id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name
        )

    media = SimpleNamespace(file_id=file_id) if file_id else None

    return SimpleNamespace(
        from_user=sender,
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        text=text if media_type == "text" else None,
        caption=text if media_type != "text" else None,
        photo=[media] if media_type == "photo" and media else None,
        video=media if media_type == "video" else None,
        voice=media if media_type == "voice" else None,
        video_note=media if media_type == "video_note" else None,
        document=media if media_type == "document" else None,
        audio=media if media_type == "audio" else None,
        animation=media if media_type == "animation" else None,
        sticker=media if media_type == "sticker" else None
    )


def find_archived_message_for_quote(source_message, quote):
    """Ищет в архиве сообщение, текст которого содержит цитату из ответа."""

    quote_text = getattr(quote, "text", None) or ""
    connection_id = (
        source_message.business_connection_id
        or (
            group_connection_id(source_message.chat.id)
            if getattr(source_message.chat, "type", None) in ("group", "supergroup")
            else None
        )
    )

    if not quote_text.strip() or not connection_id:
        return None, False

    cursor.execute("""
        SELECT
            message_id,
            user_id,
            username,
            first_name,
            last_name,
            text,
            media_type,
            file_id
        FROM messages
        WHERE connection_id = ?
          AND chat_id = ?
          AND date <= ?
          AND instr(COALESCE(text, ''), ?) > 0
        ORDER BY date DESC
        LIMIT 100
    """, (
        connection_id,
        source_message.chat.id,
        source_message.date.isoformat(),
        quote_text
    ))

    matches = cursor.fetchall()
    if len(matches) == 1:
        return matches[0], False

    # Если одинаковая цитата встречается в нескольких сообщениях, используем
    # позицию цитаты, когда она однозначно оставляет одну запись.
    quote_position = getattr(quote, "position", None)
    if quote_position is not None and len(matches) > 1:
        positioned_matches = []
        for row in matches:
            original_text = row[5] or ""
            char_position = original_text.find(quote_text)
            if char_position < 0:
                continue
            utf16_position = len(
                original_text[:char_position].encode("utf-16-le")
            ) // 2
            if utf16_position == quote_position:
                positioned_matches.append(row)

        if len(positioned_matches) == 1:
            return positioned_matches[0], False

    return None, len(matches) > 1


async def send_quote_to_replier(source_message, quote_text, ambiguous=False):
    """Отправляет автору ответа цитату в личный чат с ботом."""

    recipient_id = reply_archive_recipient_id(source_message)
    if not recipient_id:
        return

    reason = (
        "В архиве нашлось несколько сообщений с этой цитатой, поэтому "
        "отправляю только цитату, чтобы не выбрать не тот файл."
        if ambiguous
        else "Telegram не передал исходное сообщение; отправляю цитату, "
             "которую он приложил к ответу."
    )

    await bot.send_message(
        chat_id=recipient_id,
        text=(
            "💾 <b>ЦИТАТА ИЗ ОТВЕТА</b>\n\n"
            f"💬 <b>Chat ID:</b> <code>{source_message.chat.id}</code>\n"
            f"📨 <b>Message ID ответа:</b> "
            f"<code>{source_message.message_id}</code>\n\n"
            f"⚠️ {safe(reason)}\n\n"
            f"<blockquote>{safe(quote_text or 'Цитата без текста')}</blockquote>"
        ),
        parse_mode="HTML"
    )

@dp.callback_query(
    lambda c: c.data == "command_qr"
)
async def command_qr(
    callback: CallbackQuery
):

    user_id = callback.from_user.id

    qr_users.add(user_id)

    await replace_menu(
        callback,

        "🔳 <b>QR-код</b>\n\n"
        "Отправь мне ссылку или любой текст, "
        "и я превращу его в QR-код.\n\n"

        "Например:\n"
        "<code>https://google.com</code>\n\n"

        "📱 После отправки я сразу пришлю "
        "готовый QR-код.",

        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data="cancel_qr"
                    )
                ]
            ]
        )
    )

@dp.callback_query(
    lambda c: c.data == "cancel_qr"
)
async def cancel_qr(
    callback: CallbackQuery
):

    qr_users.discard(
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

    await callback.answer(
        "Создание QR отменено."
    )
async def forward_replied_message_to_replier(
    message: types.Message,
    owner_id: int
) -> bool:
    """Отправляет автору ответа медиа в его личный чат с ботом."""

    if not is_subscription_active(owner_id):
        await notify_subscription_required(owner_id)
        logger.info(
            "Business reply media forwarding skipped without subscription: "
            "owner_id=%s chat_id=%s message_id=%s",
            owner_id,
            message.chat.id,
            message.message_id
        )
        return False

    if not reply_archive_recipient_id(message):
        logger.error("Reply author Bot chat unavailable: business_chat_id=%s", message.chat.id)
        return False

    reply = message.reply_to_message or getattr(message, "external_reply", None)
    quote = getattr(message, "quote", None)
    quote_text = getattr(quote, "text", None) or ""

    # Обрабатываем только ответы на медиа. Если Telegram передал лишь цитату,
    # пробуем восстановить исходное медиа из архива по её тексту.
    if reply is not None and not is_media_message(reply):
        return False

    if reply is None:
        archived_row, ambiguous = find_archived_message_for_quote(
            source_message=message,
            quote=quote
        ) if quote else (None, False)

        if archived_row:
            reply = archived_row_as_message(
                archived_row,
                chat_id=message.chat.id
            )
        elif quote_text:
            await send_quote_to_replier(
                source_message=message,
                quote_text=quote_text,
                ambiguous=ambiguous
            )
            logger.info(
                "Reply quote sent to author in Bot DM: user_id=%s",
                getattr(message.from_user, "id", None) or message.chat.id
            )
            return True
        else:
            logger.warning(
                "Reply source unavailable: chat_id=%s message_id=%s",
                message.chat.id,
                message.message_id
            )
            return False

    if not is_media_message(reply):
        return False

    logger.debug(
        "Reply source selected: chat_id=%s message_id=%s type=%s",
        message.chat.id,
        getattr(reply, "message_id", None),
        type(reply).__name__
    )

    source_media_message_id = getattr(reply, "message_id", None)
    recipient_id = reply_archive_recipient_id(message)
    delivery_claimed = False
    reply_message_id = getattr(message, "message_id", None)
    if (
        source_media_message_id is not None
        and recipient_id is not None
        and reply_message_id is not None
    ):
        delivery_claimed = claim_archive_delivery(
            message.business_connection_id,
            message.chat.id,
            source_media_message_id,
            recipient_id,
            "reply",
            reply_message_id
        )
        if not delivery_claimed:
            already_delivered = await wait_for_archive_delivery(
                message.business_connection_id,
                message.chat.id,
                source_media_message_id,
                recipient_id,
                "reply",
                reply_message_id
            )
            if already_delivered is not False:
                logger.info(
                    "Duplicate archive delivery skipped: connection_id=%s chat_id=%s "
                    "source_message_id=%s recipient_id=%s",
                    message.business_connection_id,
                    message.chat.id,
                    source_media_message_id,
                    recipient_id
                )
                return True
            delivery_claimed = claim_archive_delivery(
                message.business_connection_id,
                message.chat.id,
                source_media_message_id,
                recipient_id,
                "reply",
                reply_message_id
            )
            if not delivery_claimed:
                return True

    sent = await send_replied_message_to_replier(
        source_message=message,
        reply=reply
    )
    if delivery_claimed:
        finish_archive_delivery(
            message.business_connection_id,
            message.chat.id,
            source_media_message_id,
            recipient_id,
            "reply",
            reply_message_id,
            sent
        )

    if sent:
        logger.info(
            "Reply source sent to author in Bot DM: user_id=%s source_message_id=%s",
            getattr(message.from_user, "id", None) or message.chat.id,
            getattr(reply, "message_id", None)
        )
    else:
        logger.warning("Reply source was not forwarded: chat_id=%s", message.chat.id)

    return sent


def is_media_message(message) -> bool:
    """True for reply media we return; stickers are intentionally ignored."""
    if not message:
        return False
    if getattr(message, "sticker", None):
        return False
    media_type, file_id = get_media_info(message)
    return media_type != "text" and bool(file_id)


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

        logger.warning('%s %s', "⚠️ Не удалось удалить .spam:", repr(error))

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

                logger.info("Spam message sent: message_id=%s", result.message_id)

                await asyncio.sleep(
                    SPAM_DELAY
                )

            except Exception as error:

                logger.error('%s %s', "❌ SPAM ERROR:", repr(error))

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

        logger.warning('%s %s', "⚠️ Не удалось удалить .bspam:", repr(error))

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

            logger.error('%s %s', "❌ BSPAM ERROR:", repr(error))


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

    audio_converter_users.discard(callback.from_user.id)
    converter_users.add(
        callback.from_user.id
    )
    logger.info("Video-to-circle mode enabled: user_id=%s", callback.from_user.id)

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
    audio_converter_users.discard(callback.from_user.id)

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

    converter_users.discard(callback.from_user.id)
    audio_converter_users.add(
        callback.from_user.id
    )
    logger.info("Video-to-audio mode enabled: user_id=%s", callback.from_user.id)

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
    converter_users.discard(callback.from_user.id)

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


@dp.message(
    lambda message: (
        message.chat.type == "private"
        and message.video is not None
        and message.from_user is not None
        and message.from_user.id in audio_converter_users
    )
)
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
            logger.error('%s %s', "❌ AUDIO FFMPEG ERROR:", error_text)
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
            logger.warning('%s %s', "⚠️ Не удалось удалить исходное видео:", repr(error))

        await show_main_menu(
            owner_id=user_id,
            chat_id=message.chat.id,
            first_name=(
                message.from_user.first_name
                or "друг"
            )
        )

    except Exception as error:

        logger.error('%s %s', "❌ AUDIO CONVERTER ERROR:", repr(error))

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
    lambda message: (
        message.chat.type == "private"
        and message.video is not None
        and message.from_user is not None
        and message.from_user.id in converter_users
    )
)
async def convert_video_handler(
    message: types.Message
):

    user_id = (
        message.from_user.id
    )

    if user_id not in converter_users:
        return

    logger.info(
        "Video received for circle conversion: user_id=%s message_id=%s duration=%s file_size=%s",
        user_id,
        message.message_id,
        getattr(message.video, "duration", None),
        getattr(message.video, "file_size", None)
    )

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

    actual_ffmpeg = (
        FFMPEG_PATH
        if os.path.isfile(FFMPEG_PATH)
        else shutil.which(FFMPEG_PATH)
    )

    if not actual_ffmpeg:

        await message.answer(
            "❌ <b>FFmpeg не найден.</b>\n\n"
            "Установи FFmpeg на сервер или укажи путь в переменной "
            "<code>FFMPEG_PATH</code>.\n\n"
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

        ffmpeg_command = [

            actual_ffmpeg,

            "-y",

            "-nostdin",

            "-hide_banner",

            "-loglevel",
            "error",

            "-i",
            input_file,

            "-map",
            "0:v:0",

            "-map",
            "0:a:0?",

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



        logger.info('🎬 FFMPEG')

        logger.debug('%s %s', "FFmpeg:", FFMPEG_PATH)
        logger.debug('%s %s', "Input:", input_file)
        logger.debug('%s %s', "Output:", output_file)


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

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=180
            )
        except asyncio.TimeoutError:
            process.kill()
            try:
                await process.communicate()
            except Exception:
                pass
            raise RuntimeError("FFmpeg слишком долго обрабатывал видео. Попробуй ролик покороче или с меньшим разрешением.")

        if process.returncode != 0:

            error_text = stderr.decode(
                errors="ignore"
            )

            logger.error("FFmpeg завершился с ошибкой: %s", error_text)

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

        logger.info('✅ FFMPEG OK')

        logger.debug('%s %s', "Output size:", output_size)

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

        logger.info('✅ Кружок отправлен')

        # -------------------------------------------------
        # DELETE ORIGINAL VIDEO
        # -------------------------------------------------

        try:

            await message.delete()

            logger.info('🗑 Исходное видео удалено')

        except Exception as error:

            logger.warning('%s %s', "⚠️ Не удалось удалить исходное видео:", repr(error))

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

        logger.warning('%s %s', "🌐 TELEGRAM NETWORK ERROR:", repr(error))

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


        logger.exception("Не удалось конвертировать видео")


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

                logger.warning('%s %s %s', "⚠️ Не удалось удалить:", filename, repr(error))



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
        logger.warning('%s %s', "⚠️ BACKUP PHOTO ERROR:", repr(error))

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
        logger.warning('%s %s', "⚠️ TARGET PHOTO ERROR:", repr(error))

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

        logger.debug(
            "Business rights: name=%s bio=%s photo=%s",
            can_name,
            can_bio,
            can_photo
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
                logger.warning('%s %s', "⚠️ REMOVE TARGET PHOTO ERROR:", repr(error))

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

        logger.debug(
            "Business rights: name=%s bio=%s photo=%s",
            can_name,
            can_bio,
            can_photo
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
            logger.warning('%s %s', "⚠️ REMOVE OLD PHOTO ERROR:", repr(error))

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

        logger.warning(
            '%s',
            "❌ .copy должен быть ответом "
                                        "на сообщение собеседника.",
        )

        return

    connection = await get_business_connection(
        message.business_connection_id
    )

    if not connection:

        logger.warning('❌ Business connection не найден.')

        return



    logger.info('👤 COPY PROFILE')
    logger.debug('%s %s', "Connection:", connection.id)
    logger.debug('%s %s', "Owner:", connection.user.id)
    logger.debug('%s %s', "Target:", reply.from_user.id)


    try:

        ok, info = await copy_business_profile(
            connection,
            reply.from_user.id
        )

        logger.info('%s', info)

    except Exception as error:

        logger.error('%s %s', "❌ COPY ERROR:", repr(error))


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

        logger.warning('❌ Business connection не найден.')

        return



    logger.info('↩️ BACK PROFILE')
    logger.debug('%s %s', "Connection:", connection.id)
    logger.debug('%s %s', "Owner:", connection.user.id)


    try:

        ok, info = await restore_business_profile(
            connection
        )

        logger.info('%s', info)

    except Exception as error:

        logger.error('%s %s', "❌ BACK ERROR:", repr(error))



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

    command_text = (message.text or "").strip().casefold()

    # Игры доступны владельцу в личном Business-чате, а также в обычном
    # личном чате с ботом. PvP создаётся именно в чате с соперником.
    if message.chat.type == "private" and user_id == owner_id:
        game_commands = {
            ".ttt": "ttt",
            ".chk": "chk",
            ".rps": "rps",
            ".c4": "c4"
        }
        if command_text in game_commands:
            await delete_game_command_message(message)
            await create_business_game_invite(
                message,
                game_commands[command_text]
            )
            return
        if command_text == ".flip":
            await delete_game_command_message(message)
            await send_coin_flip(
                chat_id=message.chat.id,
                business_connection_id=message.business_connection_id
            )
            return
        if command_text == ".dice":
            await delete_game_command_message(message)
            await bot.send_dice(
                chat_id=message.chat.id,
                business_connection_id=message.business_connection_id,
                emoji="🎲"
            )
            return

    # Управление мутом конкретного Business-диалога доступно только владельцу.
    if (
        message.chat.type == "private"
        and user_id == owner_id
        and command_text in {".mute", ".unmute"}
    ):
        command = command_text
        warning_key = (message.business_connection_id, message.chat.id)
        if command == ".mute":
            cursor.execute(
                "INSERT OR IGNORE INTO muted_business_chats "
                "(connection_id, chat_id, muted_at) VALUES (?, ?, ?)",
                (message.business_connection_id, message.chat.id, datetime.now(timezone.utc).isoformat())
            )
            db.commit()
            rights = getattr(connection, "rights", None)
            can_delete_all = bool(getattr(rights, "can_delete_all_messages", False))
            if can_delete_all:
                muted_delete_permission_warnings.discard(warning_key)
                response = "🔇 Мут включён: новые сообщения собеседника будут удаляться."
            else:
                muted_delete_permission_warnings.add(warning_key)
                response = (
                    "🔇 Мут включён, но бот пока не может удалять сообщения собеседника. "
                    "Открой Настройки → Изм. → Автоматизация чатов → "
                    "Message Archive Bot и включи право "
                    "«Удалять все сообщения», затем отправь .mute ещё раз."
                )
            logger.info(
                "Business chat muted: connection_id=%s chat_id=%s owner_id=%s",
                message.business_connection_id, message.chat.id, owner_id
            )
        else:
            cursor.execute(
                "DELETE FROM muted_business_chats WHERE connection_id = ? AND chat_id = ?",
                (message.business_connection_id, message.chat.id)
            )
            db.commit()
            muted_delete_permission_warnings.discard(warning_key)
            response = "🔊 Архивирование и уведомления для этого Business-чата возобновлены."
            logger.info(
                "Business chat unmuted: connection_id=%s chat_id=%s owner_id=%s",
                message.business_connection_id, message.chat.id, owner_id
            )
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=message.business_connection_id,
            text=response
        )
        return

    # При муте удаляем каждое новое входящее сообщение собеседника.
    if message.chat.type == "private" and user_id != owner_id:
        muted = cursor.execute(
            "SELECT 1 FROM muted_business_chats WHERE connection_id = ? AND chat_id = ?",
            (message.business_connection_id, message.chat.id)
        ).fetchone()
        if muted:
            warning_key = (message.business_connection_id, message.chat.id)
            rights = getattr(connection, "rights", None)
            if not bool(getattr(rights, "can_delete_all_messages", False)):
                if warning_key not in muted_delete_permission_warnings:
                    muted_delete_permission_warnings.add(warning_key)
                    logger.warning(
                        "Muted chat cannot delete incoming messages without can_delete_all_messages: "
                        "connection_id=%s chat_id=%s owner_id=%s",
                        message.business_connection_id, message.chat.id, owner_id
                    )
                    try:
                        await bot.send_message(
                            chat_id=connection.user_chat_id,
                            text=(
                                "⚠️ Мут включён, но новые сообщения собеседника не удаляются: "
                                "у бота нет права «Удалять все сообщения». Включи его: "
                                "Настройки → Изм. → Автоматизация чатов → Message Archive Bot."
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Could not notify Business owner about missing delete permission: "
                            "connection_id=%s chat_id=%s owner_id=%s",
                            message.business_connection_id, message.chat.id, owner_id
                        )
                return

            try:
                await bot.delete_business_messages(
                    business_connection_id=message.business_connection_id,
                    message_ids=[message.message_id]
                )
                logger.info(
                    "Muted incoming Business message deleted: connection_id=%s chat_id=%s message_id=%s",
                    message.business_connection_id, message.chat.id, message.message_id
                )
                muted_delete_permission_warnings.discard(warning_key)
            except Exception as error:
                logger.warning(
                    "Failed to delete muted incoming Business message: "
                    "connection_id=%s chat_id=%s message_id=%s error=%s",
                    message.business_connection_id, message.chat.id, message.message_id, error
                )
                if warning_key not in muted_delete_permission_warnings:
                    muted_delete_permission_warnings.add(warning_key)
                    try:
                        await bot.send_message(
                            chat_id=connection.user_chat_id,
                            text=(
                                "⚠️ Не получилось удалить сообщение собеседника в замьюченном чате. "
                                "Проверь право «Удалять все сообщения»: Настройки → Изм. → "
                                "Автоматизация чатов → Message Archive Bot."
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Could not notify Business owner about muted-message delete failure: "
                            "connection_id=%s chat_id=%s owner_id=%s",
                            message.business_connection_id, message.chat.id, owner_id
                        )
            return

    if not is_subscription_active(owner_id):
        await notify_subscription_required(owner_id)
        logger.debug("Business archive skipped without subscription: owner_id=%s", owner_id)
        return

    media_type, file_id = get_media_info(
        message
    )

    if (
        not save_media
        and media_type != "text"
    ):

        media_type = "text"
        file_id = None

    chat_username = getattr(message.chat, "username", None)
    chat_first_name = getattr(message.chat, "first_name", None)
    chat_last_name = getattr(message.chat, "last_name", None)

    cursor.execute("""
        INSERT OR REPLACE INTO messages (
            connection_id, chat_id, message_id,
            user_id, username, first_name, last_name,
            chat_username, chat_first_name, chat_last_name,
            text, date, media_type, file_id, deleted
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        message.business_connection_id,
        message.chat.id,
        message.message_id,
        user_id,
        username,
        first_name,
        last_name,
        chat_username,
        chat_first_name,
        chat_last_name,
        text,
        message.date.isoformat(),
        media_type,
        file_id
    ))

    db.commit()

    logger.debug(
        "Business message archived: chat_id=%s message_id=%s "
        "media_type=%s user_id=%s",
        message.chat.id,
        message.message_id,
        media_type,
        user_id or "unknown"
    )

    if message.chat.type == "private" and (
        message.reply_to_message
        or getattr(message, "external_reply", None)
        or getattr(message, "quote", None)
    ):
        try:
            await forward_replied_message_to_replier(message, owner_id)
        except Exception:
            logger.exception(
                "Business reply media forwarding failed: chat_id=%s message_id=%s",
                message.chat.id,
                message.message_id
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

    owner_id = connection.user.id
    if not is_subscription_active(owner_id):
        await notify_subscription_required(owner_id)
        logger.info(
            "Edited Business message skipped without subscription: "
            "owner_id=%s chat_id=%s message_id=%s",
            owner_id,
            message.chat.id,
            message.message_id
        )
        return

    save_connection(
        connection,
        last_chat_id=message.chat.id
    )

    user_chat_id = connection.user_chat_id

    if cursor.execute(
        "SELECT 1 FROM muted_business_chats WHERE connection_id = ? AND chat_id = ?",
        (message.business_connection_id, message.chat.id)
    ).fetchone():
        return

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

    chat_username = getattr(message.chat, "username", None)
    chat_first_name = getattr(message.chat, "first_name", None)
    chat_last_name = getattr(message.chat, "last_name", None)

    cursor.execute("""
        INSERT OR REPLACE INTO messages (
            connection_id, chat_id, message_id,
            user_id, username, first_name, last_name,
            chat_username, chat_first_name, chat_last_name,
            text, date, media_type, file_id, deleted
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        message.business_connection_id,
        message.chat.id,
        message.message_id,
        user_id,
        username,
        first_name,
        last_name,
        chat_username,
        chat_first_name,
        chat_last_name,
        new_text,
        message.date.isoformat(),
        media_type,
        file_id
    ))

    db.commit()

    if user_id == owner_id:
        logger.debug(
            "Owner's edited Business message archived without notification: "
            "connection_id=%s chat_id=%s message_id=%s",
            message.business_connection_id,
            message.chat.id,
            message.message_id
        )
        return

    if not edit_notifications:
        return

    try:

        await bot.send_message(
            chat_id=user_chat_id,
            text=notification,
            parse_mode="HTML"
        )

    except TelegramNetworkError as error:

        logger.warning('%s %s', "🌐 EDIT NETWORK ERROR:", repr(error))

    except Exception as error:

        logger.error('%s %s', "❌ EDIT ERROR:", repr(error))


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

    if not is_subscription_active(owner_id):
        await notify_subscription_required(owner_id)
        return

    if cursor.execute(
        "SELECT 1 FROM muted_business_chats WHERE connection_id = ? AND chat_id = ?",
        (event.business_connection_id, event.chat.id)
    ).fetchone():
        return

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

        # Помечаем сообщение как удалённое
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

        if (text or "").strip().casefold() in {
            ".flip", ".dice", ".ttt", ".chk", ".rps", ".c4"
        }:
            logger.debug(
                "Game command deletion notice suppressed: connection_id=%s chat_id=%s message_id=%s",
                event.business_connection_id,
                event.chat.id,
                message_id
            )
            continue

        if not is_subscription_active(owner_id):
            await notify_subscription_required(owner_id)
            return

        if user_id == owner_id:
            logger.debug(
                "Owner's deleted Business message archived without notification: "
                "connection_id=%s chat_id=%s message_id=%s",
                event.business_connection_id,
                event.chat.id,
                message_id
            )
            continue

        if not claim_archive_delivery(
            event.business_connection_id,
            event.chat.id,
            message_id,
            user_chat_id,
            "deleted",
            message_id
        ):
            already_delivered = await wait_for_archive_delivery(
                event.business_connection_id,
                event.chat.id,
                message_id,
                user_chat_id,
                "deleted",
                message_id
            )
            if already_delivered is not False:
                logger.info(
                    "Duplicate deletion delivery skipped: connection_id=%s chat_id=%s "
                    "message_id=%s recipient_id=%s",
                    event.business_connection_id,
                    event.chat.id,
                    message_id,
                    user_chat_id
                )
                continue
            if not claim_archive_delivery(
                event.business_connection_id,
                event.chat.id,
                message_id,
                user_chat_id,
                "deleted",
                message_id
            ):
                continue

        name, username_text = get_user_name(
            first_name,
            last_name,
            username
        )

        # Определяем тип сообщения
        media_names = {
            "text": "💬 Текст",
            "photo": "🖼 Фото",
            "video": "🎥 Видео",
            "video_note": "⭕️ Кружок",
            "voice": "🎤 Голосовое",
            "audio": "🎵 Аудио",
            "document": "📎 Документ",
            "animation": "🎞 GIF",
            "sticker": "🎨 Стикер",
        }

        media_name = media_names.get(
            media_type,
            "📦 Медиа"
        )

        header = (
            "🗑 <b>Удалено сообщение</b>\n\n"

            f"👤 <b>{safe(name)}</b>\n"
            f"🔹 {safe(username_text)}\n"
            f"🆔 <code>{user_id}</code>\n"
            f"🕐 {safe(moscow_time(datetime.fromisoformat(date)))}\n"
            f"📦 {media_name}"
        )

        # ─────────────────────────────
        # ТЕКСТ
        # ─────────────────────────────

        if media_type == "text" or not file_id:

            delivered = False
            try:
                await bot.send_message(
                    chat_id=user_chat_id,
                    text=(
                        header +
                        "\n\n"
                        "💬 <b>Сообщение:</b>\n"
                        "<blockquote>"
                        f"{safe(text or '—')}"
                        "</blockquote>"
                    ),
                    parse_mode="HTML"
                )
                delivered = True

            except Exception as error:
                logger.error('%s %s', "❌ DELETE NOTIFY ERROR:", repr(error))
            finally:
                finish_archive_delivery(
                    event.business_connection_id,
                    event.chat.id,
                    message_id,
                    user_chat_id,
                    "deleted",
                    message_id,
                    delivered
                )

            continue

        # ─────────────────────────────
        # Обычные медиа с caption
        # ─────────────────────────────

        caption = (
            header +
            "\n\n"
            "💬 <b>Текст:</b>\n"
            f"{safe(text or '—')}"
        )

        delivered = False
        try:

            if media_type == "photo":

                await bot.send_photo(
                    chat_id=user_chat_id,
                    photo=file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "video":

                await bot.send_video(
                    chat_id=user_chat_id,
                    video=file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "voice":

                await bot.send_voice(
                    chat_id=user_chat_id,
                    voice=file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "document":

                await bot.send_document(
                    chat_id=user_chat_id,
                    document=file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "audio":

                await bot.send_audio(
                    chat_id=user_chat_id,
                    audio=file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            elif media_type == "animation":

                await bot.send_animation(
                    chat_id=user_chat_id,
                    animation=file_id,
                    caption=caption,
                    parse_mode="HTML"
                )

            # ─────────────────────────
            # КРУЖОК
            # ─────────────────────────

            elif media_type == "video_note":

                # Сначала информация об удалении
                await bot.send_message(
                    chat_id=user_chat_id,
                    text=header,
                    parse_mode="HTML"
                )

                # Потом сам кружок
                await bot.send_video_note(
                    chat_id=user_chat_id,
                    video_note=file_id
                )

                # Если был текст к сообщению
                if text:
                    await bot.send_message(
                        chat_id=user_chat_id,
                        text=(
                            "💬 <b>Текст:</b>\n"
                            f"{safe(text)}"
                        ),
                        parse_mode="HTML"
                    )

            # ─────────────────────────
            # СТИКЕР
            # ─────────────────────────

            elif media_type == "sticker":

                # Сначала информация об удалении
                await bot.send_message(
                    chat_id=user_chat_id,
                    text=header,
                    parse_mode="HTML"
                )

                # Потом сам стикер
                await bot.send_sticker(
                    chat_id=user_chat_id,
                    sticker=file_id
                )

                # Если был текст
                if text:
                    await bot.send_message(
                        chat_id=user_chat_id,
                        text=(
                            "💬 <b>Текст:</b>\n"
                            f"{safe(text)}"
                        ),
                        parse_mode="HTML"
                    )

            delivered = True

        except Exception as error:

            logger.error('%s %s', "❌ MEDIA DELETE ERROR:", repr(error))
        finally:
            finish_archive_delivery(
                event.business_connection_id,
                event.chat.id,
                message_id,
                user_chat_id,
                "deleted",
                message_id,
                delivered
            )
# =========================================================
# GROUP CHAT SUPPORT
# =========================================================

@dp.my_chat_member()
async def bot_group_membership_handler(
    event: types.ChatMemberUpdated
):
    if event.chat.type not in ("group", "supergroup"):
        return

    status = event.new_chat_member.status
    is_active = status in ("member", "administrator")
    save_group_chat(
        event.chat,
        added_by_user_id=getattr(event.from_user, "id", None),
        enabled=is_active
    )

    old_status = event.old_chat_member.status
    if is_active and old_status not in ("member", "administrator"):
        try:
            me = await bot.get_me()
            group_command = f"/group@{me.username}" if me.username else "/group"
            welcome_keyboard = None
            added_by = getattr(event.from_user, "id", None)
            has_access = is_subscription_active(added_by)
            if has_access:
                welcome_text = (
                    "📚 <b>Архив-бот подключён к группе.</b>\n\n"
                    "Архивирование активно: бот сохраняет сообщения и доступные медиа, которые получает.\n"
                    f"Напиши {group_command} для справки."
                )
            else:
                welcome_text = (
                    "📚 <b>Архив-бот добавлен в группу.</b>\n\n"
                    "Для включения архивирования пользователь, который добавил бота, "
                    "должен оформить доступ на 30 дней в личном чате.\n"
                    f"Напиши {group_command} для справки."
                )
            if not has_access and me.username:
                welcome_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="⭐ Оформить доступ",
                        url=f"https://t.me/{me.username}?start=subscription"
                    )
                ]])
            await bot.send_message(
                chat_id=event.chat.id,
                text=welcome_text,
                parse_mode="HTML",
                reply_markup=welcome_keyboard
            )
        except Exception:
            logger.exception(
                "Could not send group welcome message: chat_id=%s",
                event.chat.id
            )

    logger.info(
        "Bot group membership changed: chat_id=%s title=%r status=%s",
        event.chat.id,
        getattr(event.chat, "title", None),
        status
    )


@dp.message(Command("group"))
async def group_help_command(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer(
            "Добавь бота в группу, затем отправь там /group."
        )
        return

    sender_id = getattr(message.from_user, "id", None)
    added_by_user_id = sender_id if await is_group_admin(message.chat.id, sender_id) else None
    save_group_chat(message.chat, added_by_user_id=added_by_user_id)
    license_owner = group_subscription_owner(message.chat.id)
    archive_access = is_subscription_active(license_owner)
    if not archive_access:
        await notify_subscription_required(license_owner)
    archive_status = (
        "• сохраняет сообщения и медиа, которые Telegram ему передаёт;\n"
        if archive_access else
        "🔒 Архивирование и команды рассылки приостановлены до оформления подписки. "
        "Пользователь, добавивший бота, должен открыть бота в личке и отправить /subscription.\n"
    )
    group_help_text = (
        "👥 <b>Бот работает в группе</b>\n\n"
        + archive_status
        + "• автопересылка медиа по ответу работает только в личных Business-чатах;\n"
        + "• команды: <code>/spam N текст</code>, <code>/bspam</code>, "
        + "<code>/stopspam</code>; игровые и личные команды в группе не работают;\n"
        + "• администратор бота может открыть архив в личном чате командой "
        + "<code>/logs</code>.\n\n"
        + "Чтобы архивировать все групповые сообщения, пользователь, добавивший бота, "
        + "должен оформить подписку через /subscription; боту также нужны права "
        + "администратора либо отключённый Privacy Mode у @BotFather.\n"
        + "Удаления обычных групповых сообщений Telegram боту не присылает."
    )
    await message.answer(group_help_text, parse_mode="HTML")


@dp.edited_message(F.chat.type.in_({"group", "supergroup"}))
async def edited_group_message_handler(message: types.Message):
    save_group_chat(message.chat)
    license_owner = group_subscription_owner(message.chat.id)
    if not is_subscription_active(license_owner):
        await notify_subscription_required(license_owner)
        return
    connection_id = group_connection_id(message.chat.id)
    chat_id = message.chat.id
    old_row = cursor.execute("""
        SELECT text FROM messages
        WHERE connection_id = ? AND chat_id = ? AND message_id = ?
    """, (connection_id, chat_id, message.message_id)).fetchone()
    old_text = old_row[0] if old_row else ""

    user = message.from_user
    sender_chat = getattr(message, "sender_chat", None)
    user_id = user.id if user else None
    username = user.username if user else getattr(sender_chat, "username", None)
    first_name = user.first_name if user else getattr(sender_chat, "title", None)
    last_name = user.last_name if user else None
    new_text = message.text or message.caption or ""
    media_type, file_id = get_media_info(message)
    _, save_media, edit_notifications = get_settings(chat_id)
    if not save_media and media_type != "text":
        media_type, file_id = "text", None

    cursor.execute("""
        INSERT OR REPLACE INTO messages (
            connection_id, chat_id, message_id,
            user_id, username, first_name, last_name,
            chat_username, chat_first_name, chat_last_name,
            text, date, media_type, file_id, deleted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        connection_id, chat_id, message.message_id,
        user_id, username, first_name, last_name,
        getattr(message.chat, "username", None), None, None,
        new_text, message.date.isoformat(), media_type, file_id
    ))
    db.commit()

    if edit_notifications and old_text != new_text:
        sender = _display_person_name(first_name, last_name, username, user_id)
        old_html, new_html = make_diff_html(old_text, new_text)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✏️ <b>{safe(sender)}</b> изменил(а) сообщение.\n\n"
                    f"<b>Было:</b> <blockquote>{old_html}</blockquote>\n"
                    f"<b>Стало:</b> <blockquote>{new_html}</blockquote>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            logger.exception(
                "Could not post group edit notice: chat_id=%s message_id=%s",
                chat_id,
                message.message_id
            )


async def handle_group_spam_command(message: types.Message, command, args):
    if command not in (".spam", "/spam", ".bspam", "/bspam", ".stopspam", "/stopspam"):
        return False

    chat_id = message.chat.id
    if not await is_group_admin(
        chat_id,
        getattr(message.from_user, "id", None)
    ):
        await message.reply("⛔ Команда доступна только администраторам группы.")
        return True

    license_owner = group_subscription_owner(chat_id)
    if not is_subscription_active(license_owner):
        await message.reply(
            "🔒 Для команд бота в группе нужна активная подписка пользователя, "
            "который добавил бота. Откройте бота в личке и отправьте /subscription."
        )
        await notify_subscription_required(license_owner)
        return True

    connection_id = group_connection_id(chat_id)

    if command in (".spam", "/spam"):
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply(
                "Формат: <code>/spam N текст</code>\n"
                "Например: <code>/spam 3 Привет</code>",
                parse_mode="HTML"
            )
            return True
        try:
            count = int(parts[0])
        except ValueError:
            await message.reply("N должно быть числом.")
            return True
        text = parts[1].strip()
        if not text or count < 1 or count > MAX_SPAM_COUNT:
            await message.reply(f"Укажи от 1 до {MAX_SPAM_COUNT} сообщений и текст.")
            return True
        if chat_id in active_spam_tasks:
            await message.reply("В этой группе уже выполняется рассылка.")
            return True

        try:
            await message.delete()
        except Exception:
            pass

        stop_event = asyncio.Event()
        active_spam_tasks[chat_id] = stop_event
        sent_count = 0
        try:
            for _ in range(count):
                if stop_event.is_set():
                    break
                sent = await bot.send_message(chat_id=chat_id, text=text)
                cursor.execute("""
                    INSERT INTO spam_messages (
                        owner_id, connection_id, chat_id, message_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                """, (
                    chat_id, connection_id, chat_id, sent.message_id,
                    datetime.now(timezone.utc).isoformat()
                ))
                db.commit()
                sent_count += 1
                await asyncio.sleep(3.1)
        except Exception:
            logger.exception("Group spam command failed: chat_id=%s", chat_id)
            await bot.send_message(chat_id, "❌ Не удалось продолжить отправку.")
        finally:
            active_spam_tasks.pop(chat_id, None)
        logger.info("Group spam finished: chat_id=%s sent=%s", chat_id, sent_count)
        return True

    if command in (".bspam", "/bspam"):
        rows = cursor.execute("""
            SELECT message_id FROM spam_messages
            WHERE owner_id = ? AND connection_id = ? AND chat_id = ?
            ORDER BY id ASC
        """, (chat_id, connection_id, chat_id)).fetchall()
        removed = 0
        for (message_id,) in rows:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
                cursor.execute("""
                    DELETE FROM spam_messages
                    WHERE owner_id = ? AND connection_id = ?
                      AND chat_id = ? AND message_id = ?
                """, (chat_id, connection_id, chat_id, message_id))
                removed += 1
            except Exception as error:
                logger.debug(
                    "Could not remove group spam message: chat_id=%s message_id=%s error=%s",
                    chat_id, message_id, error
                )
        db.commit()
        try:
            await message.delete()
        except Exception:
            pass
        await bot.send_message(chat_id, f"🗑 Удалено сообщений: {removed}.")
        return True

    if command in (".stopspam", "/stopspam"):
        event = active_spam_tasks.get(chat_id)
        if event is None:
            await message.reply("Сейчас в группе ничего не отправляется.")
        else:
            event.set()
            await message.reply("⏹ Останавливаю отправку…")
        return True

    return False


# =========================================================
# PRIVATE GAMES AND SHORTCUTS
# =========================================================


async def delete_game_command_message(message) -> bool:
    try:
        connection_id = getattr(message, "business_connection_id", None)
        if connection_id:
            await bot.delete_business_messages(
                business_connection_id=connection_id,
                message_ids=[message.message_id]
            )
        else:
            await message.delete()
        return True
    except Exception as error:
        logger.warning(
            "Could not remove game command: chat_id=%s message_id=%s error=%s",
            getattr(getattr(message, "chat", None), "id", None),
            getattr(message, "message_id", None),
            error
        )
        return False


def create_coin_flip_gif(result: str) -> bytes:
    width, height = 240, 180
    frame_count = 24
    frames = []

    def draw_star(draw, center_x, center_y, radius, color):
        points = []
        for index in range(10):
            angle = -math.pi / 2 + index * math.pi / 5
            point_radius = radius if index % 2 == 0 else radius * 0.43
            points.append((
                center_x + math.cos(angle) * point_radius,
                center_y + math.sin(angle) * point_radius
            ))
        draw.polygon(points, fill=color)

    for index in range(frame_count):
        progress = index / (frame_count - 1)
        angle = 10 * math.pi * progress + 3 * math.pi * progress * (1 - progress)
        final_pose = progress >= 0.88
        coin_width = 116 if final_pose else max(8, int(116 * abs(math.cos(angle))))
        face = result if final_pose else ("Орёл" if math.cos(angle) >= 0 else "Решка")

        frame = Image.new("RGB", (width, height), "#111a27")
        draw = ImageDraw.Draw(frame)
        draw.ellipse((55, 145, 185, 163), fill="#080d14")

        left = (width - coin_width) // 2
        right = left + coin_width
        top, bottom = 25, 145
        draw.ellipse((left, top, right, bottom), fill="#9c6418", outline="#f9d76c", width=4)

        if coin_width > 26:
            inset = max(3, min(10, coin_width // 10))
            draw.ellipse(
                (left + inset, top + 8, right - inset, bottom - 8),
                fill="#e7ae35",
                outline="#fff0ad",
                width=2
            )
            center_x = width // 2
            center_y = (top + bottom) // 2
            dark_gold = "#8a5713"
            if face == "Орёл":
                draw.ellipse((center_x - 20, center_y - 30, center_x + 13, center_y + 9), fill=dark_gold)
                draw.polygon(
                    [(center_x + 5, center_y - 7), (center_x + 25, center_y + 1), (center_x + 7, center_y + 8)],
                    fill=dark_gold
                )
                draw.pieslice(
                    (center_x - 34, center_y + 2, center_x + 30, center_y + 46),
                    180, 360, fill=dark_gold
                )
            else:
                draw.ellipse(
                    (center_x - 27, center_y - 27, center_x + 27, center_y + 27),
                    outline=dark_gold,
                    width=3
                )
                draw_star(draw, center_x, center_y, 23, dark_gold)

        frames.append(frame)

    output = BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[65] * (frame_count - 1) + [1100],
        disposal=2,
        optimize=True
    )
    return output.getvalue()


async def send_coin_flip(chat_id: int, business_connection_id=None):
    result = random.choice(("Орёл", "Решка"))
    animation = BufferedInputFile(
        create_coin_flip_gif(result),
        filename="coin-flip.gif"
    )
    try:
        await bot.send_animation(
            chat_id=chat_id,
            business_connection_id=business_connection_id,
            animation=animation,
            caption=f"🪙 {result}!"
        )
    except Exception:
        logger.exception("Coin animation send failed: chat_id=%s", chat_id)
        await bot.send_message(
            chat_id=chat_id,
            business_connection_id=business_connection_id,
            text=f"🪙 {result}!"
        )


def ttt_winner(board):
    for a, b, c in ((0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)):
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


@dp.message(
    lambda message: (
        message.chat.type == "private"
        and isinstance(message.text, str)
        and message.text.strip().casefold() == ".flip"
    )
)
async def flip_command(message: types.Message):
    await delete_game_command_message(message)
    await send_coin_flip(chat_id=message.chat.id)


@dp.message(
    lambda message: (
        message.chat.type == "private"
        and isinstance(message.text, str)
        and message.text.strip().casefold() == ".dice"
    )
)
async def dice_command(message: types.Message):
    await delete_game_command_message(message)
    await bot.send_dice(chat_id=message.chat.id, emoji="🎲")


@dp.message(
    lambda message: (
        message.chat.type == "private"
        and isinstance(message.text, str)
        and message.text.strip().casefold() in {".ttt", ".chk", ".rps", ".c4"}
    )
)
async def business_game_help(message: types.Message):
    await delete_game_command_message(message)


def _checkers_moves(board, side):
    captures, regular = [], []
    for r in range(8):
        for c in range(8):
            piece = board[r][c]
            if not piece or piece.lower() != side:
                continue
            dirs = [(-1,-1),(-1,1),(1,-1),(1,1)]
            for dr, dc in dirs:
                nr, nc = r + dr, c + dc
                if 0 <= nr < 8 and 0 <= nc < 8:
                    if board[nr][nc] and board[nr][nc].lower() != side:
                        tr, tc = r + 2*dr, c + 2*dc
                        if 0 <= tr < 8 and 0 <= tc < 8 and board[tr][tc] is None:
                            captures.append((r,c,tr,tc))
            if piece.isupper():
                move_dirs = dirs
            else:
                move_dirs = [(-1, -1), (-1, 1)] if side == "w" else [(1, -1), (1, 1)]
            for dr, dc in move_dirs:
                nr, nc = r + dr, c + dc
                if 0 <= nr < 8 and 0 <= nc < 8 and board[nr][nc] is None:
                    regular.append((r,c,nr,nc))
    return captures or regular


def _checkers_apply(board, move):
    r, c, nr, nc = move
    piece = board[r][c]
    board[r][c] = None
    if abs(nr-r) == 2:
        board[(r+nr)//2][(c+nc)//2] = None
    if piece == "w" and nr == 0:
        piece = "W"
    elif piece == "b" and nr == 7:
        piece = "B"
    board[nr][nc] = piece


def _checkers_over(board, turn):
    white = any(cell and cell.lower() == "w" for row in board for cell in row)
    black = any(cell and cell.lower() == "b" for row in board for cell in row)
    if not white:
        return "Чёрные победили!", True
    if not black:
        return "Белые победили!", True
    if not _checkers_moves(board, turn):
        winner = "Чёрные" if turn == "w" else "Белые"
        return f"У {('белых' if turn == 'w' else 'чёрных')} нет ходов. {winner} победили!", True
    return "", False


# =========================================================
# PLAYER-TO-PLAYER GAMES
# =========================================================

async def create_game_invite(message: types.Message, game_type: str):
    me = await bot.get_me()
    if not me.username:
        await message.answer("У бота нет username — ссылку приглашения создать нельзя.")
        return
    invite_id = uuid.uuid4().hex[:16]
    game_invites[invite_id] = {
        "type": game_type,
        "creator_id": message.from_user.id,
        "creator_label": game_user_label(message.from_user, message.from_user.id),
        "created_at": datetime.now(timezone.utc).timestamp()
    }
    logger.info(
        "PvP game invite created: invite_id=%s game_type=%s creator_id=%s",
        invite_id, game_type, message.from_user.id
    )
    title = "крестики-нолики" if game_type == "ttt" else "шашки"
    invite_url = f"https://t.me/{me.username}?start=game_{invite_id}"
    await message.answer(
        f"🎮 Приглашение в игру «{title}» готово. Отправь ссылку сопернику.\n"
        "Когда он откроет её и нажмёт Start, игра появится у вас обоих в личных чатах.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📨 Пригласить соперника", url=invite_url)
        ]])
    )


def game_user_label(user, fallback_id=None):
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    display_name = " ".join(part for part in (first_name, last_name) if part)
    return display_name or f"ID {fallback_id or 'неизвестен'}"


def update_pvp_player_label(game, user):
    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    player_ids = set(game.get("players", {}).values())
    player_ids.update((game.get("creator_id"), game.get("opponent_id")))
    if user_id in player_ids:
        game.setdefault("player_labels", {})[user_id] = game_user_label(user, user_id)


async def create_business_game_invite(message: types.Message, game_type: str):
    connection_id = getattr(message, "business_connection_id", None)
    creator_id = getattr(getattr(message, "from_user", None), "id", None)
    opponent_id = getattr(getattr(message, "chat", None), "id", None)
    if not connection_id or not creator_id or not opponent_id or message.chat.type != "private":
        logger.warning("Cannot create Business PvP invite: missing chat or connection data")
        return
    if creator_id == opponent_id:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=connection_id,
            text="Не удалось определить собеседника для игры. Отправь команду в личном Business-чате с ним."
        )
        return

    invite_id = uuid.uuid4().hex[:16]
    invite = {
        "type": game_type,
        "creator_id": creator_id,
        "opponent_id": opponent_id,
        "connection_id": connection_id,
        "chat_id": message.chat.id,
        "location": "business",
        "creator_label": game_user_label(message.from_user, creator_id),
        "opponent_label": game_user_label(message.chat, opponent_id),
        "created_at": datetime.now(timezone.utc).timestamp()
    }
    game_invites[invite_id] = invite
    title = {
        "ttt": "крестики-нолики",
        "chk": "шашки",
        "rps": "камень, ножницы, бумага",
        "c4": "четыре в ряд"
    }[game_type]
    try:
        sent = await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=connection_id,
            text=(
                f"🎮 {invite['creator_label']} вызывает тебя на игру «{title}»! "
                "Нажми кнопку, чтобы принять вызов."
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ Принять вызов",
                    callback_data=f"pvp_accept:{invite_id}"
                )
            ]])
        )
        invite["message_id"] = sent.message_id
        logger.info(
            "Business PvP invite sent: invite_id=%s game_type=%s creator_id=%s opponent_id=%s",
            invite_id, game_type, creator_id, opponent_id
        )
    except Exception:
        game_invites.pop(invite_id, None)
        logger.exception(
            "Could not send Business PvP invite: game_type=%s creator_id=%s chat_id=%s",
            game_type, creator_id, message.chat.id
        )
        try:
            await bot.send_message(
                chat_id=creator_id,
                text="Не получилось отправить приглашение в эту переписку. Проверь права бота на отправку Business-сообщений."
            )
        except Exception:
            logger.exception("Could not notify game creator about failed Business invite: creator_id=%s", creator_id)


def new_pvp_game_state(game_type: str, creator_id: int, opponent_id: int, **extra):
    if game_type == "ttt":
        game = {
            "type": "ttt", "players": {"X": creator_id, "O": opponent_id},
            "board": [None] * 9, "turn": "X", "finished": False,
            "status": "", "message_ids": {}
        }
    elif game_type == "chk":
        game = {
            "type": "chk", "players": {"w": creator_id, "b": opponent_id},
            "board": _pvp_checkers_board(), "turn": "w", "finished": False,
            "status": "", "selected": None, "selected_by": None, "message_ids": {}
        }
    elif game_type == "rps":
        game = {
            "type": "rps", "players": {"creator": creator_id, "opponent": opponent_id},
            "choices": {}, "finished": False, "status": "", "message_ids": {}
        }
    else:
        game = {
            "type": "c4", "players": {"R": creator_id, "Y": opponent_id},
            "board": [[None for _ in range(7)] for _ in range(6)],
            "turn": "R", "finished": False, "status": "", "message_ids": {}
        }
    game.update(extra)
    return game


def _pvp_checkers_board():
    board = [[None for _ in range(8)] for _ in range(8)]
    for r in range(3):
        for c in range(8):
            if (r + c) % 2:
                board[r][c] = "b"
    for r in range(5, 8):
        for c in range(8):
            if (r + c) % 2:
                board[r][c] = "w"
    return board


def _connect4_has_winner(board, row, column, token):
    for row_step, column_step in ((0, 1), (1, 0), (1, 1), (1, -1)):
        count = 1
        for direction in (-1, 1):
            r, c = row, column
            while True:
                r += row_step * direction
                c += column_step * direction
                if not (0 <= r < 6 and 0 <= c < 7) or board[r][c] != token:
                    break
                count += 1
        if count >= 4:
            return True
    return False


def _pvp_game_text(game, user_id):
    if game["type"] == "ttt":
        mark = "X" if game["players"]["X"] == user_id else "O"
        current_player = game["players"][game["turn"]]
        status = game["status"] if game["finished"] else (
            f"Ход: {_pvp_player_label(game, current_player)}."
        )
        return f"⭕ Крестики-нолики PvP\nТы играешь за {mark}. {status}"
    side = "w" if game["players"]["w"] == user_id else "b"
    current_player = game["players"][game["turn"]]
    status = game["status"] if game["finished"] else (
        f"Ход: {_pvp_player_label(game, current_player)}."
    )
    return f"♟ Шашки PvP\nТы играешь {'белыми' if side == 'w' else 'чёрными'}. {status}"


def _pvp_player_label(game, user_id):
    return game.get("player_labels", {}).get(user_id, f"ID {user_id}")


def _pvp_turn_notice(game):
    current_player = game["players"][game["turn"]]
    return f"Сейчас ход: {_pvp_player_label(game, current_player)}."


def _pvp_business_game_text(game):
    if game["type"] == "ttt":
        current_player = game["players"][game["turn"]]
        status = game["status"] if game["finished"] else f"Ход: {_pvp_player_label(game, current_player)}."
        return f"⭕ Крестики-нолики — игра один на один\n{status}"

    if game["type"] == "chk":
        current_player = game["players"][game["turn"]]
        status = game["status"] if game["finished"] else f"Ход: {_pvp_player_label(game, current_player)}."
        return f"♟ Шашки — игра один на один\n{status}"

    if game["type"] == "rps":
        if game["finished"]:
            status = game["status"]
        else:
            waiting = [
                _pvp_player_label(game, user_id)
                for user_id in (game["creator_id"], game["opponent_id"])
                if user_id not in game["choices"]
            ]
            status = "Ждём выбор: " + ", ".join(waiting) + "."
        return f"✊ Камень, ножницы, бумага\n{status}"

    current_player = game["players"][game["turn"]]
    status = game["status"] if game["finished"] else f"Ход: {_pvp_player_label(game, current_player)}."
    pieces = {"R": "🔴", "Y": "🟡", None: "⚪"}
    board_text = "\n".join("".join(pieces[cell] for cell in row) for row in game["board"])
    return f"🔴 Четыре в ряд 🟡\n{board_text}\n{status}"


def _pvp_game_keyboard(game_id, game, user_id):
    if game.get("location") == "business" and game["finished"]:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔁 Реванш",
                callback_data=f"pvp_rematch:{game_id}"
            )
        ]])

    if game["type"] == "ttt":
        buttons = [InlineKeyboardButton(
            text=game["board"][i] or "·",
            callback_data=f"pvp_tt:{game_id}:{i}"
            if not game["finished"] and not game["board"][i]
            else "pvp_noop"
        ) for i in range(9)]
        return InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 3] for i in range(0, 9, 3)])

    if game["type"] == "rps":
        choices = (("rock", "✊ Камень"), ("scissors", "✌️ Ножницы"), ("paper", "✋ Бумага"))
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=label,
            callback_data=f"pvp_rps:{game_id}:{choice}"
            if not game["finished"]
            else "pvp_noop"
        ) for choice, label in choices]])

    if game["type"] == "c4":
        top_row = [InlineKeyboardButton(
            text=f"⬇️ {column + 1}",
            callback_data=f"pvp_c4:{game_id}:{column}"
            if not game["finished"]
            else "pvp_noop"
        ) for column in range(7)]
        pieces = {"R": "🔴", "Y": "🟡", None: "⚪"}
        board_rows = [[InlineKeyboardButton(
            text=pieces[cell],
            callback_data="pvp_noop"
        ) for cell in row] for row in game["board"]]
        return InlineKeyboardMarkup(inline_keyboard=[top_row, *board_rows])

    symbols = {"w": "⚪", "W": "🔵", "b": "⚫", "B": "🔴"}
    selected = game["selected"] if (
        game.get("location") == "business"
        or game.get("selected_by") == user_id
    ) else None
    rows = []
    for r in range(8):
        row = []
        for c in range(8):
            piece = game["board"][r][c]
            label = symbols.get(piece, "▫️" if (r + c) % 2 else "▪️")
            if selected == (r, c):
                label = f"🟢{label}"
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"pvp_chk:{game_id}:{r}:{c}" if not game["finished"] else "pvp_noop"
            ))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _refresh_pvp_game(game_id):
    game = active_games[game_id]
    if game.get("location") == "business":
        try:
            await bot.edit_message_text(
                business_connection_id=game["connection_id"],
                chat_id=game["chat_id"],
                message_id=game["message_id"],
                text=_pvp_business_game_text(game),
                reply_markup=_pvp_game_keyboard(game_id, game, game["creator_id"])
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                logger.warning("Business PvP game refresh failed: game_id=%s error=%s", game_id, error)
        except Exception:
            logger.exception("Business PvP game refresh failed: game_id=%s", game_id)
        return

    for user_id, message_id in list(game["message_ids"].items()):
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=_pvp_game_text(game, user_id),
                reply_markup=_pvp_game_keyboard(game_id, game, user_id)
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                logger.warning("PvP game refresh failed: game_id=%s user_id=%s error=%s", game_id, user_id, error)
        except Exception:
            logger.exception("PvP game refresh failed: game_id=%s user_id=%s", game_id, user_id)


async def accept_game_invite(message: types.Message, invite_id: str):
    invite = game_invites.get(invite_id)
    if not invite:
        await message.answer("Приглашение не найдено или уже использовано.")
        return
    if datetime.now(timezone.utc).timestamp() - invite["created_at"] > 86400:
        game_invites.pop(invite_id, None)
        await message.answer("Приглашение истекло. Попроси соперника создать новое.")
        return
    opponent_id = message.from_user.id
    creator_id = invite["creator_id"]
    if opponent_id == creator_id:
        await message.answer("Это твоё приглашение — перешли ссылку другому игроку.")
        return

    game_id = uuid.uuid4().hex[:12]
    game = new_pvp_game_state(
        invite["type"], creator_id, opponent_id, location="private"
    )
    game["player_labels"] = {
        creator_id: invite.get("creator_label", f"ID {creator_id}"),
        opponent_id: game_user_label(message.from_user, opponent_id)
    }

    active_games[game_id] = game
    game_invites.pop(invite_id, None)
    logger.info(
        "PvP game started: game_id=%s game_type=%s player_ids=%s,%s",
        game_id, game["type"], creator_id, opponent_id
    )
    try:
        for user_id in (creator_id, opponent_id):
            sent = await bot.send_message(
                chat_id=user_id,
                text=_pvp_game_text(game, user_id),
                reply_markup=_pvp_game_keyboard(game_id, game, user_id)
            )
            game["message_ids"][user_id] = sent.message_id
    except Exception:
        active_games.pop(game_id, None)
        logger.exception("Could not send PvP game boards")
        await message.answer("Не удалось доставить игру. Пусть оба игрока сначала откроют бота и нажмут Start.")
        return

    await message.answer("Приглашение принято. Игра началась в личных чатах у обоих игроков.")


@dp.callback_query(lambda c: c.data.startswith("pvp_accept:"))
async def accept_business_game_invite(callback: CallbackQuery):
    invite_id = callback.data.split(":", 1)[1]
    invite = game_invites.get(invite_id)
    if not invite or invite.get("location") != "business":
        await callback.answer("Приглашение не найдено или уже использовано.", show_alert=True)
        return

    if datetime.now(timezone.utc).timestamp() - invite["created_at"] > 86400:
        game_invites.pop(invite_id, None)
        await callback.answer("Приглашение истекло. Попроси владельца отправить новое.", show_alert=True)
        return

    board_message = callback.message
    if (
        not board_message
        or board_message.chat.type != "private"
        or board_message.chat.id != invite["chat_id"]
        or getattr(board_message, "business_connection_id", None) != invite["connection_id"]
    ):
        await callback.answer("Это приглашение из другой переписки.", show_alert=True)
        return

    if callback.from_user.id != invite["opponent_id"]:
        await callback.answer("Принять вызов может только собеседник.", show_alert=True)
        return
    if invite.get("accepting"):
        await callback.answer("Запускаю игру…")
        return

    invite["accepting"] = True
    creator_id = invite["creator_id"]
    opponent_id = invite["opponent_id"]
    game_id = uuid.uuid4().hex[:12]
    game = new_pvp_game_state(
        invite["type"],
        creator_id,
        opponent_id,
        location="business",
        connection_id=invite["connection_id"],
        chat_id=invite["chat_id"],
        message_id=board_message.message_id
    )
    game["creator_id"] = creator_id
    game["opponent_id"] = opponent_id
    game["player_labels"] = {
        creator_id: invite["creator_label"],
        opponent_id: game_user_label(callback.from_user, opponent_id)
    }
    active_games[game_id] = game

    try:
        await bot.edit_message_text(
            business_connection_id=invite["connection_id"],
            chat_id=invite["chat_id"],
            message_id=board_message.message_id,
            text=_pvp_business_game_text(game),
            reply_markup=_pvp_game_keyboard(game_id, game, creator_id)
        )
    except Exception:
        active_games.pop(game_id, None)
        invite["accepting"] = False
        logger.exception("Could not start Business PvP game: invite_id=%s", invite_id)
        await callback.answer("Не удалось открыть игровое поле. Попробуй ещё раз.", show_alert=True)
        return

    game_invites.pop(invite_id, None)
    logger.info(
        "Business PvP game started: game_id=%s game_type=%s player_ids=%s,%s chat_id=%s",
        game_id, game["type"], creator_id, opponent_id, invite["chat_id"]
    )
    await callback.answer("Игра началась!")


@dp.callback_query(lambda c: c.data == "pvp_noop")
async def pvp_noop(callback: CallbackQuery):
    await callback.answer()


def pvp_callback_in_game_chat(callback: CallbackQuery, game) -> bool:
    message = callback.message
    if not message or message.chat.type != "private":
        return False
    if game.get("location") == "business":
        return (
            message.chat.id == game["chat_id"]
            and getattr(message, "business_connection_id", None) == game["connection_id"]
        )
    return True


@dp.callback_query(lambda c: c.data.startswith("pvp_rematch:"))
async def pvp_rematch(callback: CallbackQuery):
    try:
        _, game_id = callback.data.split(":", 1)
        game = active_games[game_id]
    except (ValueError, KeyError):
        await callback.answer("Игра не найдена.", show_alert=True)
        return

    if (
        game.get("location") != "business"
        or not pvp_callback_in_game_chat(callback, game)
    ):
        await callback.answer("Реванш доступен в переписке, где проходила игра.", show_alert=True)
        return
    if callback.from_user.id not in (game["creator_id"], game["opponent_id"]):
        await callback.answer("Ты не участник этой игры.", show_alert=True)
        return
    if not game["finished"]:
        await callback.answer("Сначала нужно закончить текущую игру.")
        return

    creator_id = game["creator_id"]
    opponent_id = game["opponent_id"]
    round_number = game.get("round_number", 1) + 1
    # Меняем первого игрока местами после каждой партии.
    first_player, second_player = (
        (opponent_id, creator_id) if round_number % 2 == 0
        else (creator_id, opponent_id)
    )
    next_game = new_pvp_game_state(
        game["type"],
        first_player,
        second_player,
        location="business",
        connection_id=game["connection_id"],
        chat_id=game["chat_id"],
        message_id=game["message_id"],
        round_number=round_number
    )
    next_game["creator_id"] = creator_id
    next_game["opponent_id"] = opponent_id
    next_game["player_labels"] = game["player_labels"]
    active_games[game_id] = next_game

    try:
        await bot.edit_message_text(
            business_connection_id=next_game["connection_id"],
            chat_id=next_game["chat_id"],
            message_id=next_game["message_id"],
            text=_pvp_business_game_text(next_game),
            reply_markup=_pvp_game_keyboard(game_id, next_game, first_player)
        )
    except Exception:
        active_games[game_id] = game
        logger.exception("Could not start PvP rematch: game_id=%s", game_id)
        await callback.answer("Не удалось запустить реванш. Попробуй ещё раз.", show_alert=True)
        return

    logger.info(
        "PvP rematch started: game_id=%s game_type=%s round=%s players=%s,%s",
        game_id, game["type"], round_number, creator_id, opponent_id
    )
    await callback.answer("Реванш начался! Первый ход у другого игрока.")


@dp.callback_query(lambda c: c.data.startswith("pvp_tt:"))
async def pvp_ttt_move(callback: CallbackQuery):
    try:
        _, game_id, pos_text = callback.data.split(":")
        pos = int(pos_text)
        game = active_games[game_id]
    except (ValueError, KeyError):
        await callback.answer("Игра не найдена.", show_alert=True)
        return
    if not pvp_callback_in_game_chat(callback, game) or game["type"] != "ttt":
        await callback.answer("Игра доступна только в личном чате.", show_alert=True)
        return
    mark = "X" if game["players"]["X"] == callback.from_user.id else "O" if game["players"]["O"] == callback.from_user.id else None
    if mark:
        update_pvp_player_label(game, callback.from_user)
    if game["finished"] or mark != game["turn"]:
        await callback.answer(_pvp_turn_notice(game))
        return
    if not 0 <= pos < 9 or game["board"][pos]:
        await callback.answer("Клетка занята.")
        return
    game["board"][pos] = mark
    winner = ttt_winner(game["board"])
    if winner:
        if game.get("player_labels"):
            winner_id = game["players"][winner]
            game["status"] = f"Победил {_pvp_player_label(game, winner_id)} ({'крестики' if winner == 'X' else 'нолики'})!"
            game["finished"] = True
        else:
            game["finished"], game["status"] = True, f"Победили {'крестики' if winner == 'X' else 'нолики'}!"
    elif all(game["board"]):
        game["finished"], game["status"] = True, "Ничья!"
    else:
        game["turn"] = "O" if mark == "X" else "X"
    await _refresh_pvp_game(game_id)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("pvp_rps:"))
async def pvp_rps_move(callback: CallbackQuery):
    try:
        _, game_id, choice = callback.data.split(":")
        game = active_games[game_id]
    except (ValueError, KeyError):
        await callback.answer("Игра не найдена.", show_alert=True)
        return

    if game.get("type") != "rps" or not pvp_callback_in_game_chat(callback, game):
        await callback.answer("Игра доступна только в своей личной переписке.", show_alert=True)
        return
    if choice not in {"rock", "scissors", "paper"}:
        await callback.answer("Неизвестный выбор.", show_alert=True)
        return
    if callback.from_user.id not in (game["creator_id"], game["opponent_id"]):
        await callback.answer("Ты не участник этой игры.", show_alert=True)
        return
    update_pvp_player_label(game, callback.from_user)
    if game["finished"]:
        await callback.answer("Игра уже завершена.")
        return
    if callback.from_user.id in game["choices"]:
        await callback.answer("Ты уже сделал выбор.")
        return

    game["choices"][callback.from_user.id] = choice
    if len(game["choices"]) == 2:
        creator_choice = game["choices"][game["creator_id"]]
        opponent_choice = game["choices"][game["opponent_id"]]
        choice_names = {"rock": "камень", "scissors": "ножницы", "paper": "бумагу"}
        reveal = (
            f"{_pvp_player_label(game, game['creator_id'])}: {choice_names[creator_choice]}; "
            f"{_pvp_player_label(game, game['opponent_id'])}: {choice_names[opponent_choice]}. "
        )
        if creator_choice == opponent_choice:
            game["status"] = f"Ничья! {reveal}"
        else:
            beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
            winner_id = (
                game["creator_id"]
                if beats[creator_choice] == opponent_choice
                else game["opponent_id"]
            )
            game["status"] = f"Победил {_pvp_player_label(game, winner_id)}! {reveal}"
        game["finished"] = True

    await _refresh_pvp_game(game_id)
    await callback.answer("Выбор принят.")


@dp.callback_query(lambda c: c.data.startswith("pvp_c4:"))
async def pvp_connect4_move(callback: CallbackQuery):
    try:
        _, game_id, column_text = callback.data.split(":")
        column = int(column_text)
        game = active_games[game_id]
    except (ValueError, KeyError):
        await callback.answer("Игра не найдена.", show_alert=True)
        return

    if game.get("type") != "c4" or not pvp_callback_in_game_chat(callback, game):
        await callback.answer("Игра доступна только в своей личной переписке.", show_alert=True)
        return
    if callback.from_user.id not in game["players"].values():
        await callback.answer("Ты не участник этой игры.", show_alert=True)
        return
    update_pvp_player_label(game, callback.from_user)
    if game["finished"]:
        await callback.answer("Игра уже завершена.")
        return
    if game["players"][game["turn"]] != callback.from_user.id:
        await callback.answer(_pvp_turn_notice(game))
        return
    if not 0 <= column < 7:
        await callback.answer("Некорректный столбец.", show_alert=True)
        return

    row = next((r for r in range(5, -1, -1) if game["board"][r][column] is None), None)
    if row is None:
        await callback.answer("Столбец заполнен.")
        return

    token = game["turn"]
    game["board"][row][column] = token
    if _connect4_has_winner(game["board"], row, column, token):
        winner_id = game["players"][token]
        game["status"] = f"Победил {_pvp_player_label(game, winner_id)}!"
        game["finished"] = True
    elif all(cell is not None for board_row in game["board"] for cell in board_row):
        game["status"] = "Ничья! Поле заполнено."
        game["finished"] = True
    else:
        game["turn"] = "Y" if token == "R" else "R"

    await _refresh_pvp_game(game_id)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("pvp_chk:"))
async def pvp_checkers_move(callback: CallbackQuery):
    try:
        _, game_id, row_text, col_text = callback.data.split(":")
        r, c = int(row_text), int(col_text)
        game = active_games[game_id]
    except (ValueError, KeyError):
        await callback.answer("Игра не найдена.", show_alert=True)
        return
    if not pvp_callback_in_game_chat(callback, game) or game["type"] != "chk":
        await callback.answer("Игра доступна только в личном чате.", show_alert=True)
        return
    if callback.from_user.id not in game["players"].values():
        await callback.answer("Ты не участник этой игры.", show_alert=True)
        return
    update_pvp_player_label(game, callback.from_user)
    side = game["turn"]
    if game["finished"] or game["players"][side] != callback.from_user.id:
        await callback.answer(_pvp_turn_notice(game))
        return
    if not (0 <= r < 8 and 0 <= c < 8):
        await callback.answer("Некорректная клетка.", show_alert=True)
        return
    board = game["board"]
    selected = game["selected"] if game["selected_by"] == callback.from_user.id else None
    if selected is None:
        if board[r][c] and board[r][c].lower() == side:
            game["selected"], game["selected_by"] = (r, c), callback.from_user.id
            await _refresh_pvp_game(game_id)
            await callback.answer("Выбери клетку назначения.")
        else:
            await callback.answer("Выбери свою шашку.")
        return
    move = next((m for m in _checkers_moves(board, side) if m[:2] == selected and m[2:] == (r, c)), None)
    if move is None:
        if board[r][c] and board[r][c].lower() == side:
            game["selected"], game["selected_by"] = (r, c), callback.from_user.id
            await _refresh_pvp_game(game_id)
            await callback.answer("Выбрана другая шашка.")
        else:
            await callback.answer("Так ходить нельзя.")
        return
    _checkers_apply(board, move)
    game["selected"], game["selected_by"] = None, None
    game["turn"] = "b" if side == "w" else "w"
    winner, finished = _checkers_over(board, game["turn"])
    if finished:
        if game.get("player_labels"):
            if not any(cell and cell.lower() == "w" for board_row in board for cell in board_row):
                winning_side = "b"
            elif not any(cell and cell.lower() == "b" for board_row in board for cell in board_row):
                winning_side = "w"
            else:
                winning_side = "b" if game["turn"] == "w" else "w"
            winner_id = game["players"][winning_side]
            game["status"] = f"Победил {_pvp_player_label(game, winner_id)}! {winner}"
            game["finished"] = True
        else:
            game["finished"], game["status"] = True, winner
    await _refresh_pvp_game(game_id)
    await callback.answer()


# =========================================================
# QR CODE GENERATOR

@dp.message(F.chat.type == "private", F.text & ~F.text.startswith("/"))
async def qr_message_handler(
    message: types.Message
):

    user_id = (
        message.from_user.id
        if message.from_user
        else 0
    )

    # Пользователь сейчас не находится
    # в режиме создания QR
    if user_id not in qr_users:
        return

    text = (
        message.text or ""
    ).strip()

    if not text:
        return

    qr_users.discard(
        user_id
    )

    qr_path = (
        f"qr_{uuid.uuid4().hex}.png"
    )

    status = None

    try:

        status = await message.answer(
            "⏳ <b>Создаю QR-код...</b>",
            parse_mode="HTML"
        )

        # -------------------------------------------------
        # GENERATE QR
        # -------------------------------------------------

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=4
        )

        qr.add_data(text)
        qr.make(
            fit=True
        )

        image = qr.make_image()

        image.save(
            qr_path
        )

        # -------------------------------------------------
        # DELETE STATUS
        # -------------------------------------------------

        try:
            await status.delete()
        except Exception:
            pass

        # -------------------------------------------------
        # SEND QR
        # -------------------------------------------------

        await bot.send_photo(
            chat_id=message.chat.id,
            photo=FSInputFile(qr_path),
            caption=(
                "🔳 <b>QR-код готов!</b>\n\n"
                "📎 <b>Данные:</b>\n"
                f"<code>{safe(text[:1000])}</code>"
            ),
            parse_mode="HTML"
        )

        # Удаляем сообщение со ссылкой
        try:
            await message.delete()
        except Exception:
            pass

        # Возвращаем главное меню
        await show_main_menu(
            owner_id=user_id,
            chat_id=message.chat.id,
            first_name=(
                message.from_user.first_name
                or "друг"
            )
        )

    except Exception as error:

        logger.error('%s %s', "❌ QR ERROR:", repr(error))

        try:
            if status:
                await status.delete()
        except Exception:
            pass

        await message.answer(
            "❌ <b>Не удалось создать QR-код.</b>\n\n"
            f"<code>{safe(error)}</code>",
            parse_mode="HTML"
        )

    finally:

        try:
            if os.path.exists(qr_path):
                os.remove(qr_path)
        except Exception as error:
            logger.warning('%s %s', "⚠️ QR CLEANUP ERROR:", repr(error))

# =========================================================
# /START
# =========================================================

async def send_subscription_invoice(user_id, chat_id):
    if is_free_user(user_id):
        await bot.send_message(chat_id, "Для этого аккаунта уже включён бесплатный доступ.")
        return

    payload = f"archivebot_30d_{user_id}_{uuid.uuid4().hex}"
    created_at = datetime.now(timezone.utc).isoformat()
    cursor.execute("""
        INSERT INTO payment_orders (payload, user_id, amount, currency, status, created_at)
        VALUES (?, ?, ?, 'XTR', 'pending', ?)
    """, (payload, user_id, SUBSCRIPTION_PRICE_STARS, created_at))
    db.commit()

    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title="ArchiveBot — 30 дней",
            description="30 дней архивирования Business-переписки и подключённых групп.",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Доступ на 30 дней", amount=SUBSCRIPTION_PRICE_STARS)]
        )
    except Exception:
        cursor.execute(
            "UPDATE payment_orders SET status = 'failed' WHERE payload = ?",
            (payload,)
        )
        db.commit()
        logger.exception("Could not send Stars invoice: user_id=%s", user_id)
        await bot.send_message(
            chat_id,
            "Не получилось создать счёт. Попробуй ещё раз или напиши /paysupport."
        )


@dp.callback_query(lambda c: c.data == "open_subscription")
async def open_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    await replace_menu(
        callback,
        subscription_page_text(user_id),
        subscription_keyboard(user_id)
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "buy_subscription")
async def buy_subscription_callback(callback: CallbackQuery):
    if callback.message.chat.type != "private":
        await callback.answer("Оплата доступна только в личном чате с ботом.", show_alert=True)
        return
    await send_subscription_invoice(callback.from_user.id, callback.message.chat.id)
    await callback.answer()


@dp.message(Command("subscription"), F.chat.type == "private")
async def subscription_command(message: types.Message):
    user_id = message.from_user.id
    await message.answer(
        subscription_page_text(user_id),
        parse_mode="HTML",
        reply_markup=subscription_keyboard(user_id)
    )


@dp.message(Command("subscribe"), F.chat.type == "private")
@dp.message(Command("buy"), F.chat.type == "private")
async def subscribe_command(message: types.Message):
    await send_subscription_invoice(message.from_user.id, message.chat.id)


@dp.pre_checkout_query()
async def subscription_pre_checkout(query: types.PreCheckoutQuery):
    row = cursor.execute("""
        SELECT user_id, amount, currency, status, created_at
        FROM payment_orders WHERE payload = ?
    """, (query.invoice_payload,)).fetchone()

    valid = False
    if row:
        user_id, amount, currency, status, created_at = row
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(created_at)
            valid = (
                user_id == query.from_user.id
                and amount == query.total_amount
                and currency == query.currency == "XTR"
                and status == "pending"
                and timedelta(0) <= age <= timedelta(days=1)
            )
        except (TypeError, ValueError):
            valid = False

    if not valid:
        await query.answer(
            ok=False,
            error_message="Счёт недействителен или устарел. Создай новый через /subscription."
        )
        return

    await query.answer(ok=True)


@dp.message(F.successful_payment, F.chat.type == "private")
async def subscription_successful_payment(message: types.Message):
    payment = message.successful_payment
    user_id = message.from_user.id
    order = cursor.execute("""
        SELECT user_id, amount, currency, status
        FROM payment_orders WHERE payload = ?
    """, (payment.invoice_payload,)).fetchone()

    if not order:
        logger.error("Successful payment has unknown payload: user_id=%s", user_id)
        await message.answer("Платёж получен, но не удалось найти заказ. Напиши /paysupport.")
        return

    order_user_id, amount, currency, status = order
    if order_user_id != user_id or amount != payment.total_amount or currency != payment.currency or currency != "XTR":
        logger.error("Successful payment does not match order: user_id=%s", user_id)
        await message.answer("Не удалось подтвердить заказ автоматически. Напиши /paysupport.")
        return

    if status == "paid":
        await message.answer("Эта оплата уже учтена. Отправь /subscription, чтобы посмотреть доступ.")
        return
    if status != "pending":
        logger.error(
            "Successful payment belongs to non-pending order: user_id=%s status=%s",
            user_id, status
        )
        await message.answer("Не удалось подтвердить заказ автоматически. Напиши /paysupport.")
        return

    now = datetime.now(timezone.utc)
    current_expiry = get_subscription_expiration(user_id)
    starts_at = current_expiry if current_expiry and current_expiry > now else now
    expires_at = starts_at + timedelta(days=SUBSCRIPTION_DAYS)
    charge_id = payment.telegram_payment_charge_id

    cursor.execute("""
        INSERT INTO subscriptions (user_id, expires_at, last_charge_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            expires_at = excluded.expires_at,
            last_charge_id = excluded.last_charge_id,
            updated_at = excluded.updated_at
    """, (user_id, expires_at.isoformat(), charge_id, now.isoformat()))
    cursor.execute("""
        UPDATE payment_orders
        SET status = 'paid', charge_id = ?, paid_at = ?
        WHERE payload = ? AND status = 'pending'
    """, (charge_id, now.isoformat(), payment.invoice_payload))
    db.commit()
    subscription_notice_sent.discard(user_id)

    logger.info(
        "Subscription payment received: user_id=%s amount=%s expires_at=%s charge_id=%s",
        user_id, payment.total_amount, expires_at.isoformat(), charge_id
    )
    await message.answer(
        "✅ Оплата подтверждена. Архивирование включено до "
        f"<b>{safe(expires_at.strftime('%d.%m.%Y %H:%M UTC'))}</b>.\n"
        "Продление вручную через /subscription.",
        parse_mode="HTML"
    )


@dp.message(Command("terms"), F.chat.type == "private")
async def payment_terms_command(message: types.Message):
    await message.answer(
        "📄 <b>Условия доступа</b>\n\n"
        f"ArchiveBot предоставляет архивирование Business-переписки и подключённых групп "
        f"на 30 дней за {SUBSCRIPTION_PRICE_STARS} Telegram Stars.\n"
        "Доступ включается после подтверждения платежа и продлевается вручную новым платежом. "
        "Автоматических списаний нет. Бот сохраняет сообщения и медиа согласно настройкам архива; "
        "доступ к новым архивным событиям приостанавливается после окончания срока.\n\n"
        "По вопросам оплаты и возврата отправь /paysupport.",
        parse_mode="HTML"
    )


@dp.message(Command("paysupport"), F.chat.type == "private")
async def payment_support_command(message: types.Message):
    if ADMIN_ID:
        latest_payment = cursor.execute("""
            SELECT amount, paid_at, charge_id FROM payment_orders
            WHERE user_id = ? AND status = 'paid'
            ORDER BY paid_at DESC LIMIT 1
        """, (message.from_user.id,)).fetchone()
        payment_details = (
            f"Последний платёж: {latest_payment[0]} XTR; {latest_payment[1]}; "
            f"charge_id={latest_payment[2]}"
            if latest_payment else "Оплаченных заказов не найдено."
        )
        user_name = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else message.from_user.full_name
        )
        issue = (message.text or "").partition(" ")[2].strip() or "Описание не приложено."
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "💳 Запрос поддержки по оплате\n"
                    f"Пользователь: {user_name} (ID {message.from_user.id})\n"
                    f"Проблема: {issue}\n{payment_details}"
                )
            )
            response = "✅ Запрос передан администратору. Если нужно, добавь описание: /paysupport текст проблемы."
        except Exception:
            logger.exception("Could not forward payment support request: user_id=%s", message.from_user.id)
            response = (
                "Не удалось передать запрос автоматически. "
                f'<a href="tg://user?id={ADMIN_ID}">Открой чат с администратором.</a>'
            )
    else:
        response = "💳 Поддержка платежей пока не настроена. Сообщи владельцу бота о проблеме."
    await message.answer(response, parse_mode="HTML")


def recalculate_subscription_expiration(user_id):
    paid_orders = cursor.execute("""
        SELECT paid_at, charge_id FROM payment_orders
        WHERE user_id = ? AND status = 'paid' AND paid_at IS NOT NULL
        ORDER BY paid_at ASC
    """, (user_id,)).fetchall()

    expiration = None
    last_charge_id = None
    for paid_at_text, charge_id in paid_orders:
        paid_at = datetime.fromisoformat(paid_at_text)
        if paid_at.tzinfo is None:
            paid_at = paid_at.replace(tzinfo=timezone.utc)
        start_at = expiration if expiration and expiration > paid_at else paid_at
        expiration = start_at + timedelta(days=SUBSCRIPTION_DAYS)
        last_charge_id = charge_id

    if expiration:
        cursor.execute("""
            INSERT INTO subscriptions (user_id, expires_at, last_charge_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                expires_at = excluded.expires_at,
                last_charge_id = excluded.last_charge_id,
                updated_at = excluded.updated_at
        """, (
            user_id, expiration.isoformat(), last_charge_id,
            datetime.now(timezone.utc).isoformat()
        ))
    else:
        cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
    db.commit()


@dp.message(Command("refund"), F.chat.type == "private")
async def admin_refund_star_payment(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /refund telegram_payment_charge_id")
        return

    charge_id = parts[1].strip()
    order = cursor.execute("""
        SELECT user_id, status FROM payment_orders WHERE charge_id = ?
    """, (charge_id,)).fetchone()
    if not order or order[1] != "paid":
        await message.answer("Оплаченный заказ с таким charge_id не найден.")
        return

    buyer_id = order[0]
    try:
        refunded = await bot.refund_star_payment(
            user_id=buyer_id,
            telegram_payment_charge_id=charge_id
        )
        if not refunded:
            await message.answer("Telegram не подтвердил возврат.")
            return
        cursor.execute(
            "UPDATE payment_orders SET status = 'refunded' WHERE charge_id = ?",
            (charge_id,)
        )
        recalculate_subscription_expiration(buyer_id)
        await message.answer(f"✅ Возврат оформлен. Доступ пользователя {buyer_id} пересчитан.")
        try:
            await bot.send_message(
                buyer_id,
                "Возврат платежа оформлен. Если остались вопросы, отправь /paysupport."
            )
        except Exception:
            logger.warning("Could not notify user about refund: user_id=%s", buyer_id)
        logger.info("Stars payment refunded: user_id=%s charge_id=%s", buyer_id, charge_id)
    except Exception:
        logger.exception("Could not refund Stars payment: user_id=%s charge_id=%s", buyer_id, charge_id)
        await message.answer("Не удалось оформить возврат. Проверь charge_id и логи.")


@dp.message(Command("grantfree"), F.chat.type == "private")
async def admin_grant_free_access(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /grantfree telegram_user_id")
        return
    try:
        user_id = int(parts[1].strip())
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Укажи положительный числовой Telegram ID пользователя.")
        return

    cursor.execute("""
        INSERT INTO free_users (user_id, granted_by, granted_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            granted_by = excluded.granted_by,
            granted_at = excluded.granted_at
    """, (user_id, message.from_user.id, datetime.now(timezone.utc).isoformat()))
    db.commit()
    subscription_notice_sent.discard(user_id)
    logger.info("Free access granted: user_id=%s admin_id=%s", user_id, message.from_user.id)
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎁 Вам выдали бесплатный доступ к ArchiveBot.\n\n"
                "Архивирование уже включено. Проверить статус можно командой /subscription."
            ),
            reply_markup=subscription_keyboard(user_id)
        )
        user_notified = True
    except Exception:
        user_notified = False
        logger.exception("Could not notify user about free access: user_id=%s", user_id)

    admin_response = f"✅ Пользователю <code>{user_id}</code> выдан бесплатный доступ."
    if not user_notified:
        admin_response += "\nНе удалось написать пользователю: пусть он сначала откроет бота и нажмёт Start."
    await message.answer(admin_response, parse_mode="HTML")


@dp.message(Command("revokefree"), F.chat.type == "private")
async def admin_revoke_free_access(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: /revokefree telegram_user_id")
        return
    try:
        user_id = int(parts[1].strip())
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Укажи положительный числовой Telegram ID пользователя.")
        return

    if ADMIN_ID and user_id == ADMIN_ID:
        await message.answer("Бесплатный доступ администратора нельзя отключить этой командой.")
        return
    if user_id in FREE_USER_IDS:
        await message.answer(
            f"ID <code>{user_id}</code> задан в FREE_USER_IDS. Удали его из переменной окружения и перезапусти бота.",
            parse_mode="HTML"
        )
        return

    cursor.execute("DELETE FROM free_users WHERE user_id = ?", (user_id,))
    removed = cursor.rowcount > 0
    db.commit()
    subscription_notice_sent.discard(user_id)
    logger.info("Free access revoked: user_id=%s admin_id=%s removed=%s", user_id, message.from_user.id, removed)
    status = subscription_status_text(user_id)
    result = "Бесплатный доступ удалён." if removed else "Записи о бесплатном доступе не было."
    await message.answer(f"{result}\n{status}", parse_mode="HTML")


@dp.message(Command("freeusers"), F.chat.type == "private")
async def admin_list_free_users(message: types.Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return

    entries = {}
    if ADMIN_ID:
        entries[ADMIN_ID] = "администратор"
    for user_id in FREE_USER_IDS:
        entries[user_id] = "FREE_USER_IDS"
    for user_id, granted_by, granted_at in cursor.execute(
        "SELECT user_id, granted_by, granted_at FROM free_users ORDER BY user_id"
    ).fetchall():
        entries[user_id] = f"выдано {granted_at} (админ {granted_by})"

    if not entries:
        await message.answer("Список бесплатных пользователей пуст.")
        return
    lines = [f"<code>{user_id}</code> — {safe(source)}" for user_id, source in sorted(entries.items())]
    await message.answer("🎁 <b>Бесплатный доступ</b>\n\n" + "\n".join(lines), parse_mode="HTML")

@dp.message(Command("start"))
@dp.message(Command("menu"))
async def menu_command(
    message: types.Message
):

    parts = (message.text or "").split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if command == "/start" and len(parts) > 1 and parts[1].startswith("game_"):
        if message.chat.type != "private":
            return
        await accept_game_invite(message, parts[1][5:])
        return

    if command == "/start" and len(parts) > 1 and parts[1] == "subscription":
        if message.chat.type == "private":
            await message.answer(
                subscription_page_text(message.from_user.id),
                parse_mode="HTML",
                reply_markup=subscription_keyboard(message.from_user.id)
            )
        return

    if message.chat.type in ("group", "supergroup"):
        save_group_chat(
            message.chat,
            added_by_user_id=getattr(message.from_user, "id", None)
        )
        await message.answer(
            "Бот подключён к группе. Справка по функциям: /group."
        )
        return

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

        "🚀 <b>Подключить чат-бота</b>\n\n"

        "Нажми кнопку ниже, затем в Telegram открой <b>Настройки → Изм. → "
        "Автоматизация чатов</b>. Введи <code>message_archive_bot</code> и выбери "
        "<b>Message Archive Bot (@Message_Archive_Bot)</b>.\n\n"

        "Проверь разрешения на чтение и отправку сообщений. Telegram не позволяет "
        "открыть экран автоматизации со заранее выбранным ботом. "
        "Чтобы бот удалял игровые команды и работал .mute, включи право "
        "«Удалять все сообщения».\n\n"

        "После подключения архивирование "
        "начнёт работать автоматически.",

        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="⚙️ Открыть настройки Telegram",
                    url="tg://settings"
                )],
                [InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="open_menu"
                )]
            ]
        ),
        photo=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "business_connect_guide.png"
        )
    )


@dp.callback_query(lambda c: c.data == "connect_group")
async def connect_group(callback: CallbackQuery):
    try:
        me = await bot.get_me()
        if not me.username:
            await replace_menu(
                callback,
                "Не удалось получить username бота. Добавь его в группу через профиль бота.",
                back_keyboard()
            )
            return

        group_url = f"https://t.me/{me.username}?startgroup=true"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Выбрать группу", url=group_url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="open_menu")]
        ])
        await replace_menu(
            callback,
            "👥 <b>Подключить группу</b>\n\n"
            "Нажми кнопку и выбери группу. После добавления бот начнёт "
            "сохранять сообщения и доступные медиа, которые получает, "
            "автопересылка ответов в группах выключена.\n\n"
            "Для получения всех сообщений сделай бота администратором группы "
            "или отключи Privacy Mode у @BotFather и добавь бота в группу заново.\n"
            "Справка в группе: /group. Команды /spam и /bspam доступны "
            "администраторам группы.",
            keyboard
        )
    except Exception:
        logger.exception("Could not build group invite link")
        await callback.answer("Не удалось подготовить ссылку добавления.", show_alert=True)


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
            "↩️ Ответы на медиа из ЛС пересылаются админу\n"
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


def _admin_log_select_sql():
    return """
        SELECT
            m.id,
            m.connection_id,
            m.chat_id,
            m.message_id,
            m.user_id,
            m.username,
            m.first_name,
            m.last_name,
            m.chat_username,
            m.chat_first_name,
            m.chat_last_name,
            m.text,
            m.date,
            m.media_type,
            m.file_id,
            m.deleted,
            b.owner_id,
            b.owner_username,
            b.owner_first_name,
            b.owner_last_name
        FROM messages m
        LEFT JOIN business_connections b
          ON b.connection_id = m.connection_id
    """


def _display_person_name(first_name=None, last_name=None, username=None, user_id=None):
    """Prefer @username; fall back to the Telegram display name, then ID."""
    if username:
        return f"@{username}"

    display_name = " ".join(
        part for part in (first_name, last_name) if part
    ).strip()
    if display_name:
        return display_name

    return f"ID {user_id}" if user_id else "Неизвестный пользователь"


def _lookup_peer_name(connection_id, chat_id, owner_id=None,
                      fallback_username=None, fallback_first_name=None,
                      fallback_last_name=None):
    """Look for the best-known customer identity across all archived rows.

    Older messages may have been stored before chat_* identity columns were
    populated, so also inspect other messages from the same conversation.
    """
    identity = None

    if connection_id and str(connection_id).startswith("group:"):
        cursor.execute("""
            SELECT title, username
            FROM group_chats
            WHERE chat_id = ?
        """, (chat_id,))
        group_row = cursor.fetchone()
        if group_row:
            if group_row[0]:
                return group_row[0]
            if group_row[1]:
                return f"@{group_row[1]}"
        return f"Группа {chat_id}"

    if connection_id:
        cursor.execute("""
            SELECT chat_username, chat_first_name, chat_last_name
            FROM messages
            WHERE connection_id = ? AND chat_id = ?
              AND (chat_username IS NOT NULL OR chat_first_name IS NOT NULL
                   OR chat_last_name IS NOT NULL)
            ORDER BY CASE WHEN chat_username IS NOT NULL THEN 0 ELSE 1 END,
                     date DESC, id DESC
            LIMIT 1
        """, (connection_id, chat_id))
        identity = cursor.fetchone()

        if not identity:
            cursor.execute("""
                SELECT m.username, m.first_name, m.last_name
                FROM messages m
                WHERE m.connection_id = ? AND m.chat_id = ?
                  AND m.user_id IS NOT NULL
                  AND (? IS NULL OR m.user_id != ?)
                  AND (m.username IS NOT NULL OR m.first_name IS NOT NULL
                       OR m.last_name IS NOT NULL)
                ORDER BY CASE WHEN m.username IS NOT NULL THEN 0 ELSE 1 END,
                         m.date DESC, m.id DESC
                LIMIT 1
            """, (connection_id, chat_id, owner_id, owner_id))
            identity = cursor.fetchone()
    else:
        cursor.execute("""
            SELECT title, username
            FROM group_chats
            WHERE chat_id = ?
        """, (chat_id,))
        group_row = cursor.fetchone()
        if group_row and group_row[0]:
            return group_row[0]
        if group_row and group_row[1]:
            return f"@{group_row[1]}"

        cursor.execute("""
            SELECT chat_username, chat_first_name, chat_last_name
            FROM messages
            WHERE chat_id = ?
              AND (chat_username IS NOT NULL OR chat_first_name IS NOT NULL
                   OR chat_last_name IS NOT NULL)
            ORDER BY CASE WHEN chat_username IS NOT NULL THEN 0 ELSE 1 END,
                     date DESC, id DESC
            LIMIT 1
        """, (chat_id,))
        identity = cursor.fetchone()

        if not identity:
            cursor.execute("""
                SELECT m.username, m.first_name, m.last_name
                FROM messages m
                LEFT JOIN business_connections b
                  ON b.connection_id = m.connection_id
                WHERE m.chat_id = ? AND m.user_id IS NOT NULL
                  AND (b.owner_id IS NULL OR m.user_id != b.owner_id)
                  AND (m.username IS NOT NULL OR m.first_name IS NOT NULL
                       OR m.last_name IS NOT NULL)
                ORDER BY CASE WHEN m.username IS NOT NULL THEN 0 ELSE 1 END,
                         m.date DESC, m.id DESC
                LIMIT 1
            """, (chat_id,))
            identity = cursor.fetchone()

    if identity:
        candidate = _display_person_name(
            identity[1], identity[2], identity[0], chat_id
        )
        if candidate and not candidate.startswith("ID "):
            return candidate

    candidate = _display_person_name(
        fallback_first_name, fallback_last_name,
        fallback_username, chat_id
    )
    return candidate


def _admin_log_participants(row):
    # Row columns are returned by _admin_log_select_sql().
    (
        db_id, connection_id, chat_id, message_id,
        user_id, username, first_name, last_name,
        chat_username, chat_first_name, chat_last_name,
        msg_text, date, media_type, file_id, deleted,
        owner_id, owner_username, owner_first_name, owner_last_name
    ) = row

    owner_name = _display_person_name(
        owner_first_name, owner_last_name, owner_username, owner_id
    )

    peer_name = _lookup_peer_name(
        connection_id=connection_id,
        chat_id=chat_id,
        owner_id=owner_id,
        fallback_username=chat_username or username,
        fallback_first_name=chat_first_name or first_name,
        fallback_last_name=chat_last_name or last_name,
    )

    sender_name = _display_person_name(
        first_name, last_name, username, user_id
    )

    if connection_id and str(connection_id).startswith("group:"):
        return sender_name, peer_name, peer_name, peer_name

    if owner_id and user_id == owner_id:
        sender, recipient = owner_name, peer_name
    elif user_id:
        # Incoming messages should be attributed to the customer.
        sender = sender_name if sender_name != f"ID {user_id}" else peer_name
        recipient = owner_name
    else:
        sender, recipient = peer_name, owner_name

    return sender, recipient, owner_name, peer_name


def _format_admin_log_row(row, index=None):
    (
        db_id, connection_id, chat_id, message_id,
        user_id, username, first_name, last_name,
        chat_username, chat_first_name, chat_last_name,
        msg_text, date, media_type, file_id, deleted,
        owner_id, owner_username, owner_first_name, owner_last_name
    ) = row

    sender, recipient, owner_name, peer_name = _admin_log_participants(row)

    try:
        time_text = moscow_time(datetime.fromisoformat(date))
    except Exception:
        time_text = str(date or "дата неизвестна")

    clean_text = (msg_text or "").strip()
    if not clean_text:
        clean_text = "Медиа без подписи" if media_type != "text" else "Сообщение без текста"

    lines = []
    prefix = f"#{db_id} — " if index is None else f"#{index} (ID {db_id}) — "
    lines.append(prefix + time_text)
    lines.append(f"{sender} → {recipient}")
    lines.append(f"Тип: {media_name(media_type)}")
    lines.append(f"Текст: {clean_text}")
    if deleted:
        lines.append("Статус: УДАЛЕНО")
    lines.append(f"Chat ID: {chat_id} | Message ID: {message_id}")
    return "\n".join(lines)


def _admin_log_sort_key(row):
    """Sort archived messages by Telegram timestamp, then by DB id."""

    try:
        message_date = datetime.fromisoformat(row[12])
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        timestamp = message_date.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        timestamp = 0

    return timestamp, row[0]


def build_admin_logs_text(limit=20, chat_id=None):
    query = _admin_log_select_sql()
    params = []
    if chat_id is not None:
        query += " WHERE m.chat_id = ?"
        params.append(chat_id)
    query += " ORDER BY julianday(m.date) DESC, m.id DESC LIMIT ?"
    params.append(limit)
    cursor.execute(query, params)
    rows = cursor.fetchall()

    if not rows:
        if chat_id is None:
            return "📋 <b>Последние логи переписки</b>\n\nПока нет сохранённых сообщений."
        label = _lookup_peer_name(None, chat_id) if chat_id is not None else ""
        return f"📋 <b>Логи: {safe(label)}</b>\n\nСообщений не найдено." if chat_id is not None else "📋 <b>Последние логи переписки</b>\n\nПока нет сохранённых сообщений."

    title = "📋 <b>Последние логи переписки</b>"
    if chat_id is not None:
        label = _lookup_peer_name(None, chat_id)
        title = f"📋 <b>Логи: {safe(label)}</b>"
    result = [title, "", f"Показано сообщений: {len(rows)}", ""]

    # Show one global timeline. Grouping by chat here made messages from
    # different conversations appear out of chronological order.
    for row in sorted(rows, key=_admin_log_sort_key):
        connection_id, row_chat_id = row[1], row[2]
        peer_name = _lookup_peer_name(connection_id, row_chat_id)
        dialog_label = "Группа" if str(connection_id).startswith("group:") else "Диалог"
        result.append(
            f"💬 <b>{dialog_label}: {safe(peer_name)}</b> · "
            f"<code>{row_chat_id}</code>"
        )
        result.append(safe(_format_admin_log_row(row)))
        result.append("────────────────────")

    text = "\n".join(result)
    # Telegram message length cap; keep latest tail if needed.
    if len(text) > 3900:
        text = "… (часть логов скрыта; скачай TXT для полного архива)\n\n" + text[-3750:]
    return text


def get_recent_admin_log_media(limit=20, chat_id=None):
    query = """
        SELECT id, media_type
        FROM messages
        WHERE media_type != 'text'
          AND file_id IS NOT NULL
    """
    params = []
    if chat_id is not None:
        query += " AND chat_id = ?"
        params.append(chat_id)
    query += " ORDER BY julianday(date) DESC, id DESC LIMIT ?"
    params.append(limit)
    cursor.execute(query, params)
    return cursor.fetchall()


def admin_logs_keyboard(log_ids=None, chat_id=None):
    refresh_data = "open_admin_logs" if chat_id is None else f"open_admin_logs_user:{chat_id}"
    download_data = "download_admin_logs" if chat_id is None else f"download_admin_logs_user:{chat_id}"
    rows = [
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=refresh_data)],
        [InlineKeyboardButton(text="👥 Фильтр по пользователю", callback_data="admin_logs_users")],
    ]

    if log_ids:
        for db_id, media_type in log_ids[:10]:
            rows.append([
                InlineKeyboardButton(
                    text=f"📎 Открыть медиа #{db_id}",
                    callback_data=f"open_log_media:{db_id}"
                )
            ])

    rows.extend([
        [InlineKeyboardButton(text="📥 Скачать TXT", callback_data=download_data)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="open_menu")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        logger.error('%s %s', "❌ ADMIN MEDIA ERROR:", repr(error))
        await callback.answer(
            "❌ Не удалось отправить медиа.",
            show_alert=True
        )



@dp.callback_query(
    lambda c: c.data == "open_admin_logs"
)
async def open_admin_logs(callback: CallbackQuery):
    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ только для администратора.", show_alert=True)
        return

    text = build_admin_logs_text(20)
    media_ids = get_recent_admin_log_media(20)
    await replace_menu(callback, text, admin_logs_keyboard(media_ids))


@dp.callback_query(
    lambda c: c.data == "admin_logs_users"
)
async def admin_logs_users(callback: CallbackQuery):
    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ только для администратора.", show_alert=True)
        return

    cursor.execute("""
        SELECT m.chat_id, COUNT(*) AS message_count,
               MAX(julianday(m.date)) AS last_activity,
               MAX(m.id) AS last_id
        FROM messages m
        GROUP BY m.chat_id
        ORDER BY last_activity DESC, last_id DESC
        LIMIT 50
    """)
    rows = cursor.fetchall()

    if not rows:
        await callback.answer("Пока нет сообщений для фильтра.", show_alert=True)
        return

    buttons = []
    for chat_id, count, _last_activity, last_id in rows:
        peer_name = _lookup_peer_name(
            connection_id=None,
            chat_id=chat_id,
        )
        if len(peer_name) > 32:
            peer_name = peer_name[:29] + "..."
        buttons.append([InlineKeyboardButton(
            text=f"👤 {peer_name} ({count})",
            callback_data=f"admin_logs_user:{chat_id}"
        )])

    buttons.append([InlineKeyboardButton(text="⬅️ К логам", callback_data="open_admin_logs")])
    await replace_menu(
        callback,
        "👥 <b>Фильтр логов по пользователю</b>\n\nВыбери собеседника. В скобках — количество сохранённых сообщений.",
        InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(
    lambda c: c.data.startswith("admin_logs_user:")
)
async def admin_logs_user(callback: CallbackQuery):
    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ только для администратора.", show_alert=True)
        return
    try:
        chat_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return

    text = build_admin_logs_text(20, chat_id=chat_id)
    media_ids = get_recent_admin_log_media(10, chat_id=chat_id)
    await replace_menu(callback, text, admin_logs_keyboard(media_ids, chat_id=chat_id))


@dp.callback_query(
    lambda c: c.data.startswith("open_admin_logs_user:")
)
async def refresh_admin_logs_user(callback: CallbackQuery):
    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ только для администратора.", show_alert=True)
        return
    try:
        chat_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    text = build_admin_logs_text(20, chat_id=chat_id)
    media_ids = get_recent_admin_log_media(10, chat_id=chat_id)
    await replace_menu(callback, text, admin_logs_keyboard(media_ids, chat_id=chat_id))


@dp.message(Command("logs"))
async def admin_logs_command(message: types.Message):
    if not ADMIN_ID or not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Команда доступна только администратору.")
        return
    if message.chat.type != "private":
        await message.answer("Открой личный чат с ботом и отправь /logs — архив не публикуется в группе.")
        return
    text = build_admin_logs_text(20)
    media_ids = get_recent_admin_log_media(20)
    await message.answer(text, parse_mode="HTML", reply_markup=admin_logs_keyboard(media_ids))


# =========================================================
# DOWNLOAD ALL ADMIN LOGS
# =========================================================

def build_admin_logs_file(chat_id=None):
    query = _admin_log_select_sql()
    params = []
    if chat_id is not None:
        query += " WHERE m.chat_id = ?"
        params.append(chat_id)
    query += " ORDER BY m.connection_id, m.chat_id, m.date ASC, m.id ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()

    if not rows:
        return None, 0

    lines = [
        "MESSAGE ARCHIVE — ЛОГИ ПЕРЕПИСКИ",
        f"Создано: {moscow_time()}",
        f"Количество сообщений: {len(rows)}",
        "Фильтр: все пользователи" if chat_id is None else f"Фильтр: пользователь Chat ID {chat_id}",
        "=" * 72,
        "",
    ]

    current_group = None
    for row in rows:
        _, connection_id, row_chat_id = row[0], row[1], row[2]
        group = (connection_id, row_chat_id)
        if group != current_group:
            current_group = group
            sender, recipient, owner_name, peer_name = _admin_log_participants(row)
            lines.extend([
                "",
                "#" * 72,
                f"ГРУППА: {peer_name}" if str(connection_id).startswith("group:")
                else f"ДИАЛОГ: {owner_name} ↔ {peer_name}",
                f"Chat ID: {row_chat_id}",
                f"Источник: {'группа' if str(connection_id).startswith('group:') else 'Business'} · {connection_id}",
                "#" * 72,
                "",
            ])
        lines.append(_format_admin_log_row(row))
        lines.append("-" * 72)
        lines.append("")

    return "\n".join(lines), len(rows)


async def _send_admin_logs_document(callback: CallbackQuery, chat_id_filter=None):
    if not ADMIN_ID or callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ только для администратора.", show_alert=True)
        return

    await callback.answer("⏳ Формирую TXT-файл...")
    content, count = build_admin_logs_file(chat_id=chat_id_filter)
    if content is None:
        await callback.message.answer("📋 По выбранному фильтру логов пока нет.")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    suffix = "all" if chat_id_filter is None else f"chat_{chat_id_filter}"
    filename = f"business_logs_{suffix}_{timestamp}.txt"
    document = BufferedInputFile(content.encode("utf-8"), filename=filename)

    await bot.send_document(
        chat_id=ADMIN_ID,
        document=document,
        caption=(
            "📋 <b>Экспорт логов переписки</b>\n\n"
            f"📨 Сообщений: <b>{count}</b>\n"
            f"📄 Формат: <code>TXT</code>\n"
            f"🔎 Фильтр: <code>{safe(suffix)}</code>"
        ),
        parse_mode="HTML"
    )
    logger.info('%s', f"📥 TXT LOG EXPORT OK: {count} rows -> {filename}")


@dp.callback_query(
    lambda c: c.data == "download_admin_logs"
)
async def download_admin_logs(callback: CallbackQuery):
    try:
        await _send_admin_logs_document(callback)
    except Exception as error:
        logger.error('%s %s', "❌ LOG TXT EXPORT ERROR:", repr(error))
        try:
            await callback.message.answer(
                "❌ <b>Не удалось сформировать TXT-логи.</b>\n\n"
                f"<code>{safe(error)}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass


@dp.callback_query(
    lambda c: c.data.startswith("download_admin_logs_user:")
)
async def download_admin_logs_user(callback: CallbackQuery):
    try:
        chat_id_filter = int(callback.data.split(":", 1)[1])
        await _send_admin_logs_document(callback, chat_id_filter=chat_id_filter)
    except (ValueError, IndexError):
        await callback.answer("Некорректный фильтр.", show_alert=True)
    except Exception as error:
        logger.error('%s %s', "❌ FILTERED TXT EXPORT ERROR:", repr(error))
        try:
            await callback.message.answer(
                "❌ <b>Не удалось сформировать TXT-логи.</b>\n\n"
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
        "↩️ <b>Автопересылка ответов</b>\n\n"
        "Ответь на медиа в личном Business-чате. "
        "Бот автоматически отправит его администратору. Обычные медиа тоже могут быть отправлены. "
        "Команду вводить не нужно."
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
                [InlineKeyboardButton(
                    text="⬅️ К командам",
                    callback_data="open_commands"
                )]
            ]
        )
    )


@dp.callback_query(lambda c: c.data == "command_flip_dice")
async def command_flip_dice_help(callback: CallbackQuery):
    await replace_menu(
        callback,
        "🪙 <b>.flip</b> — подбросить монетку\n"
        "🎲 <b>.dice</b> — бросить кубик.\n\n"
        "Работает в личном чате с ботом и в личном Business-чате от владельца аккаунта.",
        back_keyboard("open_commands", "⬅️ К командам")
    )


@dp.callback_query(lambda c: c.data == "command_games")
async def command_games_help(callback: CallbackQuery):
    await replace_menu(
        callback,
        "♟ <b>.chk</b> — пригласить игрока в шашки\n"
        "⭕ <b>.ttt</b> — крестики-нолики\n"
        "✊ <b>.rps</b> — камень, ножницы, бумага\n"
        "🔴 <b>.c4</b> — четыре в ряд.\n"
        "Игровые команды удаляются после обработки, приглашение и поле остаются. После партии нажмите «Реванш», чтобы начать следующую; первый ход перейдёт другому игроку.\n\n"
        "Отправь команду прямо в личном Business-чате с нужным соперником. Приглашение и общее игровое поле появятся в этой переписке. Собеседник принимает вызов кнопкой, затем вы ходите по очереди. В личном чате с ботом игровые команды удаляются молча.",
        back_keyboard("open_commands", "⬅️ К командам")
    )


@dp.callback_query(lambda c: c.data == "command_mute")
async def command_mute_help(callback: CallbackQuery):
    await replace_menu(
        callback,
        "🔇 <b>.mute</b> — приостановить архивирование Business-чата\n"
        "🔊 <b>.unmute</b> — возобновить архивирование.\n\n"
        "Отправь .mute в личном подключённом чате от своего аккаунта: новые сообщения собеседника будут удаляться. Для этого выдай боту право «Удалять все сообщения»: Настройки → Изм. → Автоматизация чатов → Message Archive Bot. Команды в группах не работают.",
        back_keyboard("open_commands", "⬅️ К командам")
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

        logger.error('%s %s', "❌ INVITE ERROR:", repr(error))


# =========================================================
# PRIVATE /SETTINGS
# =========================================================

@dp.message(Command("settings"))
async def private_settings_command(
    message: types.Message
):

    if message.chat.type != "private":
        await message.answer("Настройки доступны в личном чате с ботом.")
        return

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

    if message.chat.type != "private":
        await message.answer("Статистика доступна в личном чате с ботом.")
        return

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

    if message.chat.type != "private":
        await message.answer("Архив удалённых сообщений доступен в личном чате с ботом.")
        return

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


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_message_handler(message: types.Message):
    save_group_chat(message.chat)

    message_text = (message.text or "").strip()
    if message_text:
        command_parts = message_text.split(maxsplit=1)
        command = command_parts[0].split("@", 1)[0].lower()
        args = command_parts[1] if len(command_parts) > 1 else ""
        if command in {".flip", ".dice", ".ttt", ".chk", ".mute", ".unmute"}:
            return
        if await handle_group_spam_command(message, command, args):
            return

    license_owner = group_subscription_owner(message.chat.id)
    if not is_subscription_active(license_owner):
        await notify_subscription_required(license_owner)
        return

    connection_id = group_connection_id(message.chat.id)
    if message.from_user and message.from_user.is_bot:
        return

    user = message.from_user
    sender_chat = getattr(message, "sender_chat", None)
    user_id = user.id if user else None
    username = user.username if user else getattr(sender_chat, "username", None)
    first_name = user.first_name if user else getattr(sender_chat, "title", None)
    last_name = user.last_name if user else None
    text = message.text or message.caption or ""
    media_type, file_id = get_media_info(message)
    _, save_media, _ = get_settings(message.chat.id)
    if not save_media and media_type != "text":
        media_type, file_id = "text", None

    cursor.execute("""
        INSERT OR REPLACE INTO messages (
            connection_id, chat_id, message_id,
            user_id, username, first_name, last_name,
            chat_username, chat_first_name, chat_last_name,
            text, date, media_type, file_id, deleted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        connection_id, message.chat.id, message.message_id,
        user_id, username, first_name, last_name,
        getattr(message.chat, "username", None), None, None,
        text, message.date.isoformat(), media_type, file_id
    ))
    db.commit()
    logger.debug(
        "Group message archived: chat_id=%s message_id=%s media_type=%s user_id=%s",
        message.chat.id, message.message_id, media_type, user_id or "unknown"
    )

## =========================================================
# MAIN
# =========================================================

async def main():

    if not TOKEN:

        logger.error('❌ BOT_TOKEN не задан.')

        return



    logger.info('🚀 BUSINESS ARCHIVE ЗАПУЩЕН')



    # -----------------------------------------------------
    # FFmpeg
    # -----------------------------------------------------

    actual_ffmpeg = (
        FFMPEG_PATH
        if os.path.isfile(FFMPEG_PATH)
        else shutil.which(FFMPEG_PATH)
    )

    if actual_ffmpeg:
        logger.info("FFmpeg найден: %s", actual_ffmpeg)
    else:
        logger.warning("FFmpeg не найден; конвертация видео недоступна")

    # -----------------------------------------------------
    # ADMIN
    # -----------------------------------------------------

    logger.info("ADMIN_ID: %s", "задан" if ADMIN_ID else "не задан")
    logger.info(
        "Тариф архива: %s Telegram Stars / %s дней; продление вручную",
        SUBSCRIPTION_PRICE_STARS,
        SUBSCRIPTION_DAYS
    )

    # =====================================================
    # WEBHOOK
    # =====================================================
    #
    # Мы используем polling:
    #
    #     dp.start_polling(bot)
    #
    # Поэтому старый webhook необходимо удалить.
    #
    # drop_pending_updates=False
    # означает, что накопившиеся обновления НЕ удаляются.
    #
    # =====================================================

    try:

        webhook_info = await bot.get_webhook_info()

        logger.info("Webhook configured: %s", bool(webhook_info.url))

        logger.debug('%s %s', "📦 Pending updates:", webhook_info.pending_update_count)

        if webhook_info.url:

            logger.warning('⚠️ Обнаружен активный webhook.')

            logger.info('🗑 Удаляем webhook...')

            await bot.delete_webhook(
                drop_pending_updates=False
            )

            logger.info('✅ Webhook успешно удалён.')

        else:

            logger.info('✅ Webhook не установлен.')

    except Exception as error:

        logger.exception("Не удалось получить или обновить состояние webhook")

        return

    # =====================================================
    # START POLLING
    # =====================================================

    reminder_task = asyncio.create_task(subscription_expiry_reminder_supervisor())

    logger.info('📡 Запускаю polling...')

    try:

        logger.info('🗑 Удаляем webhook...')
        await bot.delete_webhook(drop_pending_updates=False)

        webhook = await bot.get_webhook_info()
        logger.info("Webhook configured after cleanup: %s", bool(webhook.url))

        logger.info('📡 Запускаю polling...')
        await dp.start_polling(bot)

    except TelegramNetworkError as error:

        logger.warning("Потеряно соединение с Telegram: %s", error)

    except Exception as error:

        logger.exception("Polling остановился из-за ошибки")

    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass



# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:


        logger.info('🛑 BUSINESS ARCHIVE ОСТАНОВЛЕН')
