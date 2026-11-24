import asyncio
import difflib
import html
import logging
import os
import random
import sqlite3
from io import StringIO
import uuid
import shutil
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from logging.handlers import RotatingFileHandler
import qrcode
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
                text="🚀 Подключить Business",
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

        logger.warning('%s %s', "⚠️ Не удалось удалить старый экран:", repr(error))

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

        logger.warning('%s %s', "🌐 Telegram network error:", repr(error))

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
async def forward_replied_message_to_replier(message: types.Message) -> bool:
    """Отправляет автору ответа медиа в его личный чат с ботом."""

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
            await create_business_game_invite(
                message,
                game_commands[command_text]
            )
            return
        if command_text == ".flip":
            await bot.send_message(
                chat_id=message.chat.id,
                business_connection_id=message.business_connection_id,
                text=random.choice(("🪙 Орёл!", "🪙 Решка!"))
            )
            return
        if command_text == ".dice":
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
                    "В настройках Telegram Business → Чат-боты включи для бота право "
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
                                "у бота нет права «Удалять все сообщения». Включи его в "
                                "Telegram Business → Чат-боты."
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
                                "Проверь, что у бота есть право «Удалять все сообщения» "
                                "в Telegram Business → Чат-боты."
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
            await forward_replied_message_to_replier(message)
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

    save_connection(
        connection,
        last_chat_id=message.chat.id
    )

    owner_id = connection.user.id
    user_chat_id = connection.user_chat_id

    if not is_subscription_active(owner_id):
        await notify_subscription_required(owner_id)
        return

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
    await message.answer(random.choice(("🪙 Орёл!", "🪙 Решка!")))


@dp.message(
    lambda message: (
        message.chat.type == "private"
        and isinstance(message.text, str)
        and message.text.strip().casefold() == ".dice"
    )
)
async def dice_command(message: types.Message):
    await bot.send_dice(chat_id=message.chat.id, emoji="🎲")


@dp.message(
    lambda message: (
        message.chat.type == "private"
        and isinstance(message.text, str)
        and message.text.strip().casefold() in {".ttt", ".chk", ".rps", ".c4"}
    )
)
async def business_game_help(message: types.Message):
    await message.answer(
        "Чтобы сыграть с собеседником, отправь команду прямо в личном Business-чате с ним. "
        "Приглашение и игровое поле появятся в этой переписке."
    )


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
        opponent_id: invite["opponent_label"]
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

        "🚀 <b>Подключить Business</b>\n\n"

        "Открой:\n"
        "<b>Настройки → Telegram Business → Чат-боты</b>\n\n"

        "Добавь этого бота и выдай права на чтение и отправку сообщений. "
        "Для команды .mute также включи право «Удалять все сообщения».\n\n"

        "После подключения архивирование "
        "начнёт работать автоматически.",

        back_keyboard()
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
        "🔴 <b>.c4</b> — четыре в ряд.\n\n"
        "Отправь команду прямо в личном Business-чате с нужным соперником. Приглашение и общее игровое поле появятся в этой переписке. Собеседник принимает вызов кнопкой, затем вы ходите по очереди. В личном чате с ботом команды подскажут, куда их отправить.",
        back_keyboard("open_commands", "⬅️ К командам")
    )


@dp.callback_query(lambda c: c.data == "command_mute")
async def command_mute_help(callback: CallbackQuery):
    await replace_menu(
        callback,
        "🔇 <b>.mute</b> — приостановить архивирование Business-чата\n"
        "🔊 <b>.unmute</b> — возобновить архивирование.\n\n"
        "Отправь .mute в личном Business-диалоге от своего аккаунта: новые сообщения собеседника будут удаляться. Для этого выдай боту право «Удалять все сообщения» в настройках Telegram Business → Чат-боты. Команды в группах не работают.",
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
