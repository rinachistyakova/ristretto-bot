print("RUNNING FILE: MAIN.PY V2 PROFILES + MATCHING + THANKS + ADMIN + RELIABILITY + COMMANDS")

import asyncio
import logging
import os
import random
from contextlib import suppress
from datetime import datetime, timedelta
from itertools import combinations
from zoneinfo import ZoneInfo

import asyncpg
from dotenv import load_dotenv

from v2_foundation import ensure_v2_foundation

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# =========================================================
# НАСТРОЙКИ
# =========================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL missing")

ADMIN_ID = 75734295
ADMIN_USER_IDS: set[int] = {ADMIN_ID}
TZ = ZoneInfo("Europe/Istanbul")

# Понедельник = 0, вторник = 1, среда = 2, пятница = 4
CHECKIN_WEEKDAY = 0
CHECKIN_HOUR = 10
MATCH_WEEKDAY = 1
MATCH_HOUR = 11
THANKS_REMINDER_WEEKDAY = 2
THANKS_REMINDER_HOUR = 11
THANKS_DIGEST_WEEKDAY = 4
THANKS_REVIEW_HOUR = 15
THANKS_DIGEST_HOUR = 16

# Safe outgoing pacing. This is deliberately conservative for a team bot.
OUTGOING_PACE_SECONDS = 0.09
RETRY_AFTER_MARGIN_SECONDS = 0.5
MAX_RETRY_AFTER_ATTEMPTS = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
pool: asyncpg.Pool | None = None
_send_lock = asyncio.Lock()
_last_send_monotonic = 0.0

ICEBREAKERS = [
    "Какой город идеально подходит для одного ristretto?",
    "Какой рабочий ритуал ты никому не отдашь?",
    "Какой проект дал неожиданный инсайт?",
    "Что в работе ты считаешь недооценённым?",
    "Какой внутренний мем должен жить вечно?",
    "Какой хороший рабочий разговор был у тебя недавно?",
    "Что сейчас в твоей сфере переоценено?",
    "Какой маленький ритуал помогает тебе работать?",
]

# =========================================================
# ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def refresh_admin_cache() -> None:
    global ADMIN_USER_IDS
    if pool is None:
        ADMIN_USER_IDS = {ADMIN_ID}
        return

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id
            FROM users
            WHERE role IN ('owner', 'admin')
              AND status='active'
              AND deleted_at IS NULL
              AND removed_by_admin=0
        """)
    ADMIN_USER_IDS = {ADMIN_ID, *(row["user_id"] for row in rows)}


USER_BOT_COMMANDS = [
    BotCommand(command="start", description="Открыть Random Ristretto"),
    BotCommand(command="thanks", description="Поблагодарить коллегу"),
    BotCommand(command="profile", description="Мой профиль"),
    BotCommand(command="blocked", description="Заблокированные отправители"),
    BotCommand(command="leave", description="Поставить рассылки на паузу"),
    BotCommand(command="resume", description="Вернуть автоматические сообщения"),
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="delete_me", description="Удалить профиль"),
]


async def configure_bot_commands() -> None:
    """Keep the Telegram command menu aligned with the user's role."""
    await bot.set_my_commands(
        USER_BOT_COMMANDS,
        scope=BotCommandScopeAllPrivateChats(),
    )
    admin_commands = USER_BOT_COMMANDS + [
        BotCommand(command="admin", description="Админка Random Ristretto"),
    ]
    for admin_id in ADMIN_USER_IDS:
        await bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id),
        )


def user_help_text(user: asyncpg.Record | dict | None) -> str:
    if not user:
        return "Сначала отправь /start — познакомимся и создадим профиль."

    if user["status"] == "access_denied":
        return access_denied_message()

    if user["status"] == "self_deleted" or user["deleted_at"] is not None:
        return "Профиль удалён. Если захочешь вернуться, отправь /start."

    if user["status"] == "pending_activation":
        if user["onboarding_step"] != "completed":
            return (
                "Сначала закончим профиль. Отправь /start — я продолжу с того шага, "
                "на котором мы остановились."
            )
        return "Профиль готов и ждёт активации администратора."

    intro = "Random Ristretto на паузе, но ручные команды доступны." if user["status"] == "paused" else "Random Ristretto"
    migration_note = (
        "\n\nЧтобы участвовать в Ristretto, нужно один раз дополнить профиль через /profile."
        if user["profile_migration_required"] == 1
        else ""
    )
    return (
        f"{intro}\n\n"
        "/thanks — поблагодарить коллегу\n"
        "/profile — посмотреть или изменить профиль\n"
        "/blocked — посмотреть и снять блокировки благодарностей\n"
        "/leave — поставить автоматические сообщения на паузу\n"
        "/resume — вернуть автоматические сообщения\n"
        "/help — показать эту памятку\n"
        "/delete_me — удалить профиль из бота"
        f"{migration_note}"
    )


def current_cycle(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(TZ)
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def next_cycle(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(TZ)
    return current_cycle(moment + timedelta(days=7))


def clean_name(value: str | None) -> str:
    value = (value or "").strip()
    return value if value else "Коллега"


TEAM_LABELS = {
    "sales": "Продажи",
    "operations": "Операции",
    "marketing": "Маркетинг",
    "other": "",
}


def team_label(team: str | None) -> str:
    return TEAM_LABELS.get(team or "", "")


def profile_is_complete(user: asyncpg.Record | dict) -> bool:
    return (
        bool(clean_name(user["display_name"]))
        and user["name_confirmed"] == 1
        and user["team"] in TEAM_LABELS
        and bool((user["responsibility_text"] or "").strip())
        and user["onboarding_step"] == "completed"
        and user["profile_migration_required"] == 0
    )


def profile_title(user: asyncpg.Record | dict) -> str:
    name = clean_name(user["display_name"])
    label = team_label(user["team"])
    return f"{name} · {label}" if label else name


def access_denied_message() -> str:
    return (
        "Сейчас не могу предоставить доступ к боту.\n\n"
        "Попробуйте обратиться к коллегам из отдела PR и коммуникаций."
    )


def contact_name(user: asyncpg.Record | dict) -> str:
    display_name = clean_name(user["display_name"])
    username = user["username"]
    if username:
        return f"{display_name} — @{username}"
    return display_name


def split_long_text(text: str, limit: int = 3500) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for line in text.splitlines():
        addition = len(line) + 1
        if current and current_length + addition > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_length = addition
        else:
            current.append(line)
            current_length += addition

    if current:
        chunks.append("\n".join(current))

    return chunks


async def require_admin(message: Message) -> bool:
    if is_admin(message.from_user.id):
        return True
    await message.answer("Эта команда доступна только администратору.")
    return False


async def require_owner(message: Message) -> bool:
    if is_owner(message.from_user.id):
        return True
    await message.answer("Эта команда доступна только владельцу бота.")
    return False

# =========================================================
# БАЗА ДАННЫХ
# =========================================================
async def init_db_pool() -> None:
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=60,
    )


async def init_db() -> None:
    assert pool is not None

    async with pool.acquire() as conn:
        # Старая таблица сохраняется. Данные не удаляются.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                active INT DEFAULT 1,
                confirmed INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Добавляем новые поля без удаления старых пользователей.
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS name_confirmed INT DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS confirmed_cycle TEXT")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS removed_by_admin INT DEFAULT 0")

        await conn.execute("""
            UPDATE users
            SET display_name = COALESCE(
                NULLIF(TRIM(first_name), ''),
                NULLIF(TRIM(username), ''),
                'Коллега'
            )
            WHERE display_name IS NULL OR TRIM(display_name) = ''
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id SERIAL PRIMARY KEY,
                user1 BIGINT,
                user2 BIGINT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_runs (
                job_name TEXT NOT NULL,
                cycle_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                error_text TEXT,
                PRIMARY KEY (job_name, cycle_key)
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_deliveries (
                job_name TEXT NOT NULL,
                cycle_key TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (job_name, cycle_key, user_id)
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_groups (
                id BIGSERIAL PRIMARY KEY,
                cycle_key TEXT NOT NULL,
                topic TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_group_members (
                group_id BIGINT NOT NULL REFERENCES match_groups(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                cycle_key TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id),
                UNIQUE (cycle_key, user_id)
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS match_notifications (
                group_id BIGINT NOT NULL REFERENCES match_groups(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (group_id, user_id)
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS thanks (
                id BIGSERIAL PRIMARY KEY,
                cycle_key TEXT NOT NULL,
                sender_id BIGINT NOT NULL,
                recipient_id BIGINT NOT NULL,
                sender_name TEXT NOT NULL,
                recipient_name TEXT NOT NULL,
                message_text TEXT NOT NULL,
                include_in_digest INT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                personal_delivered_at TIMESTAMPTZ,
                digest_delivered_at TIMESTAMPTZ
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_flows (
                user_id BIGINT PRIMARY KEY,
                flow_type TEXT NOT NULL,
                step TEXT NOT NULL,
                cycle_key TEXT,
                recipient_id BIGINT,
                draft_text TEXT,
                include_in_digest INT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(active)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_confirmed_cycle ON users(confirmed_cycle)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_match_groups_cycle ON match_groups(cycle_key)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_thanks_cycle ON thanks(cycle_key)")

        # Random Ristretto v2: additive foundation only.
        # This does not switch the current user-facing UX to v2 yet.
        await ensure_v2_foundation(conn, ADMIN_ID)
        await backfill_match_relationships(conn)

    await refresh_admin_cache()


async def get_user(user_id: int):
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def register_user(user_id: int, username: str | None, first_name: str | None):
    """
    Создаёт нового пользователя в pending_activation или обновляет Telegram-данные
    существующего пользователя. Старые активные пользователи не проходят повторную
    активацию, но должны один раз дополнить профиль v2.
    """
    assert pool is not None
    fallback_name = clean_name(first_name or username)

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM users WHERE user_id=$1",
            user_id,
        )

        if not existing:
            await conn.execute("""
                INSERT INTO users (
                    user_id, username, first_name, display_name,
                    name_confirmed, active, confirmed, confirmed_cycle,
                    removed_by_admin, deleted_at, created_at, updated_at,
                    team, responsibility_text, status, role,
                    onboarding_step, profile_migration_required,
                    last_interaction_at, delivery_available
                )
                VALUES (
                    $1, $2, $3, $4,
                    0, 0, 0, NULL,
                    0, NULL, NOW(), NOW(),
                    NULL, NULL, 'pending_activation', 'user',
                    'name', 0,
                    NOW(), 1
                )
            """, user_id, username, first_name, fallback_name)
            return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)

        # Старое админское удаление трактуем как закрытый доступ.
        if existing["removed_by_admin"] == 1:
            await conn.execute("""
                UPDATE users
                SET username=$2,
                    first_name=$3,
                    active=0,
                    status='access_denied',
                    deleted_at=NULL,
                    access_denied_reason=COALESCE(access_denied_reason, 'admin_revoked'),
                    access_denied_at=COALESCE(access_denied_at, NOW()),
                    last_interaction_at=NOW(),
                    delivery_available=1,
                    updated_at=NOW()
                WHERE user_id=$1
            """, user_id, username, first_name)
            return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)

        if existing["status"] == "access_denied":
            await conn.execute("""
                UPDATE users
                SET username=$2,
                    first_name=$3,
                    last_interaction_at=NOW(),
                    delivery_available=1,
                    updated_at=NOW()
                WHERE user_id=$1
            """, user_id, username, first_name)
            return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)

        # После самостоятельного удаления возвращение начинается как новая заявка:
        # профиль заполняется заново и снова требует активации админом.
        if existing["status"] == "self_deleted" or existing["deleted_at"] is not None:
            await conn.execute("""
                UPDATE users
                SET username=$2,
                    first_name=$3,
                    display_name=$4,
                    name_confirmed=0,
                    team=NULL,
                    responsibility_text=NULL,
                    active=0,
                    confirmed=0,
                    confirmed_cycle=NULL,
                    removed_by_admin=0,
                    deleted_at=NULL,
                    status='pending_activation',
                    role='user',
                    onboarding_step='name',
                    profile_migration_required=0,
                    activated_at=NULL,
                    access_denied_at=NULL,
                    access_denied_reason=NULL,
                    last_interaction_at=NOW(),
                    delivery_available=1,
                    updated_at=NOW()
                WHERE user_id=$1
            """, user_id, username, first_name, fallback_name)
            return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)

        await conn.execute("""
            UPDATE users
            SET username=$2,
                first_name=$3,
                last_interaction_at=NOW(),
                delivery_available=1,
                updated_at=NOW()
            WHERE user_id=$1
        """, user_id, username, first_name)

        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)


async def set_display_name(user_id: int, display_name: str) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET display_name=$1, name_confirmed=1, updated_at=NOW()
            WHERE user_id=$2 AND deleted_at IS NULL
        """, display_name, user_id)


async def set_profile_team(user_id: int, team: str) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET team=$1, updated_at=NOW()
            WHERE user_id=$2 AND deleted_at IS NULL
        """, team, user_id)


async def set_responsibility_text(user_id: int, responsibility_text: str) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET responsibility_text=$1, updated_at=NOW()
            WHERE user_id=$2 AND deleted_at IS NULL
        """, responsibility_text, user_id)


async def set_onboarding_step(user_id: int, step: str) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET onboarding_step=$1, updated_at=NOW()
            WHERE user_id=$2
        """, step, user_id)


async def complete_profile_setup(user_id: int) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET onboarding_step='completed',
                profile_migration_required=0,
                updated_at=NOW()
            WHERE user_id=$1
        """, user_id)


async def activate_pending_user(user_id: int) -> bool:
    assert pool is not None
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE users
            SET status='active',
                active=1,
                removed_by_admin=0,
                activated_at=NOW(),
                access_denied_at=NULL,
                access_denied_reason=NULL,
                updated_at=NOW()
            WHERE user_id=$1
              AND status='pending_activation'
              AND onboarding_step='completed'
              AND profile_migration_required=0
        """, user_id)
        return result.endswith("1")


async def deny_pending_user(user_id: int) -> bool:
    assert pool is not None
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE users
            SET status='access_denied',
                active=0,
                removed_by_admin=1,
                confirmed_cycle=NULL,
                access_denied_at=NOW(),
                access_denied_reason='activation_rejected',
                updated_at=NOW()
            WHERE user_id=$1
              AND status='pending_activation'
        """, user_id)
        return result.endswith("1")


async def get_pending_activation_users():
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT *
            FROM users
            WHERE status='pending_activation'
              AND onboarding_step='completed'
              AND profile_migration_required=0
            ORDER BY created_at, user_id
        """)


async def pause_user(user_id: int) -> bool:
    assert pool is not None
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE users
            SET active=0,
                status='paused',
                confirmed_cycle=NULL,
                updated_at=NOW()
            WHERE user_id=$1
              AND deleted_at IS NULL
              AND status='active'
        """, user_id)
        return result.endswith("1")


async def resume_user(user_id: int) -> str:
    user = await get_user(user_id)
    if not user:
        return "missing"
    if user["status"] == "access_denied" or user["removed_by_admin"] == 1:
        return "admin_removed"
    if user["status"] == "self_deleted" or user["deleted_at"] is not None:
        return "deleted"
    if user["status"] == "pending_activation":
        return "pending"

    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET active=1,
                status='active',
                last_interaction_at=NOW(),
                delivery_available=1,
                updated_at=NOW()
            WHERE user_id=$1
        """, user_id)
    return "ok"


async def anonymize_self(user_id: int) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM user_flows WHERE user_id=$1", user_id)
            await conn.execute("""
                UPDATE thanks
                SET sender_name='Удалённый пользователь'
                WHERE sender_id=$1
            """, user_id)
            await conn.execute("""
                UPDATE thanks
                SET recipient_name='Удалённый пользователь'
                WHERE recipient_id=$1
            """, user_id)
            await conn.execute("""
                UPDATE users
                SET username=NULL,
                    first_name=NULL,
                    display_name='Удалённый пользователь',
                    name_confirmed=0,
                    team=NULL,
                    responsibility_text=NULL,
                    active=0,
                    confirmed_cycle=NULL,
                    status='self_deleted',
                    role='user',
                    onboarding_step='completed',
                    profile_migration_required=0,
                    removed_by_admin=0,
                    deleted_at=NOW(),
                    updated_at=NOW()
                WHERE user_id=$1
            """, user_id)

    await refresh_admin_cache()
    try:
        await configure_bot_commands()
    except Exception:
        logging.exception("Не удалось обновить меню команд после удаления профиля")


async def remove_user_by_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False

    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM user_flows WHERE user_id=$1", user_id)
            result = await conn.execute("""
                UPDATE users
                SET active=0,
                    confirmed_cycle=NULL,
                    status='access_denied',
                    role='user',
                    removed_by_admin=1,
                    deleted_at=NULL,
                    access_denied_at=NOW(),
                    access_denied_reason='admin_revoked',
                    updated_at=NOW()
                WHERE user_id=$1
            """, user_id)

    changed = result.endswith("1")
    if changed:
        await refresh_admin_cache()
        try:
            await configure_bot_commands()
        except Exception:
            logging.exception("Не удалось обновить меню команд после закрытия доступа")
    return changed


async def deactivate_unreachable_user(user_id: int) -> None:
    # Недоступность Telegram не равна паузе или закрытию доступа.
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users
            SET delivery_available=0,
                last_delivery_error_at=NOW(),
                updated_at=NOW()
            WHERE user_id=$1
        """, user_id)


async def get_active_users():
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT user_id, username, first_name, display_name, name_confirmed,
                   active, deleted_at, removed_by_admin, status, team,
                   responsibility_text, profile_migration_required, onboarding_step
            FROM users
            WHERE active=1
              AND status='active'
              AND deleted_at IS NULL
              AND removed_by_admin=0
            ORDER BY LOWER(COALESCE(display_name, first_name, username, ''))
        """)


async def get_visible_users_for_admin():
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT user_id, username, display_name, active, name_confirmed,
                   deleted_at, removed_by_admin, status, team,
                   responsibility_text, profile_migration_required, onboarding_step
            FROM users
            WHERE status <> 'self_deleted'
            ORDER BY LOWER(COALESCE(display_name, username, ''))
        """)


async def record_weekly_invite(user_id: int, cycle_key: str) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO weekly_participation (
                user_id, cycle_key, invited_at, response, responded_at
            )
            VALUES ($1, $2, NOW(), NULL, NULL)
            ON CONFLICT (user_id, cycle_key)
            DO UPDATE SET invited_at=COALESCE(weekly_participation.invited_at, EXCLUDED.invited_at)
        """, user_id, cycle_key)


async def get_weekly_response(user_id: int, cycle_key: str) -> str | None:
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            SELECT response
            FROM weekly_participation
            WHERE user_id=$1 AND cycle_key=$2
        """, user_id, cycle_key)


async def set_weekly_response(user_id: int, cycle_key: str, response: str) -> bool:
    if response not in {"yes", "no"}:
        raise ValueError("Unknown weekly participation response")

    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            eligible = await conn.fetchval("""
                SELECT 1
                FROM users
                WHERE user_id=$1
                  AND active=1
                  AND status='active'
                  AND deleted_at IS NULL
                  AND removed_by_admin=0
                  AND profile_migration_required=0
                  AND onboarding_step='completed'
            """, user_id)
            if not eligible:
                return False

            await conn.execute("""
                INSERT INTO weekly_participation (
                    user_id, cycle_key, invited_at, response, responded_at
                )
                VALUES ($1, $2, NULL, $3, NOW())
                ON CONFLICT (user_id, cycle_key)
                DO UPDATE SET
                    response=EXCLUDED.response,
                    responded_at=NOW()
            """, user_id, cycle_key, response)

            # Keep the legacy field in sync while old admin statistics still use it.
            await conn.execute("""
                UPDATE users
                SET confirmed_cycle=$1, updated_at=NOW()
                WHERE user_id=$2
            """, cycle_key if response == "yes" else None, user_id)

    return True


async def set_confirmed_cycle(user_id: int, cycle_key: str | None) -> None:
    """Compatibility wrapper for code that still references the legacy field."""
    if cycle_key is None:
        assert pool is not None
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE users
                SET confirmed_cycle=NULL, updated_at=NOW()
                WHERE user_id=$1
            """, user_id)
        return

    await set_weekly_response(user_id, cycle_key, "yes")


async def get_confirmed_users(cycle_key: str):
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT u.user_id, u.username, u.first_name, u.display_name,
                   u.team, u.responsibility_text
            FROM users u
            JOIN weekly_participation wp
              ON wp.user_id=u.user_id
             AND wp.cycle_key=$1
             AND wp.response='yes'
            WHERE u.active=1
              AND u.status='active'
              AND u.deleted_at IS NULL
              AND u.removed_by_admin=0
              AND u.profile_migration_required=0
              AND u.onboarding_step='completed'
            ORDER BY u.user_id
        """, cycle_key)


# =========================================================
# СОСТОЯНИЯ ДИАЛОГА В POSTGRES
# =========================================================
async def set_flow(
    user_id: int,
    flow_type: str,
    step: str,
    cycle_key: str | None = None,
    recipient_id: int | None = None,
    draft_text: str | None = None,
    include_in_digest: int | None = None,
) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_flows (
                user_id, flow_type, step, cycle_key, recipient_id,
                draft_text, include_in_digest, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
                flow_type=EXCLUDED.flow_type,
                step=EXCLUDED.step,
                cycle_key=EXCLUDED.cycle_key,
                recipient_id=EXCLUDED.recipient_id,
                draft_text=EXCLUDED.draft_text,
                include_in_digest=EXCLUDED.include_in_digest,
                updated_at=NOW()
        """, user_id, flow_type, step, cycle_key, recipient_id, draft_text, include_in_digest)


async def get_flow(user_id: int):
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM user_flows WHERE user_id=$1", user_id)


async def clear_flow(user_id: int) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM user_flows WHERE user_id=$1", user_id)

# =========================================================
# НАДЁЖНАЯ ОТПРАВКА В TELEGRAM
# =========================================================
async def safe_send_message(chat_id: int, text: str, **kwargs):
    """
    Serializes proactive sends, keeps a small delay between them and respects
    Telegram RetryAfter without turning a transient flood-limit into a failed job.
    TelegramForbiddenError is intentionally not swallowed: callers already mark
    that user's delivery channel as unavailable.
    """
    global _last_send_monotonic

    for attempt in range(MAX_RETRY_AFTER_ATTEMPTS + 1):
        try:
            async with _send_lock:
                loop = asyncio.get_running_loop()
                wait_for = OUTGOING_PACE_SECONDS - (loop.time() - _last_send_monotonic)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)

                result = await bot.send_message(chat_id, text, **kwargs)
                _last_send_monotonic = loop.time()
                return result

        except TelegramRetryAfter as exc:
            if attempt >= MAX_RETRY_AFTER_ATTEMPTS:
                raise
            delay = max(float(exc.retry_after), 0.0) + RETRY_AFTER_MARGIN_SECONDS
            logging.warning(
                "Telegram flood limit for chat %s; retry in %.1f sec",
                chat_id,
                delay,
            )
            await asyncio.sleep(delay)


# =========================================================
# ЗАЩИТА ПЛАНИРОВЩИКА ОТ ПОВТОРОВ
# =========================================================
async def claim_job(job_name: str, cycle_key: str) -> bool:
    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO scheduler_runs (
                job_name, cycle_key, status, started_at, completed_at, error_text
            )
            VALUES ($1, $2, 'running', NOW(), NULL, NULL)
            ON CONFLICT (job_name, cycle_key)
            DO UPDATE SET
                status='running',
                started_at=NOW(),
                completed_at=NULL,
                error_text=NULL
            WHERE scheduler_runs.status='failed'
               OR (
                    scheduler_runs.status='running'
                    AND scheduler_runs.started_at < NOW() - INTERVAL '30 minutes'
               )
            RETURNING job_name
        """, job_name, cycle_key)
        return row is not None


async def finish_job(job_name: str, cycle_key: str) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE scheduler_runs
            SET status='completed', completed_at=NOW(), error_text=NULL
            WHERE job_name=$1 AND cycle_key=$2
        """, job_name, cycle_key)


async def fail_job(job_name: str, cycle_key: str, error_text: str) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE scheduler_runs
            SET status='failed', error_text=$3
            WHERE job_name=$1 AND cycle_key=$2
        """, job_name, cycle_key, error_text[:1000])


async def get_job_status(job_name: str, cycle_key: str) -> str | None:
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            SELECT status FROM scheduler_runs
            WHERE job_name=$1 AND cycle_key=$2
        """, job_name, cycle_key)


async def run_job_once(job_name: str, cycle_key: str, action) -> bool:
    claimed = await claim_job(job_name, cycle_key)
    if not claimed:
        return False

    try:
        await action()
        await finish_job(job_name, cycle_key)
        return True
    except Exception as exc:
        await fail_job(job_name, cycle_key, str(exc))
        logging.exception("Ошибка задачи %s за %s", job_name, cycle_key)
        raise

# =========================================================
# НАДЁЖНЫЕ РАССЫЛКИ
# =========================================================
async def mark_broadcast_delivered(job_name: str, cycle_key: str, user_id: int) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO broadcast_deliveries (job_name, cycle_key, user_id, delivered_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (job_name, cycle_key, user_id) DO NOTHING
        """, job_name, cycle_key, user_id)


async def get_pending_broadcast_users(job_name: str, cycle_key: str):
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT u.user_id, u.username, u.first_name, u.display_name
            FROM users u
            WHERE u.active=1
              AND u.status='active'
              AND u.deleted_at IS NULL
              AND u.removed_by_admin=0
              AND u.delivery_available=1
              AND (
                  $1 NOT LIKE 'random_checkin%'
                  OR (
                      u.profile_migration_required=0
                      AND u.onboarding_step='completed'
                  )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM broadcast_deliveries d
                  WHERE d.job_name=$1
                    AND d.cycle_key=$2
                    AND d.user_id=u.user_id
              )
            ORDER BY u.user_id
        """, job_name, cycle_key)


async def send_broadcast(
    job_name: str,
    cycle_key: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    users = await get_pending_broadcast_users(job_name, cycle_key)
    failures = 0

    for user in users:
        try:
            await safe_send_message(
                user["user_id"],
                text,
                reply_markup=reply_markup,
            )
            await mark_broadcast_delivered(job_name, cycle_key, user["user_id"])
        except TelegramForbiddenError:
            logging.warning("Пользователь %s заблокировал бота", user["user_id"])
            await deactivate_unreachable_user(user["user_id"])
        except Exception:
            failures += 1
            logging.exception("Не удалось отправить сообщение пользователю %s", user["user_id"])

    if failures:
        raise RuntimeError(f"Не доставлено сообщений: {failures}")


async def send_broadcast_chunks(
    job_name: str,
    cycle_key: str,
    chunks: list[str],
) -> None:
    users = await get_pending_broadcast_users(job_name, cycle_key)
    failures = 0

    for user in users:
        try:
            for chunk in chunks:
                await safe_send_message(user["user_id"], chunk)
            await mark_broadcast_delivered(job_name, cycle_key, user["user_id"])
        except TelegramForbiddenError:
            logging.warning("Пользователь %s заблокировал бота", user["user_id"])
            await deactivate_unreachable_user(user["user_id"])
        except Exception:
            failures += 1
            logging.exception("Не удалось отправить дайджест пользователю %s", user["user_id"])

    if failures:
        raise RuntimeError(f"Не доставлено дайджестов: {failures}")

# =========================================================
# КЛАВИАТУРЫ
# =========================================================
def checkin_keyboard(cycle_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да",
                    callback_data=f"rr_yes:{cycle_key}",
                ),
                InlineKeyboardButton(
                    text="Пропустить неделю",
                    callback_data=f"rr_no:{cycle_key}",
                ),
            ]
        ]
    )


def thanks_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Поблагодарить коллегу", callback_data="thanks_start")]
        ]
    )


def name_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Указать имя", callback_data="name_start")]
        ]
    )


def profile_migration_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обновить профиль", callback_data="profile_migration_start")]
        ]
    )


def profile_name_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оставить это имя", callback_data="profile_keep_name")],
            [InlineKeyboardButton(text="Изменить имя", callback_data="profile_change_name")],
        ]
    )


def profile_team_keyboard(mode: str = "onboarding") -> InlineKeyboardMarkup:
    prefix = "profile_team_edit" if mode == "edit" else "profile_team"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продажи", callback_data=f"{prefix}:sales")],
            [InlineKeyboardButton(text="Операции", callback_data=f"{prefix}:operations")],
            [InlineKeyboardButton(text="Маркетинг", callback_data=f"{prefix}:marketing")],
            [InlineKeyboardButton(
                text="Не отношусь ни к одной из этих команд",
                callback_data=f"{prefix}:other",
            )],
        ]
    )


def profile_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить имя", callback_data="profile_edit_name")],
            [InlineKeyboardButton(text="Изменить команду", callback_data="profile_edit_team")],
            [InlineKeyboardButton(text="Изменить описание", callback_data="profile_edit_responsibility")],
        ]
    )


def pending_activation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Активировать", callback_data=f"admin_activate:{user_id}")],
            [InlineKeyboardButton(text="Не активировать", callback_data=f"admin_deny_activation:{user_id}")],
        ]
    )


def delete_me_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, удалить мои данные", callback_data="delete_me_confirm")],
            [InlineKeyboardButton(text="Отмена", callback_data="delete_me_cancel")],
        ]
    )


# =========================================================
# ПРОФИЛИ И ОНБОРДИНГ V2
# =========================================================
async def send_profile_step(message: Message, user: asyncpg.Record) -> None:
    step = user["onboarding_step"]

    if step == "name":
        if user["status"] == "pending_activation":
            await message.answer(
                "Привет. Это Random Ristretto.\n\n"
                "Это как Random Coffee, только короче и концентрированнее: "
                "раз в неделю я знакомлю тебя с кем-то из коллег для короткого разговора.\n\n"
                "Ещё здесь можно отправлять благодарности коллегам — лично или включать их "
                "в пятничный дайджест.\n\n"
                "Для начала давай познакомимся.\n\n"
                "Шаг 1 из 3:\n"
                "Как тебя зовут? Напиши имя и фамилию."
            )
        else:
            await message.answer(
                "Шаг 1 из 3:\n"
                "Как тебя зовут? Напиши имя и фамилию."
            )
        return

    if step == "name_confirm":
        await message.answer(
            f"Шаг 1 из 3:\n"
            f"Сейчас твоё имя в боте: {clean_name(user['display_name'])}\n\n"
            "Оставляем его или меняем?",
            reply_markup=profile_name_confirm_keyboard(),
        )
        return

    if step == "team":
        await message.answer(
            "Шаг 2 из 3:\n"
            "В какой ты команде?",
            reply_markup=profile_team_keyboard(),
        )
        return

    if step == "responsibility":
        await message.answer(
            "Шаг 3 из 3:\n"
            "Напиши коротко, за что ты отвечаешь.\n\n"
            "Это поможет человеку перед Ristretto примерно понять, чем ты занимаешься.\n\n"
            "Здесь поместятся два коротких предложения, «Война и мир» — нет 🤍"
        )
        return

    if step == "completed" and user["status"] == "pending_activation":
        await message.answer(
            "Профиль готов.\n"
            "Скоро админ активирует твой профиль — и ты в Ristretto."
        )


async def show_profile(message: Message, user: asyncpg.Record) -> None:
    username = f"@{user['username']}" if user["username"] else "Telegram username не указан"
    responsibility = (user["responsibility_text"] or "").strip() or "не заполнено"

    await message.answer(
        "Твой профиль\n\n"
        f"{profile_title(user)}\n"
        f"Отвечает за: {responsibility}\n"
        f"{username}",
        reply_markup=profile_actions_keyboard(),
    )


async def notify_owner_pending_activation(user_id: int) -> None:
    user = await get_user(user_id)
    if not user:
        return

    username = f"@{user['username']}" if user["username"] else "username отсутствует"
    team = team_label(user["team"]) or "Другая команда"

    text = (
        "Новый профиль ждёт активации\n\n"
        f"{clean_name(user['display_name'])}\n"
        f"Команда: {team}\n"
        f"Отвечает за: {(user['responsibility_text'] or '').strip()}\n"
        f"Telegram: {username}"
    )

    try:
        await safe_send_message(
            ADMIN_ID,
            text,
            reply_markup=pending_activation_keyboard(user_id),
        )
    except Exception:
        logging.exception("Не удалось отправить админу новый профиль на активацию")


async def maybe_offer_current_week_after_profile_update(user_id: int) -> None:
    user = await get_user(user_id)
    if not user or user["status"] != "active" or user["active"] != 1:
        return

    now = datetime.now(TZ)
    before_tuesday_0001 = (
        now.weekday() == 0
        or (now.weekday() == 1 and now.hour == 0 and now.minute == 0)
    )
    if not before_tuesday_0001:
        return

    cycle_key = current_cycle(now)
    if await get_weekly_response(user_id, cycle_key) is not None:
        return

    match_status = await get_job_status("random_match", cycle_key)
    if match_status is not None:
        return

    try:
        await safe_send_message(
            user_id,
            "Готов(а) к Random Ristretto на этой неделе?",
            reply_markup=checkin_keyboard(cycle_key),
        )
        await record_weekly_invite(user_id, cycle_key)
        await mark_broadcast_delivered("random_checkin", cycle_key, user_id)
    except TelegramForbiddenError:
        await deactivate_unreachable_user(user_id)
    except Exception:
        logging.exception("Не удалось отправить текущий check-in после обновления профиля")


async def finish_profile_after_responsibility(message: Message, user_id: int, text: str) -> None:
    user_before = await get_user(user_id)
    if not user_before:
        return

    was_migration = user_before["profile_migration_required"] == 1

    await set_responsibility_text(user_id, text)
    await complete_profile_setup(user_id)
    user = await get_user(user_id)

    if not user:
        return

    if user["status"] == "pending_activation":
        if user_before["access_denied_reason"] == "admin_restore_pending":
            activated = await activate_pending_user(user_id)
            if activated:
                await message.answer(
                    "Профиль обновлён. Доступ снова открыт.\n\n"
                    "По понедельникам я буду спрашивать про Ristretto, "
                    "по средам — напоминать про благодарности."
                )
                return

        await message.answer(
            "Профиль готов.\n"
            "Скоро админ активирует твой профиль — и ты в Ristretto."
        )
        await notify_owner_pending_activation(user_id)
        return

    if was_migration:
        await message.answer("Профиль обновлён.")
        await maybe_offer_current_week_after_profile_update(user_id)
        return

    await show_profile(message, user)


@router.message(Command("start"))
async def start(message: Message) -> None:
    user = await register_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    if user["status"] == "access_denied":
        await message.answer(access_denied_message())
        return

    if user["status"] == "pending_activation":
        await send_profile_step(message, user)
        return

    if user["profile_migration_required"] == 1:
        if user["onboarding_step"] == "completed":
            await message.answer(
                "Random Ristretto немного подрос.\n\n"
                "Нас становится больше, поэтому теперь перед встречей я буду показывать "
                "не только имя коллеги, но и команду и пару слов о том, чем человек занимается.\n\n"
                "Нужно один раз дополнить профиль. Всего три коротких шага.",
                reply_markup=profile_migration_start_keyboard(),
            )
        else:
            await send_profile_step(message, user)
        return

    await message.answer(user_help_text(user))


@router.callback_query(F.data == "profile_migration_start")
async def profile_migration_start_callback(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["profile_migration_required"] != 1:
        await callback.answer("Профиль уже обновлён.", show_alert=True)
        return

    await set_onboarding_step(callback.from_user.id, "name_confirm")
    user = await get_user(callback.from_user.id)
    await send_profile_step(callback.message, user)
    await callback.answer()


@router.callback_query(F.data == "profile_keep_name")
async def profile_keep_name_callback(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if (
        not user
        or user["profile_migration_required"] != 1
        or user["onboarding_step"] != "name_confirm"
    ):
        await callback.answer("Этот шаг уже завершён.", show_alert=True)
        return

    await set_display_name(callback.from_user.id, clean_name(user["display_name"]))
    await set_onboarding_step(callback.from_user.id, "team")
    user = await get_user(callback.from_user.id)
    await send_profile_step(callback.message, user)
    await callback.answer()


@router.callback_query(F.data == "profile_change_name")
async def profile_change_name_callback(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["profile_migration_required"] != 1:
        await callback.answer("Профиль уже обновлён.", show_alert=True)
        return

    await set_onboarding_step(callback.from_user.id, "name")
    user = await get_user(callback.from_user.id)
    await send_profile_step(callback.message, user)
    await callback.answer()


@router.callback_query(F.data.startswith("profile_team:"))
async def profile_team_callback(callback: CallbackQuery) -> None:
    team = callback.data.split(":", 1)[1]
    if team not in TEAM_LABELS:
        await callback.answer("Неизвестная команда.", show_alert=True)
        return

    user = await get_user(callback.from_user.id)
    if (
        not user
        or user["onboarding_step"] != "team"
        or (
            user["status"] != "pending_activation"
            and user["profile_migration_required"] != 1
        )
    ):
        await callback.answer("Этот шаг уже завершён.", show_alert=True)
        return

    await set_profile_team(callback.from_user.id, team)
    await set_onboarding_step(callback.from_user.id, "responsibility")
    user = await get_user(callback.from_user.id)
    await send_profile_step(callback.message, user)
    await callback.answer()


@router.message(Command("profile"))
async def profile_command(message: Message) -> None:
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала отправь /start.")
        return

    if user["status"] == "access_denied":
        await message.answer(access_denied_message())
        return

    if user["status"] == "self_deleted":
        await message.answer("Профиль удалён. Чтобы вернуться, отправь /start.")
        return

    if user["status"] == "pending_activation" and user["onboarding_step"] != "completed":
        await send_profile_step(message, user)
        return

    if user["profile_migration_required"] == 1:
        await message.answer(
            "Сначала нужно один раз дополнить профиль.",
            reply_markup=profile_migration_start_keyboard(),
        )
        return

    await show_profile(message, user)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    user = await get_user(message.from_user.id)
    await message.answer(user_help_text(user))


@router.message(Command("name"))
async def name_command(message: Message) -> None:
    user = await get_user(message.from_user.id)
    if not user or user["status"] in {"self_deleted", "access_denied"}:
        await message.answer("Сначала отправь /start.")
        return

    await set_flow(message.from_user.id, "profile_edit", "waiting_name")
    await message.answer("Напиши новое имя и фамилию одним сообщением.")


@router.callback_query(F.data == "profile_edit_name")
async def profile_edit_name_callback(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["status"] in {"self_deleted", "access_denied"}:
        await callback.answer("Профиль недоступен.", show_alert=True)
        return

    await set_flow(callback.from_user.id, "profile_edit", "waiting_name")
    await callback.message.answer("Напиши новое имя и фамилию одним сообщением.")
    await callback.answer()


@router.callback_query(F.data == "profile_edit_team")
async def profile_edit_team_callback(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["status"] in {"self_deleted", "access_denied"}:
        await callback.answer("Профиль недоступен.", show_alert=True)
        return

    await callback.message.answer(
        "Выбери команду:",
        reply_markup=profile_team_keyboard(mode="edit"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("profile_team_edit:"))
async def profile_team_edit_value_callback(callback: CallbackQuery) -> None:
    team = callback.data.split(":", 1)[1]
    if team not in TEAM_LABELS:
        await callback.answer("Неизвестная команда.", show_alert=True)
        return

    user = await get_user(callback.from_user.id)
    if not user or user["status"] in {"self_deleted", "access_denied"}:
        await callback.answer("Профиль недоступен.", show_alert=True)
        return

    await set_profile_team(callback.from_user.id, team)
    user = await get_user(callback.from_user.id)
    await callback.message.answer("Команда обновлена.")
    await show_profile(callback.message, user)
    await callback.answer()


@router.callback_query(F.data == "profile_edit_responsibility")
async def profile_edit_responsibility_callback(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user or user["status"] in {"self_deleted", "access_denied"}:
        await callback.answer("Профиль недоступен.", show_alert=True)
        return

    await set_flow(callback.from_user.id, "profile_edit", "waiting_responsibility")
    await callback.message.answer(
        "Напиши коротко, за что ты отвечаешь. Максимум 150 знаков."
    )
    await callback.answer()


@router.message(Command("pending"))
async def pending_activation_command(message: Message) -> None:
    if not await require_admin(message):
        return

    users = await get_pending_activation_users()
    if not users:
        await message.answer("Сейчас никто не ждёт активации.")
        return

    for user in users:
        username = f"@{user['username']}" if user["username"] else "username отсутствует"
        team = team_label(user["team"]) or "Другая команда"
        await message.answer(
            "Профиль ждёт активации\n\n"
            f"{clean_name(user['display_name'])}\n"
            f"Команда: {team}\n"
            f"Отвечает за: {(user['responsibility_text'] or '').strip()}\n"
            f"Telegram: {username}",
            reply_markup=pending_activation_keyboard(user["user_id"]),
        )


@router.callback_query(F.data.startswith("admin_activate:"))
async def admin_activate_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":", 1)[1])
    user = await get_user(user_id)
    if not user:
        await callback.answer("Профиль не найден.", show_alert=True)
        return

    activated = await activate_pending_user(user_id)
    if not activated:
        await callback.answer("Профиль уже обработан.", show_alert=True)
        return

    await audit_admin_action(
        callback.from_user.id,
        "activate_user",
        target_user_id=user_id,
        object_type="user",
        object_id=user_id,
    )

    await callback.message.edit_text(
        f"Доступ открыт: {clean_name(user['display_name'])}"
    )

    try:
        await safe_send_message(
            user_id,
            "Готово, доступ открыт.\n\n"
            "По понедельникам я буду спрашивать, готов(а) ли ты к Ristretto на этой неделе.\n"
            "По средам — напоминать, что можно отправить кому-нибудь спасибо.\n\n"
            "Команды: /thanks, /profile, /blocked, /leave, /resume, /help"
        )
    except Exception:
        logging.exception("Не удалось сообщить пользователю об активации")

    await callback.answer()


@router.callback_query(F.data.startswith("admin_deny_activation:"))
async def admin_deny_activation_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":", 1)[1])
    user = await get_user(user_id)
    if not user:
        await callback.answer("Профиль не найден.", show_alert=True)
        return

    denied = await deny_pending_user(user_id)
    if not denied:
        await callback.answer("Профиль уже обработан.", show_alert=True)
        return

    await audit_admin_action(
        callback.from_user.id,
        "deny_activation",
        target_user_id=user_id,
        object_type="user",
        object_id=user_id,
    )

    await callback.message.edit_text(
        f"Доступ не открыт: {clean_name(user['display_name'])}"
    )

    try:
        await safe_send_message(user_id, access_denied_message())
    except Exception:
        logging.exception("Не удалось сообщить пользователю об отказе в доступе")

    await callback.answer()


# =========================================================
# ПАУЗА, ВОЗВРАТ И УДАЛЕНИЕ СВОИХ ДАННЫХ
# =========================================================
@router.message(Command("leave"))
async def leave(message: Message) -> None:
    changed = await pause_user(message.from_user.id)
    if changed:
        await message.answer(
            "Бот на паузе. Автоматические сообщения приходить не будут.\n\n"
            "При этом можно вручную пользоваться /thanks, /profile и другими командами.\n"
            "Чтобы вернуться к рассылкам, отправь /resume."
        )
    else:
        user = await get_user(message.from_user.id)
        if user and user["status"] == "paused":
            await message.answer("Бот уже на паузе. Чтобы вернуться, отправь /resume.")
        elif user and user["status"] == "access_denied":
            await message.answer(access_denied_message())
        else:
            await message.answer("Профиль не найден или пока не активирован. Отправь /start.")


@router.message(Command("resume"))
async def resume(message: Message) -> None:
    result = await resume_user(message.from_user.id)

    if result == "ok":
        await message.answer("Готово, автоматические сообщения снова включены.")
    elif result == "admin_removed":
        await message.answer(access_denied_message())
    elif result == "deleted":
        await message.answer("Профиль удалён. Чтобы вернуться, отправь /start.")
    elif result == "pending":
        await message.answer("Профиль пока ждёт активации администратора.")
    else:
        await message.answer("Профиль не найден. Отправь /start.")


@router.message(Command("delete_me"))
async def delete_me(message: Message) -> None:
    user = await get_user(message.from_user.id)
    if (
        not user
        or user["status"] in {"self_deleted", "access_denied"}
        or user["deleted_at"] is not None
    ):
        await message.answer("Активный профиль не найден.")
        return

    await message.answer(
        "Удалить профиль из бота?\n\n"
        "Ты перестанешь получать рассылки и участвовать в мэтчинге. "
        "Если захочешь вернуться позже, профиль нужно будет заполнить заново "
        "и снова активировать у администратора.",
        reply_markup=delete_me_keyboard(),
    )


@router.callback_query(F.data == "delete_me_confirm")
async def delete_me_confirm(callback: CallbackQuery) -> None:
    await anonymize_self(callback.from_user.id)
    await callback.message.edit_text(
        "Профиль удалён. Если захочешь вернуться, просто отправь /start."
    )
    await callback.answer()


@router.callback_query(F.data == "delete_me_cancel")
async def delete_me_cancel(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Удаление отменено.")
    await callback.answer()


# =========================================================
# RANDOM RISTRETTO: CHECK-IN
# =========================================================
async def send_weekly_checkin(cycle_key: str, delivery_job: str = "random_checkin") -> None:
    users = await get_pending_broadcast_users(delivery_job, cycle_key)
    failures = 0

    for user in users:
        try:
            await safe_send_message(
                user["user_id"],
                "Готов(а) к Random Ristretto на этой неделе?",
                reply_markup=checkin_keyboard(cycle_key),
            )
            await record_weekly_invite(user["user_id"], cycle_key)
            await mark_broadcast_delivered(delivery_job, cycle_key, user["user_id"])
        except TelegramForbiddenError:
            logging.warning("Пользователь %s заблокировал бота", user["user_id"])
            await deactivate_unreachable_user(user["user_id"])
        except Exception:
            failures += 1
            logging.exception("Не удалось отправить check-in пользователю %s", user["user_id"])

    if failures:
        raise RuntimeError(f"Не доставлено check-in сообщений: {failures}")


@router.callback_query(F.data.startswith("rr_yes:"))
async def checkin_yes(callback: CallbackQuery) -> None:
    cycle_key = callback.data.split(":", 1)[1]

    if cycle_key != current_cycle():
        await callback.answer("Это сообщение относится к другой неделе.", show_alert=True)
        return

    user = await get_user(callback.from_user.id)
    if (
        not user
        or user["status"] != "active"
        or user["active"] != 1
        or user["profile_migration_required"] != 0
        or user["onboarding_step"] != "completed"
    ):
        await callback.answer("Сначала нужно завершить профиль.", show_alert=True)
        if user and user["profile_migration_required"] == 1:
            await callback.message.answer(
                "Чтобы участвовать в Ristretto, сначала дополни профиль.",
                reply_markup=profile_migration_start_keyboard(),
            )
        return

    match_status = await get_job_status("random_match", cycle_key)
    if match_status is not None:
        await callback.answer("Запись на эту неделю уже закрыта.", show_alert=True)
        return

    saved = await set_weekly_response(callback.from_user.id, cycle_key, "yes")
    if not saved:
        await callback.answer("Сейчас участие недоступно.", show_alert=True)
        return

    await callback.message.edit_text(
        "Есть. Во вторник пришлю твою компанию на эту неделю."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rr_no:"))
async def checkin_no(callback: CallbackQuery) -> None:
    cycle_key = callback.data.split(":", 1)[1]

    if cycle_key != current_cycle():
        await callback.answer("Это сообщение относится к другой неделе.", show_alert=True)
        return

    match_status = await get_job_status("random_match", cycle_key)
    if match_status is not None:
        await callback.answer("Запись на эту неделю уже закрыта.", show_alert=True)
        return

    saved = await set_weekly_response(callback.from_user.id, cycle_key, "no")
    if not saved:
        await callback.answer("Сейчас участие недоступно.", show_alert=True)
        return

    await callback.message.edit_text("Хорошо, пропустим эту неделю.")
    await callback.answer()


@router.callback_query(F.data.in_({"yes", "no"}))
async def old_checkin_callback(callback: CallbackQuery) -> None:
    await callback.answer(
        "Эта кнопка устарела. Дождись нового еженедельного сообщения.",
        show_alert=True,
    )


# =========================================================
# RANDOM RISTRETTO: МЭТЧИНГ
# =========================================================
def normalized_pair(user_a: int, user_b: int) -> tuple[int, int]:
    return (user_a, user_b) if user_a < user_b else (user_b, user_a)


def pair_team_kind(team_a: str | None, team_b: str | None) -> str | None:
    if not team_a or not team_b or team_a == "other" or team_b == "other":
        return None
    return "same" if team_a == team_b else "cross"


def pair_score(
    user_a: dict,
    user_b: dict,
    pair_history: dict[tuple[int, int], dict],
    user_team_history: dict[int, dict[str, int]],
) -> tuple[int, int, int, float]:
    key = normalized_pair(user_a["user_id"], user_b["user_id"])
    history = pair_history.get(key)
    is_repeat = 1 if history else 0

    team_penalty = 0
    kind = pair_team_kind(user_a.get("team"), user_b.get("team"))
    if kind:
        for user in (user_a, user_b):
            counts = user_team_history.get(user["user_id"], {"same": 0, "cross": 0})
            if kind == "same":
                team_penalty += counts.get("same", 0) - counts.get("cross", 0)
            else:
                team_penalty += counts.get("cross", 0) - counts.get("same", 0)

    repeat_count = int(history["count"]) if history else 0
    last_matched_ts = history["last_matched_at"].timestamp() if history else 0.0
    return (is_repeat, team_penalty, repeat_count, last_matched_ts)


def group_edges(group: list[dict]) -> list[tuple[dict, dict]]:
    return [(a, b) for a, b in combinations(group, 2)]


def plan_score(
    groups: list[list[dict]],
    pair_history: dict[tuple[int, int], dict],
    user_team_history: dict[int, dict[str, int]],
) -> tuple[int, int, int, float]:
    scores = [
        pair_score(a, b, pair_history, user_team_history)
        for group in groups
        for a, b in group_edges(group)
    ]
    return (
        sum(score[0] for score in scores),
        sum(score[1] for score in scores),
        sum(score[2] for score in scores),
        sum(score[3] for score in scores),
    )


def build_match_plan(
    participants: list[dict],
    pair_history: dict[tuple[int, int], dict],
    user_team_history: dict[int, dict[str, int]],
    cycle_key: str,
    trials: int = 1200,
) -> list[list[dict]]:
    """
    Heuristic search with lexicographic priorities:
    1) as few repeated acquaintances as possible;
    2) soft balance between same-team and cross-team meetings;
    3) among repeats, rarer repeats first;
    4) among equally rare repeats, the oldest repeat first.

    Every trial is deterministic for the cycle key, so a restart cannot change the
    plan before it is persisted to the database.
    """
    if len(participants) < 2:
        return []

    rng = random.Random(f"random-ristretto:{cycle_key}")
    best_groups: list[list[dict]] | None = None
    best_score: tuple[int, int, int, float] | None = None

    participant_dicts = [dict(user) for user in participants]
    trial_count = max(200, trials)

    for _ in range(trial_count):
        remaining = participant_dicts.copy()
        rng.shuffle(remaining)
        groups: list[list[dict]] = []

        while len(remaining) > 3:
            # Start with the person who currently has the fewest new options.
            option_rows = []
            for user in remaining:
                new_options = sum(
                    1
                    for other in remaining
                    if other["user_id"] != user["user_id"]
                    and normalized_pair(user["user_id"], other["user_id"]) not in pair_history
                )
                option_rows.append((new_options, rng.random(), user))
            _, _, first = min(option_rows, key=lambda row: (row[0], row[1]))
            remaining.remove(first)

            candidate_rows = []
            for other in remaining:
                score = pair_score(first, other, pair_history, user_team_history)
                candidate_rows.append((score, rng.random(), other))
            _, _, second = min(candidate_rows, key=lambda row: (row[0], row[1]))
            remaining.remove(second)
            groups.append([first, second])

        if len(remaining) == 3:
            groups.append(remaining.copy())
        elif len(remaining) == 2:
            groups.append(remaining.copy())

        score = plan_score(groups, pair_history, user_team_history)
        if best_score is None or score < best_score:
            best_score = score
            best_groups = [group.copy() for group in groups]

    return best_groups or []


async def backfill_match_relationships(conn: asyncpg.Connection) -> None:
    """Idempotently imports old pair history into the normalized v2 table."""
    await conn.execute("""
        INSERT INTO match_relationships (
            cycle_key, user_a, user_b, group_id, matched_at
        )
        SELECT
            g.cycle_key,
            LEAST(m1.user_id, m2.user_id),
            GREATEST(m1.user_id, m2.user_id),
            g.id,
            g.created_at
        FROM match_groups g
        JOIN match_group_members m1 ON m1.group_id=g.id
        JOIN match_group_members m2
          ON m2.group_id=g.id
         AND m1.user_id < m2.user_id
        ON CONFLICT (cycle_key, user_a, user_b) DO NOTHING
    """)

    await conn.execute("""
        INSERT INTO match_relationships (
            cycle_key, user_a, user_b, group_id, matched_at
        )
        SELECT
            'legacy-meeting-' || m.id::text,
            LEAST(m.user1, m.user2),
            GREATEST(m.user1, m.user2),
            NULL,
            m.created_at AT TIME ZONE 'Europe/Istanbul'
        FROM meetings m
        JOIN users u1 ON u1.user_id=m.user1
        JOIN users u2 ON u2.user_id=m.user2
        WHERE m.user1 IS NOT NULL
          AND m.user2 IS NOT NULL
          AND m.user1 <> m.user2
        ON CONFLICT (cycle_key, user_a, user_b) DO NOTHING
    """)


async def get_match_history(participants: list[asyncpg.Record]) -> tuple[dict, dict]:
    if not participants:
        return {}, {}

    participant_ids = [user["user_id"] for user in participants]
    pair_history: dict[tuple[int, int], dict] = {}
    user_team_history: dict[int, dict[str, int]] = {
        user_id: {"same": 0, "cross": 0} for user_id in participant_ids
    }

    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.user_a, r.user_b, r.matched_at,
                   ua.team AS team_a, ub.team AS team_b
            FROM match_relationships r
            JOIN users ua ON ua.user_id=r.user_a
            JOIN users ub ON ub.user_id=r.user_b
            WHERE r.user_a = ANY($1::bigint[])
               OR r.user_b = ANY($1::bigint[])
            ORDER BY r.matched_at
        """, participant_ids)

    participant_id_set = set(participant_ids)
    for row in rows:
        key = normalized_pair(row["user_a"], row["user_b"])
        if row["user_a"] in participant_id_set and row["user_b"] in participant_id_set:
            info = pair_history.setdefault(key, {"count": 0, "last_matched_at": row["matched_at"]})
            info["count"] += 1
            if row["matched_at"] > info["last_matched_at"]:
                info["last_matched_at"] = row["matched_at"]

        kind = pair_team_kind(row["team_a"], row["team_b"])
        if kind:
            if row["user_a"] in participant_id_set:
                user_team_history[row["user_a"]][kind] += 1
            if row["user_b"] in participant_id_set:
                user_team_history[row["user_b"]][kind] += 1

    return pair_history, user_team_history


async def choose_group_icebreaker(
    conn: asyncpg.Connection,
    member_ids: list[int],
    cycle_key: str,
    group_index: int,
) -> tuple[str, int | None]:
    """
    Uses the approved DB pool when it is populated. Until the approved 100-question
    pool is loaded, the legacy in-code list remains a temporary fallback.
    """
    active = await conn.fetch("""
        SELECT id, text
        FROM icebreakers
        WHERE active=1
        ORDER BY id
    """)

    if not active:
        rng = random.Random(f"{cycle_key}:icebreaker:{group_index}")
        return rng.choice(ICEBREAKERS), None

    active_ids = [row["id"] for row in active]
    active_count = len(active_ids)

    # Reset an individual cycle only after that person has seen the whole active pool.
    for user_id in member_ids:
        used_count = await conn.fetchval("""
            SELECT COUNT(*)
            FROM user_icebreakers ui
            WHERE ui.user_id=$1
              AND ui.icebreaker_id = ANY($2::bigint[])
        """, user_id, active_ids)
        if used_count >= active_count:
            await conn.execute("""
                DELETE FROM user_icebreakers
                WHERE user_id=$1
                  AND icebreaker_id = ANY($2::bigint[])
            """, user_id, active_ids)

    used_rows = await conn.fetch("""
        SELECT user_id, icebreaker_id
        FROM user_icebreakers
        WHERE user_id = ANY($1::bigint[])
          AND icebreaker_id = ANY($2::bigint[])
    """, member_ids, active_ids)
    used_by_user: dict[int, set[int]] = {user_id: set() for user_id in member_ids}
    for row in used_rows:
        used_by_user[row["user_id"]].add(row["icebreaker_id"])

    candidates = [
        row for row in active
        if all(row["id"] not in used_by_user[user_id] for user_id in member_ids)
    ]

    if not candidates:
        # Extremely rare intersection exhaustion for a shared group question.
        # Start a fresh shared cycle for this group instead of failing the match run.
        await conn.execute("""
            DELETE FROM user_icebreakers
            WHERE user_id = ANY($1::bigint[])
              AND icebreaker_id = ANY($2::bigint[])
        """, member_ids, active_ids)
        candidates = list(active)

    rng = random.Random(f"{cycle_key}:icebreaker:{group_index}")
    chosen = rng.choice(candidates)
    return chosen["text"], chosen["id"]


async def create_match_groups_if_needed(cycle_key: str) -> int:
    assert pool is not None

    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT COUNT(*) FROM match_groups WHERE cycle_key=$1",
            cycle_key,
        )
        if existing:
            return existing

    users = list(await get_confirmed_users(cycle_key))
    if len(users) < 2:
        return 0

    pair_history, user_team_history = await get_match_history(users)
    groups = build_match_plan(
        [dict(user) for user in users],
        pair_history,
        user_team_history,
        cycle_key,
    )

    async with pool.acquire() as conn:
        async with conn.transaction():
            # A second guard makes the operation idempotent if two processes race.
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM match_groups WHERE cycle_key=$1",
                cycle_key,
            )
            if existing:
                return existing

            for group_index, group in enumerate(groups):
                member_ids = [user["user_id"] for user in group]
                topic, icebreaker_id = await choose_group_icebreaker(
                    conn, member_ids, cycle_key, group_index
                )

                group_id = await conn.fetchval("""
                    INSERT INTO match_groups (cycle_key, topic, created_at)
                    VALUES ($1, $2, NOW())
                    RETURNING id
                """, cycle_key, topic)

                for user in group:
                    await conn.execute("""
                        INSERT INTO match_group_members (group_id, user_id, cycle_key)
                        VALUES ($1, $2, $3)
                    """, group_id, user["user_id"], cycle_key)

                for user_a, user_b in combinations(group, 2):
                    pair_a, pair_b = normalized_pair(user_a["user_id"], user_b["user_id"])
                    await conn.execute("""
                        INSERT INTO match_relationships (
                            cycle_key, user_a, user_b, group_id, matched_at
                        )
                        VALUES ($1, $2, $3, $4, NOW())
                        ON CONFLICT (cycle_key, user_a, user_b) DO NOTHING
                    """, cycle_key, pair_a, pair_b, group_id)

                if icebreaker_id is not None:
                    for user_id in member_ids:
                        await conn.execute("""
                            INSERT INTO user_icebreakers (user_id, icebreaker_id, used_at)
                            VALUES ($1, $2, NOW())
                            ON CONFLICT (user_id, icebreaker_id) DO NOTHING
                        """, user_id, icebreaker_id)

    return len(groups)


async def get_match_groups(cycle_key: str) -> list[dict]:
    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                g.id AS group_id,
                g.topic,
                u.user_id,
                u.username,
                u.first_name,
                u.display_name,
                u.team,
                u.responsibility_text,
                CASE WHEN n.user_id IS NULL THEN 0 ELSE 1 END AS notified
            FROM match_groups g
            JOIN match_group_members m ON m.group_id=g.id
            JOIN users u ON u.user_id=m.user_id
            LEFT JOIN match_notifications n
                ON n.group_id=g.id AND n.user_id=u.user_id
            WHERE g.cycle_key=$1
            ORDER BY g.id, u.user_id
        """, cycle_key)

    grouped: dict[int, dict] = {}
    for row in rows:
        group = grouped.setdefault(
            row["group_id"],
            {"group_id": row["group_id"], "topic": row["topic"], "members": []},
        )
        group["members"].append(row)

    return list(grouped.values())


async def mark_match_notified(group_id: int, user_id: int) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO match_notifications (group_id, user_id, delivered_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (group_id, user_id) DO NOTHING
        """, group_id, user_id)


def match_profile_block(user: asyncpg.Record | dict) -> str:
    lines = [
        profile_title(user),
        f"Отвечает за: {(user['responsibility_text'] or '').strip()}",
    ]
    if user["username"]:
        lines.append(f"@{user['username']}")
    return "\n".join(lines)


async def notify_lone_participant(cycle_key: str, user: asyncpg.Record) -> None:
    job_name = "random_match_solo"
    assert pool is not None

    async with pool.acquire() as conn:
        delivered = await conn.fetchval("""
            SELECT 1 FROM broadcast_deliveries
            WHERE job_name=$1 AND cycle_key=$2 AND user_id=$3
        """, job_name, cycle_key, user["user_id"])

    if delivered:
        return

    try:
        await safe_send_message(
            user["user_id"],
            "На этой неделе коллеги без кофеина, для Ristretto не нашлось компании.\n\n"
            "Попробуем снова в следующий понедельник.",
        )
        await mark_broadcast_delivered(job_name, cycle_key, user["user_id"])
    except TelegramForbiddenError:
        await deactivate_unreachable_user(user["user_id"])
    except Exception:
        logging.exception("Не удалось уведомить единственного участника")
        raise


async def run_matching(cycle_key: str) -> None:
    confirmed = list(await get_confirmed_users(cycle_key))

    if len(confirmed) == 1:
        await notify_lone_participant(cycle_key, confirmed[0])
        return

    if len(confirmed) == 0:
        return

    await create_match_groups_if_needed(cycle_key)
    groups = await get_match_groups(cycle_key)
    failures = 0

    for group in groups:
        for member in group["members"]:
            if member["notified"] == 1:
                continue

            others = [
                match_profile_block(other)
                for other in group["members"]
                if other["user_id"] != member["user_id"]
            ]

            text = (
                "Твоя компания для Ristretto на этой неделе:\n\n"
                + "\n\n".join(others)
                + "\n\nНапишите друг другу в Telegram или TAG — как удобнее.\n\n"
                + "На случай тех самых тихих 30 секунд в начале — "
                  "вот необязательная тема для старта:\n"
                + group["topic"]
            )

            try:
                await safe_send_message(member["user_id"], text)
                await mark_match_notified(group["group_id"], member["user_id"])
            except TelegramForbiddenError:
                await deactivate_unreachable_user(member["user_id"])
            except Exception:
                failures += 1
                logging.exception("Не удалось отправить результат мэтчинга")

    if failures:
        raise RuntimeError(f"Не доставлено результатов мэтчинга: {failures}")


# =========================================================
# БЛАГОДАРНОСТИ V2: ПОИСК, СБОР, БЛОКИРОВКИ И МОДЕРАЦИЯ
# =========================================================
THANKS_PUBLIC_LIMIT = 200

# Имя можно искать в любом регистре. Все варианты ниже автоматически
# приводятся к casefold() и "ё" -> "е", поэтому Катя/катя/КАТЯ и
# Лёша/леша обрабатываются одинаково.
NAME_ALIAS_GROUPS = [
    {"Антонина", "Тоня", "Antonina", "Tonya"},
    {"Елена", "Лена", "Elena", "Lena"},
    {"Полина", "Polina"},
    {"Данила", "Даня", "Danila", "Danya"},
    {"Филипп", "Филя", "Filipp", "Philipp"},
    {"Алёна", "Алена", "Alyona", "Alena"},
    {"Татьяна", "Таня", "Tatyana", "Tatiana", "Tanya"},
    {"Юлия", "Юля", "Julia", "Yulia"},
    {"Евгений", "Женя", "Evgeny", "Evgeniy", "Eugene", "Zhenya"},
    {"Ольга", "Оля", "Olga", "Olya"},
    {"Анна", "Аня", "Anna", "Anya"},
    {"Павел", "Паша", "Pavel", "Pasha"},
    {"Ирина", "Ира", "Irina", "Ira"},
    {"Николай", "Коля", "Nikolay", "Nikolai", "Kolya"},
    {"Дмитрий", "Дима", "Dmitry", "Dmitriy", "Dima"},
    {"Анастасия", "Настя", "Anastasia", "Anastasiya", "Anastasiia", "Nastya"},
    {"Дарья", "Даша", "Daria", "Darya", "Dasha"},
    {"Вера", "Vera"},
    {"Артём", "Артем", "Тёма", "Тема", "Artem", "Artyom", "Tyoma"},
    {"Айна", "Aina"},
    {"Евгения", "Женя", "Eugenia", "Evgenia", "Zhenya"},
    {"Яна", "Yana"},
    {"Влада", "Vlada"},
    {"Ксения", "Ксюша", "Ksenia", "Xenia", "Ksyusha"},
    {"Елизавета", "Лиза", "Elizaveta", "Liza"},
    {"Диана", "Diana"},
    {"Вадим", "Vadim"},
    {"Валерия", "Лера", "Valeria", "Valeriya", "Lera"},
    {"Виктория", "Вика", "Victoria", "Viktoria", "Vika"},
    {"Екатерина", "Катерина", "Катя", "Ekaterina", "Katerina", "Kat", "Kate", "Katya"},
    {"Мария", "Маша", "Maria", "Masha"},
    {"Роман", "Рома", "Roman", "Roma"},
    {"Андрей", "Andrey", "Andrei", "Andrew"},
    {"Никита", "Nikita"},
    {"Любовь", "Люба", "Lyubov", "Liubov", "Lyuba"},
    {"Таисия", "Тася", "Тая", "Taisia", "Tasya", "Taya"},
    {"Дамир", "Damir"},
    {"Сергей", "Серёжа", "Сережа", "Sergey", "Sergei", "Serezha"},
    {"Светлана", "Света", "Svetlana", "Sveta"},
    {"Зинаида", "Зина", "Zinaida", "Zina"},
    {"Алексей", "Лёша", "Леша", "Alexey", "Aleksey", "Lesha"},
    {"Тамара", "Тома", "Tamara", "Toma"},
    {"Илья", "Ilya", "Ilia"},
    {"Ляйсан", "Ляся", "Lyasyan", "Laysan", "Lyasya"},
    {"Людмила", "Люся", "Люда", "Ludmila", "Lyudmila", "Liudmila", "Lyusya", "Lyuda"},

    # Небольшой резерв для распространённых имён, которых пока может не быть
    # среди текущих участников.
    {"Александр", "Саша", "Alexander", "Aleksandr", "Sasha"},
    {"Александра", "Саша", "Alexandra", "Aleksandra", "Sasha"},
    {"Михаил", "Миша", "Mikhail", "Misha"},
    {"Максим", "Макс", "Maksim", "Maxim", "Max"},
    {"Владимир", "Вова", "Vladimir", "Vova"},
    {"Наталья", "Наташа", "Natalia", "Natalya", "Natasha"},
    {"Надежда", "Надя", "Nadezhda", "Nadya"},
    {"Маргарита", "Рита", "Margarita", "Rita"},
]


def _normalize_alias_literal(value: str) -> str:
    return " ".join((value or "").casefold().replace("ё", "е").split())


THANKS_ALIASES: dict[str, set[str]] = {}
for _group in NAME_ALIAS_GROUPS:
    _normalized_group = {
        _normalize_alias_literal(item)
        for item in _group
        if _normalize_alias_literal(item)
    }
    for _name in _normalized_group:
        THANKS_ALIASES.setdefault(_name, set()).update(
            _normalized_group - {_name}
        )


def truncate_public_thanks(text: str, limit: int = THANKS_PUBLIC_LIMIT) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text

    room = max(1, limit - 1)
    cut = text[:room].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0].rstrip()
    if not cut:
        cut = text[:room].rstrip()
    return cut + "…"


def normalize_thanks_lookup(value: str) -> str:
    value = (value or "").casefold().replace("ё", "е").strip()
    cleaned = []
    for char in value:
        if char.isalnum() or char in {"@", "_", " ", "-"}:
            cleaned.append(char)
        else:
            cleaned.append(" ")
    return " ".join("".join(cleaned).split())


def lookup_variants(query: str) -> set[str]:
    normalized = normalize_thanks_lookup(query)
    bare = normalized.lstrip("@")
    if not bare:
        return set()

    variants = {normalized, bare}
    tokens = bare.split()

    if len(tokens) == 1:
        token = tokens[0]
        variants.update(
            normalize_thanks_lookup(alias)
            for alias in THANKS_ALIASES.get(token, set())
        )
    elif tokens:
        first = tokens[0]
        for alias in THANKS_ALIASES.get(first, set()):
            variants.add(
                " ".join([
                    normalize_thanks_lookup(alias),
                    *tokens[1:],
                ])
            )

    return {variant for variant in variants if variant}


def thanks_mode_label(include_in_digest: int) -> str:
    return "лично + в дайджест" if include_in_digest else "только лично"


async def get_open_thanks_cycle() -> str:
    now = datetime.now(TZ)
    cycle_key = current_cycle(now)

    # Friday 16:00 closes the collection window for the current weekly delivery.
    # New thanks sent after that point belong to the next week's cycle even if
    # the public digest is still waiting for approval.
    if (
        now.weekday() > THANKS_DIGEST_WEEKDAY
        or (
            now.weekday() == THANKS_DIGEST_WEEKDAY
            and now.hour >= THANKS_DIGEST_HOUR
        )
    ):
        return next_cycle(now)

    assert pool is not None
    async with pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM digest_runs WHERE cycle_key=$1",
            cycle_key,
        )
    if status in {"approved", "published"}:
        return next_cycle(now)
    return cycle_key


async def is_thanks_blocked(blocker_id: int, sender_id: int) -> bool:
    assert pool is not None
    async with pool.acquire() as conn:
        return bool(await conn.fetchval("""
            SELECT 1
            FROM thanks_blocks
            WHERE blocker_id=$1
              AND blocked_sender_id=$2
              AND unblocked_at IS NULL
            LIMIT 1
        """, blocker_id, sender_id))


async def search_thanks_recipients(sender_id: int, query: str) -> tuple[list[asyncpg.Record], bool]:
    variants = lookup_variants(query)
    if not variants:
        return [], False

    assert pool is not None
    async with pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT u.*
            FROM users u
            WHERE u.user_id <> $1
              AND u.status='active'
              AND u.active=1
              AND u.deleted_at IS NULL
              AND u.removed_by_admin=0
            ORDER BY LOWER(COALESCE(u.display_name, u.username, '')), u.user_id
        """, sender_id)

        blocked_rows = await conn.fetch("""
            SELECT blocker_id
            FROM thanks_blocks
            WHERE blocked_sender_id=$1
              AND unblocked_at IS NULL
        """, sender_id)

    blocked_ids = {row["blocker_id"] for row in blocked_rows}
    matched: list[asyncpg.Record] = []
    matched_blocked = False

    for user in users:
        name = normalize_thanks_lookup(user["display_name"] or "")
        username = normalize_thanks_lookup(user["username"] or "").lstrip("@")
        first_token = name.split()[0] if name else ""
        name_tokens = set(name.split())

        is_match = False
        for variant in variants:
            bare = variant.lstrip("@")
            if not bare:
                continue
            if bare == username and username:
                is_match = True
                break
            if bare in name_tokens or bare == first_token:
                is_match = True
                break
            if len(bare) >= 3 and (
                name.startswith(bare + " ")
                or name == bare
                or username.startswith(bare)
            ):
                is_match = True
                break

        if not is_match:
            continue

        if user["user_id"] in blocked_ids:
            matched_blocked = True
            continue

        matched.append(user)

    return matched[:12], matched_blocked


def thanks_search_results_keyboard(users: list[asyncpg.Record]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    counts: dict[str, int] = {}

    for user in users:
        key = clean_name(user["display_name"]).casefold()
        counts[key] = counts.get(key, 0) + 1

    for user in users:
        label = clean_name(user["display_name"])
        if counts[label.casefold()] > 1 and user["username"]:
            label = f"{label} · @{user['username']}"
        rows.append([
            InlineKeyboardButton(
                text=label[:60],
                callback_data=f"thanks_to:{user['user_id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(text="Искать ещё", callback_data="thanks_search_again")
    ])
    rows.append([
        InlineKeyboardButton(text="Выбрать по команде", callback_data="thanks_browse_teams")
    ])
    rows.append([
        InlineKeyboardButton(text="Отмена", callback_data="thanks_cancel")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def thanks_team_browse_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продажи", callback_data="thanks_team:sales")],
            [InlineKeyboardButton(text="Операции", callback_data="thanks_team:operations")],
            [InlineKeyboardButton(text="Маркетинг", callback_data="thanks_team:marketing")],
            [InlineKeyboardButton(text="Другая команда", callback_data="thanks_team:other")],
            [InlineKeyboardButton(text="Отмена", callback_data="thanks_cancel")],
        ]
    )


async def thanks_team_users_keyboard(
    sender_id: int,
    team: str,
) -> InlineKeyboardMarkup | None:
    assert pool is not None
    async with pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT u.*
            FROM users u
            WHERE u.user_id <> $1
              AND u.team=$2
              AND u.status='active'
              AND u.active=1
              AND u.deleted_at IS NULL
              AND u.removed_by_admin=0
              AND NOT EXISTS (
                  SELECT 1
                  FROM thanks_blocks b
                  WHERE b.blocker_id=u.user_id
                    AND b.blocked_sender_id=$1
                    AND b.unblocked_at IS NULL
              )
            ORDER BY LOWER(COALESCE(u.display_name, u.username, '')), u.user_id
        """, sender_id, team)

    if not users:
        return None
    return thanks_search_results_keyboard(list(users))


def thanks_visibility_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Только лично", callback_data="thanks_mode:private")],
            [InlineKeyboardButton(text="Лично + в дайджест", callback_data="thanks_mode:digest")],
            [InlineKeyboardButton(text="Отмена", callback_data="thanks_cancel")],
        ]
    )


def thanks_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отправить", callback_data="thanks_send")],
            [InlineKeyboardButton(text="Изменить текст", callback_data="thanks_edit")],
            [InlineKeyboardButton(text="Отмена", callback_data="thanks_cancel")],
        ]
    )


def thanks_public_truncate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оставить так", callback_data="thanks_public_keep")],
            [InlineKeyboardButton(text="Написать короче", callback_data="thanks_public_rewrite")],
            [InlineKeyboardButton(text="Отмена", callback_data="thanks_cancel")],
        ]
    )


def thanks_block_keyboard(thanks_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="Не получать спасибо от этого человека",
                callback_data=f"thanks_block:{thanks_id}",
            )]
        ]
    )


def thanks_moderation_keyboard(thanks_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оставить", callback_data=f"thanks_mod_keep:{thanks_id}")],
            [InlineKeyboardButton(
                text="Убрать из дайджеста",
                callback_data=f"thanks_mod_remove:{thanks_id}",
            )],
            [InlineKeyboardButton(
                text="Убрать и закрыть автору доступ",
                callback_data=f"thanks_mod_revoke:{thanks_id}",
            )],
        ]
    )


def digest_review_keyboard(cycle_key: str, status: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="Предпросмотр",
            callback_data=f"digest_preview:{cycle_key}",
        )],
        [InlineKeyboardButton(
            text="Модерация",
            callback_data=f"digest_moderate:{cycle_key}",
        )],
    ]
    if status == "draft":
        rows.append([
            InlineKeyboardButton(
                text="Готово к публикации",
                callback_data=f"digest_approve:{cycle_key}",
            )
        ])
    elif status == "approved":
        rows.append([
            InlineKeyboardButton(
                text="Вернуть в модерацию",
                callback_data=f"digest_return:{cycle_key}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def begin_thanks(message: Message, sender_id: int) -> None:
    user = await get_user(sender_id)
    if not user:
        await message.answer("Сначала отправь /start.")
        return

    if user["status"] == "access_denied":
        await message.answer(access_denied_message())
        return

    if user["status"] == "pending_activation":
        await message.answer("Сначала дождись активации профиля администратором.")
        return

    if user["status"] == "self_deleted" or user["deleted_at"] is not None:
        await message.answer("Профиль удалён. Чтобы вернуться, отправь /start.")
        return

    if user["status"] not in {"active", "paused"}:
        await message.answer("Сейчас благодарности недоступны.")
        return

    await clear_flow(sender_id)
    await set_flow(sender_id, "thanks", "waiting_recipient_query")
    await message.answer(
        "Кого хочешь поблагодарить?\n\n"
        "Напиши имя, фамилию или @username. Можно как помнишь :)"
    )


@router.message(Command("thanks"))
async def thanks_command(message: Message) -> None:
    await begin_thanks(message, message.from_user.id)


@router.callback_query(F.data == "thanks_start")
async def thanks_start_callback(callback: CallbackQuery) -> None:
    await begin_thanks(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "thanks_search_again")
async def thanks_search_again_callback(callback: CallbackQuery) -> None:
    await set_flow(callback.from_user.id, "thanks", "waiting_recipient_query")
    await callback.message.answer(
        "Напиши имя, фамилию или @username. Можно как помнишь :)"
    )
    await callback.answer()


@router.callback_query(F.data == "thanks_browse_teams")
async def thanks_browse_teams_callback(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "Можно выбрать коллегу по команде:",
        reply_markup=thanks_team_browse_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("thanks_team:"))
async def thanks_team_callback(callback: CallbackQuery) -> None:
    team = callback.data.split(":", 1)[1]
    if team not in TEAM_LABELS:
        await callback.answer("Неизвестная команда.", show_alert=True)
        return

    keyboard = await thanks_team_users_keyboard(callback.from_user.id, team)
    if keyboard is None:
        await callback.message.answer(
            "В этой команде сейчас не нашлось доступных получателей."
        )
        await callback.answer()
        return

    await callback.message.answer("Выбери коллегу:", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("thanks_to:"))
async def thanks_recipient_callback(callback: CallbackQuery) -> None:
    recipient_id = int(callback.data.split(":", 1)[1])

    if recipient_id == callback.from_user.id:
        await callback.answer("Нельзя выбрать себя.", show_alert=True)
        return

    recipient = await get_user(recipient_id)
    if (
        not recipient
        or recipient["active"] != 1
        or recipient["status"] != "active"
        or recipient["deleted_at"] is not None
        or recipient["removed_by_admin"] == 1
    ):
        await callback.answer("Сейчас этому человеку нельзя отправить благодарность через бот.", show_alert=True)
        return

    if await is_thanks_blocked(recipient_id, callback.from_user.id):
        await callback.answer(
            "Сейчас этому человеку нельзя отправить благодарность через бот.",
            show_alert=True,
        )
        return

    cycle_key = await get_open_thanks_cycle()
    await set_flow(
        callback.from_user.id,
        "thanks",
        "waiting_text",
        cycle_key=cycle_key,
        recipient_id=recipient_id,
    )

    await callback.message.answer(
        f"Напиши текст благодарности для {clean_name(recipient['display_name'])}.\n\n"
        "Получатель увидит твоё имя в личном сообщении."
    )
    await callback.answer()


async def send_thanks_final_preview(
    target: Message,
    sender_id: int,
    include_in_digest: int,
) -> None:
    flow = await get_flow(sender_id)
    if not flow or not flow["draft_text"] or not flow["recipient_id"]:
        await target.answer("Черновик не найден. Начни снова командой /thanks.")
        return

    recipient = await get_user(flow["recipient_id"])
    if not recipient:
        await target.answer("Получатель больше недоступен. Начни снова командой /thanks.")
        return

    await set_flow(
        sender_id,
        "thanks",
        "confirm",
        cycle_key=flow["cycle_key"],
        recipient_id=flow["recipient_id"],
        draft_text=flow["draft_text"],
        include_in_digest=include_in_digest,
    )

    preview = (
        "Проверь перед отправкой:\n\n"
        f"Получатель: {clean_name(recipient['display_name'])}\n"
        f"Режим: {thanks_mode_label(include_in_digest)}\n\n"
        f"«{flow['draft_text']}»"
    )

    if include_in_digest:
        public_text = truncate_public_thanks(flow["draft_text"])
        preview += (
            "\n\nВ пятничных хайлайтах будет:\n"
            f"{profile_title(recipient)}\n"
            f"«{public_text}»\n\n"
            "Имя автора в общем дайджесте не указываем."
        )

    await target.answer(preview, reply_markup=thanks_confirm_keyboard())


@router.callback_query(F.data.startswith("thanks_mode:"))
async def thanks_mode_callback(callback: CallbackQuery) -> None:
    flow = await get_flow(callback.from_user.id)
    if (
        not flow
        or flow["flow_type"] != "thanks"
        or flow["step"] != "choose_mode"
        or not flow["draft_text"]
    ):
        await callback.answer(
            "Черновик не найден. Начни снова командой /thanks.",
            show_alert=True,
        )
        return

    mode = callback.data.split(":", 1)[1]
    if mode == "private":
        await send_thanks_final_preview(callback.message, callback.from_user.id, 0)
        await callback.answer()
        return

    if mode != "digest":
        await callback.answer("Неизвестный режим.", show_alert=True)
        return

    explainer = (
        "Твоё сообщение придёт получателю целиком и будет подписано твоим именем.\n\n"
        "В пятничном дайджесте появится имя коллеги и текст благодарности, "
        "но имя автора там указывать не будем.\n\n"
        "Здесь есть лимит — до 200 знаков, чтобы дайджест не превращался "
        "в бробдингнегский манускрипт.\n\n"
        "Если получится длиннее, я обрежу текст для дайджеста, "
        "но отправлю твоё сообщение целиком получателю лично :)"
    )
    await callback.message.answer(explainer)

    if len(" ".join(flow["draft_text"].split())) > THANKS_PUBLIC_LIMIT:
        public_text = truncate_public_thanks(flow["draft_text"])
        await set_flow(
            callback.from_user.id,
            "thanks",
            "public_truncate_preview",
            cycle_key=flow["cycle_key"],
            recipient_id=flow["recipient_id"],
            draft_text=flow["draft_text"],
            include_in_digest=1,
        )
        await callback.message.answer(
            "Для пятничных хайлайтов получится так:\n\n"
            f"«{public_text}»",
            reply_markup=thanks_public_truncate_keyboard(),
        )
    else:
        await send_thanks_final_preview(callback.message, callback.from_user.id, 1)

    await callback.answer()


@router.callback_query(F.data == "thanks_public_keep")
async def thanks_public_keep_callback(callback: CallbackQuery) -> None:
    flow = await get_flow(callback.from_user.id)
    if (
        not flow
        or flow["flow_type"] != "thanks"
        or flow["step"] != "public_truncate_preview"
    ):
        await callback.answer("Черновик не найден.", show_alert=True)
        return

    await send_thanks_final_preview(callback.message, callback.from_user.id, 1)
    await callback.answer()


@router.callback_query(F.data == "thanks_public_rewrite")
async def thanks_public_rewrite_callback(callback: CallbackQuery) -> None:
    flow = await get_flow(callback.from_user.id)
    if not flow or flow["flow_type"] != "thanks":
        await callback.answer("Черновик не найден.", show_alert=True)
        return

    await set_flow(
        callback.from_user.id,
        "thanks",
        "waiting_text",
        cycle_key=flow["cycle_key"],
        recipient_id=flow["recipient_id"],
    )
    await callback.message.answer(
        "Напиши новый текст. Получателю он придёт целиком; "
        "для дайджеста постарайся уложиться в 200 знаков."
    )
    await callback.answer()


@router.callback_query(F.data == "thanks_edit")
async def thanks_edit_callback(callback: CallbackQuery) -> None:
    flow = await get_flow(callback.from_user.id)
    if not flow or flow["flow_type"] != "thanks":
        await callback.answer("Черновик не найден.", show_alert=True)
        return

    await set_flow(
        callback.from_user.id,
        "thanks",
        "waiting_text",
        cycle_key=flow["cycle_key"],
        recipient_id=flow["recipient_id"],
    )
    await callback.message.answer("Напиши новый текст благодарности.")
    await callback.answer()


@router.callback_query(F.data == "thanks_cancel")
async def thanks_cancel_callback(callback: CallbackQuery) -> None:
    await clear_flow(callback.from_user.id)
    await callback.message.answer("Отправка благодарности отменена.")
    await callback.answer()


async def save_thanks_from_flow(user_id: int, flow: asyncpg.Record) -> int | None:
    sender = await get_user(user_id)
    recipient = await get_user(flow["recipient_id"])

    if not sender or not recipient:
        return None

    if sender["status"] not in {"active", "paused"}:
        return None

    if (
        recipient["status"] != "active"
        or recipient["active"] != 1
        or recipient["deleted_at"] is not None
        or recipient["removed_by_admin"] == 1
    ):
        return None

    if await is_thanks_blocked(recipient["user_id"], user_id):
        return None

    cycle_key = flow["cycle_key"]
    assert pool is not None
    async with pool.acquire() as conn:
        digest_status = await conn.fetchval(
            "SELECT status FROM digest_runs WHERE cycle_key=$1",
            cycle_key,
        )
        if digest_status in {"approved", "published"}:
            return None

        include_in_digest = int(flow["include_in_digest"] or 0)
        public_text = (
            truncate_public_thanks(flow["draft_text"])
            if include_in_digest
            else None
        )
        public_status = "pending" if include_in_digest else "not_requested"

        thanks_id = await conn.fetchval("""
            INSERT INTO thanks (
                cycle_key, sender_id, recipient_id,
                sender_name, recipient_name, message_text,
                include_in_digest, created_at,
                public_text, personal_status, public_status
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, NOW(),
                $8, 'pending', $9
            )
            RETURNING id
        """,
            cycle_key,
            user_id,
            recipient["user_id"],
            clean_name(sender["display_name"]),
            clean_name(recipient["display_name"]),
            flow["draft_text"],
            include_in_digest,
            public_text,
            public_status,
        )
    return thanks_id


async def get_public_thanks_for_moderation(thanks_id: int):
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT
                t.*,
                sender.display_name AS current_sender_name,
                recipient.display_name AS current_recipient_name,
                recipient.team AS recipient_team
            FROM thanks t
            JOIN users sender ON sender.user_id=t.sender_id
            JOIN users recipient ON recipient.user_id=t.recipient_id
            WHERE t.id=$1
              AND t.include_in_digest=1
        """, thanks_id)


async def notify_public_thanks_moderation(thanks_id: int) -> None:
    row = await get_public_thanks_for_moderation(thanks_id)
    if not row or row["public_status"] != "pending":
        return

    text = (
        "Благодарность ждёт модерации для пятничных хайлайтов\n\n"
        f"Получатель: {clean_name(row['current_recipient_name'])}\n"
        f"Автор: {clean_name(row['current_sender_name'])}\n\n"
        f"«{row['public_text'] or truncate_public_thanks(row['message_text'])}»"
    )
    try:
        await safe_send_message(
            ADMIN_ID,
            text,
            reply_markup=thanks_moderation_keyboard(thanks_id),
        )
    except Exception:
        logging.exception("Не удалось отправить публичную благодарность на модерацию")


@router.callback_query(F.data == "thanks_send")
async def thanks_send_callback(callback: CallbackQuery) -> None:
    flow = await get_flow(callback.from_user.id)
    if (
        not flow
        or flow["flow_type"] != "thanks"
        or flow["step"] != "confirm"
        or not flow["draft_text"]
    ):
        await callback.answer(
            "Черновик не найден. Начни снова командой /thanks.",
            show_alert=True,
        )
        return

    thanks_id = await save_thanks_from_flow(callback.from_user.id, flow)
    include_in_digest = int(flow["include_in_digest"] or 0)
    await clear_flow(callback.from_user.id)

    if not thanks_id:
        await callback.message.answer(
            "Не удалось сохранить благодарность: получатель сейчас недоступен, "
            "закрыл благодарности от тебя или выпуск этой недели уже закрыт."
        )
        await callback.answer()
        return

    await callback.message.answer(
        "Готово. В пятницу я доставлю благодарность :)"
    )
    if include_in_digest:
        await notify_public_thanks_moderation(thanks_id)

    await callback.answer()


# =========================================================
# БЛОКИРОВКИ БЛАГОДАРНОСТЕЙ
# =========================================================
async def create_thanks_block(blocker_id: int, blocked_sender_id: int) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO thanks_blocks (
                    blocker_id, blocked_sender_id, created_at, unblocked_at
                )
                VALUES ($1, $2, NOW(), NULL)
                ON CONFLICT (blocker_id, blocked_sender_id)
                    WHERE unblocked_at IS NULL
                DO NOTHING
            """, blocker_id, blocked_sender_id)

            await conn.execute("""
                UPDATE thanks
                SET personal_status='cancelled'
                WHERE recipient_id=$1
                  AND sender_id=$2
                  AND personal_status='pending'
            """, blocker_id, blocked_sender_id)

            await conn.execute("""
                UPDATE thanks
                SET public_status='removed',
                    moderated_at=NOW(),
                    moderation_reason='recipient_blocked_sender'
                WHERE recipient_id=$1
                  AND sender_id=$2
                  AND public_status IN ('pending', 'approved')
            """, blocker_id, blocked_sender_id)


@router.callback_query(F.data.startswith("thanks_block:"))
async def thanks_block_callback(callback: CallbackQuery) -> None:
    thanks_id = int(callback.data.split(":", 1)[1])

    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, recipient_id, sender_id, sender_name
            FROM thanks
            WHERE id=$1
        """, thanks_id)

    if not row or row["recipient_id"] != callback.from_user.id:
        await callback.answer("Эта кнопка недоступна.", show_alert=True)
        return

    await create_thanks_block(callback.from_user.id, row["sender_id"])
    await callback.message.answer(
        f"Готово. Благодарности от {clean_name(row['sender_name'])} больше не будут приходить.\n\n"
        "Разблокировать отправителя можно командой /blocked."
    )
    await callback.answer()


@router.message(Command("blocked"))
async def blocked_command(message: Message) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT b.blocked_sender_id, u.display_name
            FROM thanks_blocks b
            JOIN users u ON u.user_id=b.blocked_sender_id
            WHERE b.blocker_id=$1
              AND b.unblocked_at IS NULL
            ORDER BY LOWER(COALESCE(u.display_name, '')), b.blocked_sender_id
        """, message.from_user.id)

    if not rows:
        await message.answer("Сейчас у тебя нет заблокированных отправителей.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"Разблокировать · {clean_name(row['display_name'])}"[:60],
                callback_data=f"thanks_unblock:{row['blocked_sender_id']}",
            )]
            for row in rows
        ]
    )
    await message.answer(
        "Заблокированные отправители:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("thanks_unblock:"))
async def thanks_unblock_callback(callback: CallbackQuery) -> None:
    blocked_sender_id = int(callback.data.split(":", 1)[1])

    assert pool is not None
    async with pool.acquire() as conn:
        changed = await conn.fetchval("""
            UPDATE thanks_blocks
            SET unblocked_at=NOW()
            WHERE blocker_id=$1
              AND blocked_sender_id=$2
              AND unblocked_at IS NULL
            RETURNING id
        """, callback.from_user.id, blocked_sender_id)

    if changed:
        await callback.message.answer("Готово. Отправитель разблокирован.")
    else:
        await callback.message.answer("Этот отправитель уже разблокирован.")
    await callback.answer()


# =========================================================
# МОДЕРАЦИЯ ПУБЛИЧНЫХ БЛАГОДАРНОСТЕЙ
# =========================================================
async def audit_admin_action(
    admin_user_id: int,
    action: str,
    target_user_id: int | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO admin_audit_log (
                admin_user_id, action, target_user_id,
                object_type, object_id, metadata, created_at
            )
            VALUES ($1, $2, $3, $4, $5, '{}'::jsonb, NOW())
        """, admin_user_id, action, target_user_id, object_type, object_id)


async def moderate_public_thanks(
    thanks_id: int,
    admin_id: int,
    action: str,
) -> tuple[bool, str]:
    if action not in {"keep", "remove", "revoke"}:
        return False, "Неизвестное действие."

    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM thanks WHERE id=$1 FOR UPDATE",
                thanks_id,
            )
            if not row or row["include_in_digest"] != 1:
                return False, "Благодарность не найдена."

            digest_status = await conn.fetchval(
                "SELECT status FROM digest_runs WHERE cycle_key=$1",
                row["cycle_key"],
            )
            if digest_status == "published":
                return False, "Этот выпуск уже опубликован."
            if digest_status == "approved":
                return False, "Сначала верни выпуск в модерацию."

            if action == "keep":
                await conn.execute("""
                    UPDATE thanks
                    SET public_status='approved',
                        moderated_by=$2,
                        moderated_at=NOW(),
                        moderation_reason=NULL
                    WHERE id=$1
                """, thanks_id, admin_id)
                audit_action = "keep_digest_item"
            elif action == "remove":
                await conn.execute("""
                    UPDATE thanks
                    SET public_status='removed',
                        moderated_by=$2,
                        moderated_at=NOW(),
                        moderation_reason='admin_removed'
                    WHERE id=$1
                """, thanks_id, admin_id)
                audit_action = "remove_digest_item"
            else:
                await conn.execute("""
                    UPDATE thanks
                    SET public_status='removed',
                        personal_status=CASE
                            WHEN personal_status='pending' THEN 'cancelled'
                            ELSE personal_status
                        END,
                        moderated_by=$2,
                        moderated_at=NOW(),
                        moderation_reason='admin_removed_and_revoked'
                    WHERE id=$1
                """, thanks_id, admin_id)

                await conn.execute("""
                    UPDATE users
                    SET status='access_denied',
                        active=0,
                        role='user',
                        removed_by_admin=1,
                        confirmed_cycle=NULL,
                        access_denied_at=NOW(),
                        access_denied_reason='thanks_moderation_abuse',
                        updated_at=NOW()
                    WHERE user_id=$1
                """, row["sender_id"])
                audit_action = "remove_digest_item_and_revoke"

            await conn.execute("""
                INSERT INTO admin_audit_log (
                    admin_user_id, action, target_user_id,
                    object_type, object_id, metadata, created_at
                )
                VALUES ($1, $2, $3, 'thanks', $4, '{}'::jsonb, NOW())
            """, admin_id, audit_action, row["sender_id"], thanks_id)

    if action == "revoke":
        await refresh_admin_cache()

    if action == "keep":
        return True, "Оставлено в пятничных хайлайтах."
    if action == "remove":
        return True, "Убрано из дайджеста. Личная благодарность останется."
    return True, "Убрано из дайджеста, личная доставка отменена, автору закрыт доступ."


@router.callback_query(F.data.startswith("thanks_mod_keep:"))
async def thanks_mod_keep_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    thanks_id = int(callback.data.split(":", 1)[1])
    ok, text = await moderate_public_thanks(thanks_id, callback.from_user.id, "keep")
    if ok:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("thanks_mod_remove:"))
async def thanks_mod_remove_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    thanks_id = int(callback.data.split(":", 1)[1])
    ok, text = await moderate_public_thanks(thanks_id, callback.from_user.id, "remove")
    if ok:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("thanks_mod_revoke:"))
async def thanks_mod_revoke_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    thanks_id = int(callback.data.split(":", 1)[1])
    ok, text = await moderate_public_thanks(thanks_id, callback.from_user.id, "revoke")
    if ok:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text)
    await callback.answer()


async def get_pending_public_moderation(cycle_key: str):
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT
                t.id, t.public_text, t.message_text,
                sender.display_name AS sender_name_current,
                recipient.display_name AS recipient_name_current
            FROM thanks t
            JOIN users sender ON sender.user_id=t.sender_id
            JOIN users recipient ON recipient.user_id=t.recipient_id
            WHERE t.cycle_key=$1
              AND t.include_in_digest=1
              AND t.public_status='pending'
            ORDER BY t.created_at, t.id
        """, cycle_key)


async def get_public_moderation_queue(cycle_key: str):
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT
                t.id, t.public_text, t.message_text, t.public_status,
                sender.display_name AS sender_name_current,
                recipient.display_name AS recipient_name_current
            FROM thanks t
            JOIN users sender ON sender.user_id=t.sender_id
            JOIN users recipient ON recipient.user_id=t.recipient_id
            WHERE t.cycle_key=$1
              AND t.include_in_digest=1
              AND t.public_status IN ('pending', 'approved')
            ORDER BY t.created_at, t.id
        """, cycle_key)


async def send_pending_moderation_queue(target: Message, cycle_key: str) -> None:
    rows = await get_public_moderation_queue(cycle_key)
    if not rows:
        await target.answer("В публичном выпуске сейчас нет элементов для модерации.")
        return

    pending_count = sum(1 for row in rows if row["public_status"] == "pending")
    await target.answer(
        f"Публичных благодарностей: {len(rows)}. "
        f"Ещё ждут решения: {pending_count}."
    )
    for row in rows:
        status_label = "ждёт решения" if row["public_status"] == "pending" else "оставлена"
        await target.answer(
            "Получатель: "
            f"{clean_name(row['recipient_name_current'])}\n"
            f"Автор: {clean_name(row['sender_name_current'])}\n"
            f"Статус: {status_label}\n\n"
            f"«{row['public_text'] or truncate_public_thanks(row['message_text'])}»",
            reply_markup=thanks_moderation_keyboard(row["id"]),
        )


async def reconcile_pending_thanks(cycle_key: str) -> None:
    """Cancels undeliverable pending thanks without exposing private content."""
    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Personal delivery is cancelled only if it has not happened yet.
            await conn.execute("""
                UPDATE thanks t
                SET personal_status='cancelled'
                FROM users recipient
                WHERE t.cycle_key=$1
                  AND t.recipient_id=recipient.user_id
                  AND t.personal_status='pending'
                  AND (
                      recipient.status <> 'active'
                      OR recipient.active <> 1
                      OR recipient.deleted_at IS NOT NULL
                      OR recipient.removed_by_admin=1
                  )
            """, cycle_key)

            # Public mention is removed whenever the recipient becomes unavailable,
            # even if the private message had already been delivered.
            await conn.execute("""
                UPDATE thanks t
                SET public_status='removed',
                    moderation_reason='recipient_unavailable',
                    moderated_at=NOW()
                FROM users recipient
                WHERE t.cycle_key=$1
                  AND t.recipient_id=recipient.user_id
                  AND t.public_status IN ('pending', 'approved')
                  AND (
                      recipient.status <> 'active'
                      OR recipient.active <> 1
                      OR recipient.deleted_at IS NOT NULL
                      OR recipient.removed_by_admin=1
                  )
            """, cycle_key)

            await conn.execute("""
                UPDATE thanks t
                SET personal_status='cancelled'
                WHERE t.cycle_key=$1
                  AND t.personal_status='pending'
                  AND EXISTS (
                      SELECT 1
                      FROM thanks_blocks b
                      WHERE b.blocker_id=t.recipient_id
                        AND b.blocked_sender_id=t.sender_id
                        AND b.unblocked_at IS NULL
                  )
            """, cycle_key)

            await conn.execute("""
                UPDATE thanks t
                SET public_status='removed',
                    moderation_reason='recipient_blocked_sender',
                    moderated_at=NOW()
                WHERE t.cycle_key=$1
                  AND t.public_status IN ('pending', 'approved')
                  AND EXISTS (
                      SELECT 1
                      FROM thanks_blocks b
                      WHERE b.blocker_id=t.recipient_id
                        AND b.blocked_sender_id=t.sender_id
                        AND b.unblocked_at IS NULL
                  )
            """, cycle_key)


async def ensure_digest_run(cycle_key: str) -> str:
    assert pool is not None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO digest_runs (
                cycle_key, status, created_at, updated_at
            )
            VALUES ($1, 'draft', NOW(), NOW())
            ON CONFLICT (cycle_key)
            DO UPDATE SET updated_at=NOW()
            RETURNING status
        """, cycle_key)
    return row["status"]


async def get_digest_status(cycle_key: str) -> str | None:
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT status FROM digest_runs WHERE cycle_key=$1",
            cycle_key,
        )


async def build_public_digest_v2(cycle_key: str) -> str | None:
    await reconcile_pending_thanks(cycle_key)

    assert pool is not None
    async with pool.acquire() as conn:
        rows = list(await conn.fetch("""
            SELECT
                t.id,
                t.public_text,
                t.message_text,
                recipient.display_name,
                recipient.team
            FROM thanks t
            JOIN users recipient ON recipient.user_id=t.recipient_id
            WHERE t.cycle_key=$1
              AND t.include_in_digest=1
              AND t.public_status='approved'
              AND recipient.status='active'
              AND recipient.active=1
              AND recipient.deleted_at IS NULL
              AND recipient.removed_by_admin=0
            ORDER BY t.id
        """, cycle_key))

    if not rows:
        return None

    rng = random.Random(f"thanks-digest:{cycle_key}")
    rng.shuffle(rows)

    parts = [
        "Пятничные хайлайты спасибо 🤍\n\n"
        "Собрал благодарности, которые коллегам было важно подсветить всей команде."
    ]
    for row in rows:
        recipient_title = clean_name(row["display_name"])
        label = team_label(row["team"])
        if label:
            recipient_title = f"{recipient_title} · {label}"

        public_text = row["public_text"] or truncate_public_thanks(row["message_text"])
        parts.append(
            f"Говорим спасибо: {recipient_title}\n"
            f"«{public_text}»"
        )

    return "\n\n".join(parts)


async def prepare_friday_highlights(cycle_key: str) -> None:
    await reconcile_pending_thanks(cycle_key)
    status = await ensure_digest_run(cycle_key)

    assert pool is not None
    async with pool.acquire() as conn:
        public_count = await conn.fetchval("""
            SELECT COUNT(*)
            FROM thanks
            WHERE cycle_key=$1
              AND include_in_digest=1
              AND public_status IN ('pending', 'approved')
        """, cycle_key)

    try:
        await safe_send_message(
            ADMIN_ID,
            "Пятничные хайлайты готовы\n\n"
            f"В общий дайджест собрал {public_count} благодарностей.\n"
            "До публикации можно посмотреть выпуск и обработать всё, что ещё ждёт модерации.",
            reply_markup=digest_review_keyboard(cycle_key, status),
        )
    except Exception:
        logging.exception("Не удалось отправить админу пятничный preview")


async def approve_digest(cycle_key: str, admin_id: int) -> tuple[bool, str]:
    await reconcile_pending_thanks(cycle_key)
    pending = await get_pending_public_moderation(cycle_key)
    if pending:
        return False, f"Сначала обработай модерацию: осталось {len(pending)}."

    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM digest_runs WHERE cycle_key=$1 FOR UPDATE",
                cycle_key,
            )
            if status is None:
                await conn.execute("""
                    INSERT INTO digest_runs (
                        cycle_key, status, approved_by, approved_at,
                        created_at, updated_at
                    )
                    VALUES ($1, 'approved', $2, NOW(), NOW(), NOW())
                """, cycle_key, admin_id)
            elif status == "published":
                return False, "Выпуск уже опубликован."
            else:
                await conn.execute("""
                    UPDATE digest_runs
                    SET status='approved',
                        approved_by=$2,
                        approved_at=NOW(),
                        updated_at=NOW()
                    WHERE cycle_key=$1
                """, cycle_key, admin_id)

            await conn.execute("""
                INSERT INTO admin_audit_log (
                    admin_user_id, action, object_type, object_id,
                    metadata, created_at
                )
                VALUES (
                    $1, 'approve_digest', 'digest', NULL,
                    jsonb_build_object('cycle_key', $2::text), NOW()
                )
            """, admin_id, cycle_key)

    return True, "Готово к публикации."


async def return_digest_to_moderation(cycle_key: str, admin_id: int) -> tuple[bool, str]:
    if admin_id != ADMIN_ID:
        return False, "Вернуть выпуск в модерацию может только владелец."

    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            status = await conn.fetchval(
                "SELECT status FROM digest_runs WHERE cycle_key=$1 FOR UPDATE",
                cycle_key,
            )
            if status == "published":
                return False, "Выпуск уже опубликован."
            if status != "approved":
                return False, "Выпуск уже в модерации."

            await conn.execute("""
                UPDATE digest_runs
                SET status='draft',
                    returned_to_moderation_by=$2,
                    returned_to_moderation_at=NOW(),
                    updated_at=NOW()
                WHERE cycle_key=$1
            """, cycle_key, admin_id)

            await conn.execute("""
                INSERT INTO admin_audit_log (
                    admin_user_id, action, object_type, object_id,
                    metadata, created_at
                )
                VALUES (
                    $1, 'return_digest_to_moderation', 'digest', NULL,
                    jsonb_build_object('cycle_key', $2::text), NOW()
                )
            """, admin_id, cycle_key)

    return True, "Выпуск снова открыт для модерации."


async def send_personal_thanks(cycle_key: str) -> None:
    await reconcile_pending_thanks(cycle_key)

    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT t.*
            FROM thanks t
            JOIN users recipient ON recipient.user_id=t.recipient_id
            WHERE t.cycle_key=$1
              AND t.personal_status='pending'
              AND recipient.status='active'
              AND recipient.active=1
              AND recipient.deleted_at IS NULL
              AND recipient.removed_by_admin=0
            ORDER BY t.created_at, t.id
        """, cycle_key)

    failures = 0
    for row in rows:
        if await is_thanks_blocked(row["recipient_id"], row["sender_id"]):
            assert pool is not None
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE thanks
                    SET personal_status='cancelled',
                        public_status=CASE
                            WHEN public_status IN ('pending', 'approved') THEN 'removed'
                            ELSE public_status
                        END,
                        moderation_reason=CASE
                            WHEN public_status IN ('pending', 'approved') THEN 'recipient_blocked_sender'
                            ELSE moderation_reason
                        END
                    WHERE id=$1
                """, row["id"])
            continue

        try:
            await safe_send_message(
                row["recipient_id"],
                f"Тебе спасибо от {clean_name(row['sender_name'])} 🤍\n\n"
                f"«{row['message_text']}»",
                reply_markup=thanks_block_keyboard(row["id"]),
            )
            assert pool is not None
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE thanks
                    SET personal_status='delivered',
                        personal_delivered_at=COALESCE(personal_delivered_at, NOW())
                    WHERE id=$1
                      AND personal_status='pending'
                """, row["id"])
        except TelegramForbiddenError:
            await deactivate_unreachable_user(row["recipient_id"])
            assert pool is not None
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE thanks
                    SET personal_status='failed'
                    WHERE id=$1
                      AND personal_status='pending'
                """, row["id"])
        except Exception:
            failures += 1
            logging.exception("Не удалось доставить личную благодарность %s", row["id"])

    if failures:
        raise RuntimeError(f"Не доставлено личных благодарностей: {failures}")


async def get_digest_recipients():
    assert pool is not None
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT user_id
            FROM users
            WHERE status='active'
              AND active=1
              AND deleted_at IS NULL
              AND removed_by_admin=0
              AND delivery_available=1
            ORDER BY user_id
        """)


async def publish_approved_digest(cycle_key: str) -> bool:
    status = await get_digest_status(cycle_key)
    if status != "approved":
        return False

    digest = await build_public_digest_v2(cycle_key)
    if not digest:
        assert pool is not None
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE digest_runs
                SET status='published',
                    published_at=NOW(),
                    updated_at=NOW()
                WHERE cycle_key=$1
                  AND status='approved'
            """, cycle_key)
        return True

    chunks = split_long_text(digest)
    recipients = await get_digest_recipients()
    failures = 0

    for recipient in recipients:
        for part_no, chunk in enumerate(chunks, start=1):
            assert pool is not None
            async with pool.acquire() as conn:
                delivered = await conn.fetchval("""
                    SELECT 1
                    FROM digest_deliveries
                    WHERE cycle_key=$1
                      AND user_id=$2
                      AND part_no=$3
                """, cycle_key, recipient["user_id"], part_no)
            if delivered:
                continue

            try:
                await safe_send_message(recipient["user_id"], chunk)
                async with pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO digest_deliveries (
                            cycle_key, user_id, part_no, delivered_at
                        )
                        VALUES ($1, $2, $3, NOW())
                        ON CONFLICT (cycle_key, user_id, part_no) DO NOTHING
                    """, cycle_key, recipient["user_id"], part_no)
            except TelegramForbiddenError:
                await deactivate_unreachable_user(recipient["user_id"])
                break
            except Exception:
                failures += 1
                logging.exception(
                    "Не удалось доставить часть дайджеста пользователю %s",
                    recipient["user_id"],
                )
                break

    if failures:
        raise RuntimeError(f"Не доставлено частей дайджеста: {failures}")

    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                UPDATE digest_runs
                SET status='published',
                    published_at=NOW(),
                    updated_at=NOW()
                WHERE cycle_key=$1
                  AND status='approved'
            """, cycle_key)

            await conn.execute("""
                UPDATE thanks
                SET public_status='published',
                    digest_delivered_at=COALESCE(digest_delivered_at, NOW())
                WHERE cycle_key=$1
                  AND public_status='approved'
            """, cycle_key)

    return True


async def friday_delivery(cycle_key: str) -> None:
    personal_error = None
    try:
        await send_personal_thanks(cycle_key)
    except Exception as exc:
        personal_error = exc
        logging.exception("Ошибка личной доставки благодарностей")

    try:
        await publish_approved_digest(cycle_key)
    except Exception:
        logging.exception("Ошибка публикации пятничных хайлайтов")
        raise

    if personal_error is not None:
        raise personal_error


@router.callback_query(F.data.startswith("digest_preview:"))
async def digest_preview_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    cycle_key = callback.data.split(":", 1)[1]
    digest = await build_public_digest_v2(cycle_key)
    if not digest:
        await callback.message.answer("Для общего дайджеста пока нет одобренных благодарностей.")
    else:
        for chunk in split_long_text("ПРЕДПРОСМОТР\n\n" + digest):
            await callback.message.answer(chunk)
    await callback.answer()


@router.callback_query(F.data.startswith("digest_moderate:"))
async def digest_moderate_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    cycle_key = callback.data.split(":", 1)[1]
    await send_pending_moderation_queue(callback.message, cycle_key)
    await callback.answer()


@router.callback_query(F.data.startswith("digest_approve:"))
async def digest_approve_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    cycle_key = callback.data.split(":", 1)[1]
    ok, text = await approve_digest(cycle_key, callback.from_user.id)
    await callback.message.answer(text)
    if ok:
        now = datetime.now(TZ)
        if (
            cycle_key == current_cycle(now)
            and now.weekday() == THANKS_DIGEST_WEEKDAY
            and now.hour >= THANKS_DIGEST_HOUR
        ):
            try:
                published = await publish_approved_digest(cycle_key)
                if published:
                    await callback.message.answer("Пятничные хайлайты опубликованы.")
            except Exception:
                await callback.message.answer(
                    "Выпуск одобрен, но публикация не завершилась. Проверь Railway Logs."
                )
    await callback.answer()


@router.callback_query(F.data.startswith("digest_return:"))
async def digest_return_callback(callback: CallbackQuery) -> None:
    cycle_key = callback.data.split(":", 1)[1]
    ok, text = await return_digest_to_moderation(
        cycle_key,
        callback.from_user.id,
    )
    await callback.message.answer(text)
    await callback.answer()


# =========================================================
# БЛАГОДАРНОСТИ: НАПОМИНАНИЕ И АДМИНСКИЕ ШОРТКАТЫ
# =========================================================
async def send_thanks_reminder(cycle_key: str, delivery_job: str = "thanks_reminder") -> None:
    await send_broadcast(
        delivery_job,
        cycle_key,
        "Кажется, сегодня хороший день, чтобы кому-нибудь сказать спасибо.\n\n"
        "Предлагаю отправить его сейчас, а в пятницу я доставлю :)",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="Поблагодарить коллегу",
                    callback_data="thanks_start",
                )]
            ]
        ),
    )


@router.message(Command("thanks_digest_preview"))
async def thanks_digest_preview_command(message: Message) -> None:
    if not await require_admin(message):
        return

    cycle_key = current_cycle()
    await ensure_digest_run(cycle_key)
    digest = await build_public_digest_v2(cycle_key)

    if not digest:
        await message.answer("Для общего дайджеста пока нет одобренных благодарностей.")
    else:
        for chunk in split_long_text("ПРЕДПРОСМОТР\n\n" + digest):
            await message.answer(chunk)

    pending = await get_pending_public_moderation(cycle_key)
    if pending:
        await message.answer(
            f"Ещё ждут модерации: {len(pending)}.",
            reply_markup=digest_review_keyboard(
                cycle_key,
                await get_digest_status(cycle_key) or "draft",
            ),
        )


@router.message(Command("thanks_digest_now"))
async def thanks_digest_now_command(message: Message) -> None:
    if not await require_admin(message):
        return

    cycle_key = current_cycle()
    try:
        await friday_delivery(cycle_key)
        status = await get_digest_status(cycle_key)
        if status == "published":
            await message.answer("Личные благодарности доставлены, дайджест опубликован.")
        else:
            await message.answer(
                "Личные благодарности доставлены. "
                "Дайджест ждёт одобрения перед публикацией."
            )
    except Exception:
        await message.answer(
            "Рассылка завершилась не полностью. Проверь Railway Logs."
        )


# =========================================================
# АДМИНИСТРАТОР V2: РОЛИ, ДАШБОРД, ПОЛЬЗОВАТЕЛИ И АУДИТ
# =========================================================
ADMIN_STATUS_LABELS = {
    "active": "активен",
    "paused": "пауза",
    "pending_activation": "ждёт активации",
    "access_denied": "доступ закрыт",
    "self_deleted": "удалил профиль",
}


def admin_status_label(user: asyncpg.Record | dict) -> str:
    return ADMIN_STATUS_LABELS.get(user["status"], user["status"] or "неизвестно")


def admin_home_keyboard(owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Пользователи", callback_data="admin_users_menu")],
        [InlineKeyboardButton(text="Ждут активации", callback_data="admin_pending_menu")],
        [InlineKeyboardButton(text="Активность недели", callback_data="admin_week_activity")],
        [InlineKeyboardButton(text="Блокировки", callback_data="admin_blocks_menu")],
        [InlineKeyboardButton(text="Пятничные хайлайты", callback_data="admin_highlights_menu")],
        [InlineKeyboardButton(text="Доступ закрыт", callback_data="admin_denied_menu")],
        [InlineKeyboardButton(text="Удалили профиль", callback_data="admin_deleted_menu")],
    ]
    if owner:
        rows.extend([
            [InlineKeyboardButton(text="Администраторы", callback_data="admin_roles_menu")],
            [InlineKeyboardButton(text="Аудит", callback_data="admin_audit_menu")],
            [InlineKeyboardButton(text="Состояние бота", callback_data="admin_bot_state")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_admin_dashboard_counts(cycle_key: str) -> dict:
    assert pool is not None
    async with pool.acquire() as conn:
        return {
            "active": await conn.fetchval("SELECT COUNT(*) FROM users WHERE status='active' AND active=1 AND deleted_at IS NULL AND removed_by_admin=0"),
            "paused": await conn.fetchval("SELECT COUNT(*) FROM users WHERE status='paused' AND deleted_at IS NULL"),
            "pending": await conn.fetchval("SELECT COUNT(*) FROM users WHERE status='pending_activation' AND onboarding_step='completed'"),
            "denied": await conn.fetchval("SELECT COUNT(*) FROM users WHERE status='access_denied'"),
            "deleted": await conn.fetchval("SELECT COUNT(*) FROM users WHERE status='self_deleted' OR deleted_at IS NOT NULL"),
            "ristretto": await conn.fetchval("SELECT COUNT(*) FROM weekly_participation WHERE cycle_key=$1 AND response='yes'", cycle_key),
            "thanks": await conn.fetchval("SELECT COUNT(*) FROM thanks WHERE cycle_key=$1", cycle_key),
            "public": await conn.fetchval("SELECT COUNT(*) FROM thanks WHERE cycle_key=$1 AND include_in_digest=1 AND public_status NOT IN ('removed','cancelled')", cycle_key),
            "new_blocks": await conn.fetchval("SELECT COUNT(*) FROM thanks_blocks WHERE created_at >= date_trunc('week', NOW())"),
        }


async def send_admin_home(message: Message, user_id: int) -> None:
    cycle_key = current_cycle()
    counts = await get_admin_dashboard_counts(cycle_key)
    role = "owner" if is_owner(user_id) else "admin"
    await message.answer(
        "Random Ristretto · админка\n\n"
        f"Роль: {role}\n"
        f"Неделя: {cycle_key}\n"
        f"Активных: {counts['active']} · на паузе: {counts['paused']}\n"
        f"Ждут активации: {counts['pending']} · доступ закрыт: {counts['denied']}\n"
        f"Ristretto: {counts['ristretto']} участников\n"
        f"Благодарности: {counts['thanks']} · публичные: {counts['public']}\n"
        f"Новых блокировок: {counts['new_blocks']}",
        reply_markup=admin_home_keyboard(is_owner(user_id)),
    )


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not await require_admin(message):
        return
    await send_admin_home(message, message.from_user.id)


@router.callback_query(F.data == "admin_home")
async def admin_home_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await send_admin_home(callback.message, callback.from_user.id)
    await callback.answer()


async def get_admin_user_metrics(user_id: int, cycle_key: str) -> dict:
    assert pool is not None
    async with pool.acquire() as conn:
        return {
            "this_week": await conn.fetchval(
                "SELECT response FROM weekly_participation WHERE user_id=$1 AND cycle_key=$2",
                user_id, cycle_key,
            ),
            "weeks": await conn.fetchval(
                "SELECT COUNT(*) FROM weekly_participation WHERE user_id=$1 AND response='yes'",
                user_id,
            ),
            "thanks_week": await conn.fetchval(
                "SELECT COUNT(*) FROM thanks WHERE sender_id=$1 AND cycle_key=$2",
                user_id, cycle_key,
            ),
            "thanks_total": await conn.fetchval(
                "SELECT COUNT(*) FROM thanks WHERE sender_id=$1",
                user_id,
            ),
            "active_blocks": await conn.fetchval(
                "SELECT COUNT(*) FROM thanks_blocks WHERE blocked_sender_id=$1 AND unblocked_at IS NULL",
                user_id,
            ),
        }


async def send_admin_user_card(message: Message, user_id: int, viewer_id: int) -> None:
    user = await get_user(user_id)
    if not user:
        await message.answer("Пользователь не найден.")
        return

    cycle_key = current_cycle()
    metrics = await get_admin_user_metrics(user_id, cycle_key)
    username = f"@{user['username']}" if user["username"] else "username отсутствует"
    team = team_label(user["team"]) or "другая / не указана"
    responsibility = (user["responsibility_text"] or "").strip() or "не заполнено"
    created = user["created_at"].strftime("%d.%m.%Y") if user["created_at"] else "—"
    response = {"yes": "участвует", "no": "пропускает", None: "не ответил"}.get(metrics["this_week"], "—")

    buttons = []
    if user["status"] == "access_denied":
        buttons.append([InlineKeyboardButton(text="Вернуть доступ", callback_data=f"admin_restore:{user_id}")])
    elif user_id != ADMIN_ID and user["status"] not in {"self_deleted"}:
        buttons.append([InlineKeyboardButton(text="Закрыть доступ", callback_data=f"admin_remove:{user_id}")])

    if is_owner(viewer_id) and user_id != ADMIN_ID and user["status"] == "active" and user["active"] == 1:
        if user["role"] == "admin":
            buttons.append([InlineKeyboardButton(text="Убрать роль администратора", callback_data=f"admin_role_remove:{user_id}")])
        elif user["role"] == "user":
            buttons.append([InlineKeyboardButton(text="Сделать администратором", callback_data=f"admin_role_add:{user_id}")])

    await message.answer(
        "Карточка пользователя\n\n"
        f"{profile_title(user)}\n"
        f"Telegram: {username}\n"
        f"Отвечает за: {responsibility}\n"
        f"Команда: {team}\n"
        f"Статус: {admin_status_label(user)}\n"
        f"Роль: {user['role']}\n"
        f"Регистрация: {created}\n\n"
        f"Эта неделя: {response}\n"
        f"Участий в Ristretto: {metrics['weeks']}\n"
        f"Спасибо отправлено: {metrics['thanks_week']} на этой неделе / {metrics['thanks_total']} всего\n"
        f"Активных блокировок против пользователя: {metrics['active_blocks']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )


async def send_users_list(message: Message, statuses: tuple[str, ...] | None = None) -> None:
    assert pool is not None
    async with pool.acquire() as conn:
        if statuses:
            users = await conn.fetch("""
                SELECT * FROM users
                WHERE status = ANY($1::text[])
                ORDER BY LOWER(COALESCE(display_name, username, '')), user_id
            """, list(statuses))
        else:
            users = await conn.fetch("""
                SELECT * FROM users
                WHERE status <> 'self_deleted'
                ORDER BY LOWER(COALESCE(display_name, username, '')), user_id
            """)

    if not users:
        await message.answer("В этом разделе пока никого нет.")
        return

    rows = [
        [InlineKeyboardButton(
            text=f"{clean_name(user['display_name'])} · {admin_status_label(user)}"[:60],
            callback_data=f"admin_user:{user['user_id']}",
        )]
        for user in users
    ]
    rows.append([InlineKeyboardButton(text="← В админку", callback_data="admin_home")])
    await message.answer("Пользователи:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("users"))
async def users_command(message: Message) -> None:
    if not await require_admin(message):
        return
    await send_users_list(message)


@router.callback_query(F.data == "admin_users_menu")
async def admin_users_menu_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await send_users_list(callback.message)
    await callback.answer()


@router.callback_query(F.data == "admin_pending_menu")
async def admin_pending_menu_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    users = await get_pending_activation_users()
    if not users:
        await callback.message.answer("Сейчас никто не ждёт активации.")
    else:
        for user in users:
            username = f"@{user['username']}" if user["username"] else "username отсутствует"
            team = team_label(user["team"]) or "Другая команда"
            await callback.message.answer(
                "Профиль ждёт активации\n\n"
                f"{clean_name(user['display_name'])}\n"
                f"Команда: {team}\n"
                f"Отвечает за: {(user['responsibility_text'] or '').strip()}\n"
                f"Telegram: {username}",
                reply_markup=pending_activation_keyboard(user["user_id"]),
            )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_user:"))
async def admin_user_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    user_id = int(callback.data.split(":", 1)[1])
    await send_admin_user_card(callback.message, user_id, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_remove:"))
async def admin_remove_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":", 1)[1])
    user = await get_user(user_id)
    if not user or user["status"] == "self_deleted":
        await callback.answer("Пользователь недоступен.", show_alert=True)
        return
    if user_id == ADMIN_ID:
        await callback.answer("Нельзя закрыть доступ владельцу.", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, закрыть доступ", callback_data=f"admin_remove_confirm:{user_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin_remove_cancel")],
    ])
    await callback.message.answer(
        f"Закрыть доступ для {clean_name(user['display_name'])}?",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_remove_confirm:"))
async def admin_remove_confirm_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":", 1)[1])
    removed = await remove_user_by_admin(user_id)
    if removed:
        await audit_admin_action(callback.from_user.id, "revoke_access", user_id, "user", user_id)
        await refresh_admin_cache()
        await callback.message.edit_text("Доступ пользователю закрыт.")
        try:
            await safe_send_message(user_id, access_denied_message())
        except Exception:
            logging.exception("Не удалось сообщить пользователю о закрытии доступа")
    else:
        await callback.message.edit_text("Не удалось закрыть доступ пользователю.")
    await callback.answer()


@router.callback_query(F.data == "admin_remove_cancel")
async def admin_remove_cancel_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text("Действие отменено.")
    await callback.answer()


async def restore_denied_user(user_id: int) -> bool:
    assert pool is not None
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE users
            SET status='pending_activation',
                active=0,
                removed_by_admin=0,
                name_confirmed=0,
                team=NULL,
                responsibility_text=NULL,
                onboarding_step='name',
                profile_migration_required=0,
                access_denied_reason='admin_restore_pending',
                access_denied_at=NULL,
                updated_at=NOW()
            WHERE user_id=$1
              AND status='access_denied'
        """, user_id)
    return result.endswith("1")


@router.callback_query(F.data.startswith("admin_restore:"))
async def admin_restore_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":", 1)[1])
    restored = await restore_denied_user(user_id)
    if not restored:
        await callback.answer("Статус уже изменился.", show_alert=True)
        return

    await audit_admin_action(callback.from_user.id, "restore_access", user_id, "user", user_id)
    await callback.message.answer("Пользователь переведён на восстановление доступа.")
    try:
        await safe_send_message(
            user_id,
            "Ты можешь вернуться к Random Ristretto.\n\n"
            "Для начала обновим профиль — всего три коротких шага.\n\n"
            "Отправь /start.",
        )
    except Exception:
        logging.exception("Не удалось сообщить пользователю о восстановлении доступа")
    await callback.answer()


@router.callback_query(F.data == "admin_denied_menu")
async def admin_denied_menu_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await send_users_list(callback.message, ("access_denied",))
    await callback.answer()


@router.callback_query(F.data == "admin_deleted_menu")
async def admin_deleted_menu_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    assert pool is not None
    async with pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT user_id, display_name, deleted_at
            FROM users
            WHERE status='self_deleted' OR deleted_at IS NOT NULL
            ORDER BY deleted_at DESC NULLS LAST, user_id
        """)
    if not users:
        await callback.message.answer("Никто не удалял профиль.")
    else:
        lines = ["Удалили профиль"]
        for user in users:
            when = user["deleted_at"].strftime("%d.%m.%Y") if user["deleted_at"] else "—"
            lines.append(f"• {clean_name(user['display_name'])} · {when}")
        await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "admin_week_activity")
async def admin_week_activity_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    cycle_key = current_cycle()
    stats = await get_stats(cycle_key)
    assert pool is not None
    async with pool.acquire() as conn:
        senders = await conn.fetchval("SELECT COUNT(DISTINCT sender_id) FROM thanks WHERE cycle_key=$1", cycle_key)
        removed = await conn.fetchval("SELECT COUNT(*) FROM thanks WHERE cycle_key=$1 AND public_status='removed'", cycle_key)
    await callback.message.answer(
        "Активность недели\n\n"
        f"Неделя: {cycle_key}\n"
        f"Ristretto: {stats['confirmed']} участников\n"
        f"Пар/троек: {stats['groups_week']}\n"
        f"Благодарности: {stats['thanks_week']} от {senders} пользователей\n"
        f"Публичные: {stats['thanks_public_week']}\n"
        f"Удалено модерацией из публичного выпуска: {removed}"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_blocks_menu")
async def admin_blocks_menu_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.user_id, u.display_name,
                   COUNT(*) FILTER (WHERE b.unblocked_at IS NULL) AS active_count,
                   COUNT(*) AS ever_count
            FROM thanks_blocks b
            JOIN users u ON u.user_id=b.blocked_sender_id
            GROUP BY u.user_id, u.display_name
            ORDER BY active_count DESC, ever_count DESC, LOWER(COALESCE(u.display_name, ''))
        """)
    if not rows:
        await callback.message.answer("Блокировок пока не было.")
    else:
        buttons = [[InlineKeyboardButton(
            text=f"{clean_name(row['display_name'])} · активных {row['active_count']} / всего {row['ever_count']}"[:60],
            callback_data=f"admin_blocks_user:{row['user_id']}",
        )] for row in rows]
        await callback.message.answer("Блокировки по отправителям:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_blocks_user:"))
async def admin_blocks_user_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    sender_id = int(callback.data.split(":", 1)[1])
    sender = await get_user(sender_id)
    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT blocker.display_name, b.created_at, b.unblocked_at
            FROM thanks_blocks b
            JOIN users blocker ON blocker.user_id=b.blocker_id
            WHERE b.blocked_sender_id=$1
            ORDER BY b.created_at DESC
        """, sender_id)
    lines = [f"Кто блокировал: {clean_name(sender['display_name']) if sender else sender_id}"]
    for row in rows:
        state = "активна" if row["unblocked_at"] is None else "снята"
        lines.append(f"• {clean_name(row['display_name'])} · {state}")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "admin_highlights_menu")
async def admin_highlights_menu_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    cycle_key = current_cycle()
    await ensure_digest_run(cycle_key)
    status = await get_digest_status(cycle_key) or "draft"
    pending = await get_pending_public_moderation(cycle_key)
    await callback.message.answer(
        f"Пятничные хайлайты · {cycle_key}\n\n"
        f"Статус: {status}\n"
        f"Ждут модерации: {len(pending)}",
        reply_markup=digest_review_keyboard(cycle_key, status),
    )
    await callback.answer()


async def set_user_role(target_user_id: int, role: str, actor_id: int) -> tuple[bool, str]:
    if role not in {"user", "admin"}:
        return False, "Неизвестная роль."
    if not is_owner(actor_id):
        return False, "Только владелец может менять администраторов."
    if target_user_id == ADMIN_ID:
        return False, "Роль владельца изменить нельзя."

    assert pool is not None
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1 FOR UPDATE", target_user_id)
            if not target:
                return False, "Пользователь не найден."
            if target["status"] != "active" or target["active"] != 1 or target["deleted_at"] is not None:
                return False, "Администратором можно сделать только активного пользователя."
            if target["role"] == role:
                return False, "Эта роль уже установлена."
            await conn.execute("UPDATE users SET role=$2, updated_at=NOW() WHERE user_id=$1", target_user_id, role)
            await conn.execute("""
                INSERT INTO admin_audit_log (
                    admin_user_id, action, target_user_id, object_type, object_id, metadata, created_at
                ) VALUES ($1, $2, $3, 'user', $3, '{}'::jsonb, NOW())
            """, actor_id, "add_admin" if role == "admin" else "remove_admin", target_user_id)
    await refresh_admin_cache()
    try:
        await configure_bot_commands()
    except Exception:
        logging.exception("Не удалось обновить меню команд после изменения роли")
    return True, "Роль администратора добавлена." if role == "admin" else "Роль администратора снята."


@router.callback_query(F.data == "admin_roles_menu")
async def admin_roles_menu_callback(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца.", show_alert=True)
        return
    assert pool is not None
    async with pool.acquire() as conn:
        admins = await conn.fetch("""
            SELECT user_id, display_name, role
            FROM users
            WHERE role IN ('owner', 'admin')
            ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, LOWER(COALESCE(display_name, ''))
        """)
        candidates = await conn.fetch("""
            SELECT user_id, display_name
            FROM users
            WHERE role='user' AND status='active' AND active=1 AND deleted_at IS NULL
            ORDER BY LOWER(COALESCE(display_name, ''))
        """)
    lines = ["Администраторы"] + [f"• {clean_name(row['display_name'])} · {row['role']}" for row in admins]
    buttons = [[InlineKeyboardButton(text=f"Добавить · {clean_name(row['display_name'])}"[:60], callback_data=f"admin_role_add:{row['user_id']}")] for row in candidates]
    await callback.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_role_add:"))
async def admin_role_add_callback(callback: CallbackQuery) -> None:
    target_id = int(callback.data.split(":", 1)[1])
    ok, text = await set_user_role(target_id, "admin", callback.from_user.id)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_role_remove:"))
async def admin_role_remove_callback(callback: CallbackQuery) -> None:
    target_id = int(callback.data.split(":", 1)[1])
    ok, text = await set_user_role(target_id, "user", callback.from_user.id)
    await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "admin_audit_menu")
async def admin_audit_menu_callback(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца.", show_alert=True)
        return
    assert pool is not None
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT a.action, a.created_at,
                   actor.display_name AS actor_name,
                   target.display_name AS target_name
            FROM admin_audit_log a
            LEFT JOIN users actor ON actor.user_id=a.admin_user_id
            LEFT JOIN users target ON target.user_id=a.target_user_id
            ORDER BY a.created_at DESC
            LIMIT 20
        """)
    if not rows:
        await callback.message.answer("Журнал действий пока пуст.")
    else:
        lines = ["Последние действия администраторов"]
        for row in rows:
            when = row["created_at"].astimezone(TZ).strftime("%d.%m %H:%M")
            actor = clean_name(row["actor_name"])
            target = f" · {clean_name(row['target_name'])}" if row["target_name"] else ""
            lines.append(f"• {when} · {actor} · {row['action']}{target}")
        await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "admin_bot_state")
async def admin_bot_state_callback(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Только для владельца.", show_alert=True)
        return
    assert pool is not None
    async with pool.acquire() as conn:
        jobs = await conn.fetch("""
            SELECT job_name, cycle_key, status, started_at, completed_at, error_text
            FROM scheduler_runs
            ORDER BY started_at DESC
            LIMIT 10
        """)
        unavailable = await conn.fetchval("SELECT COUNT(*) FROM users WHERE delivery_available=0")
    lines = ["Состояние бота", f"Недоступны для доставки: {unavailable}"]
    if not jobs:
        lines.append("Запусков scheduler пока нет.")
    else:
        lines.append("Последние задачи:")
        for row in jobs:
            lines.append(f"• {row['job_name']} · {row['cycle_key']} · {row['status']}")
    await callback.message.answer("\n".join(lines))
    await callback.answer()

# =========================================================
# АДМИНИСТРАТОР: СТАТИСТИКА И РУЧНЫЕ ЗАПУСКИ
# =========================================================
async def get_stats(cycle_key: str) -> dict:
    assert pool is not None
    async with pool.acquire() as conn:
        return {
            "registered": await conn.fetchval("""
                SELECT COUNT(*) FROM users WHERE deleted_at IS NULL
            """),
            "active": await conn.fetchval("""
                SELECT COUNT(*) FROM users
                WHERE active=1 AND deleted_at IS NULL AND removed_by_admin=0
            """),
            "confirmed": await conn.fetchval("""
                SELECT COUNT(*) FROM users
                WHERE active=1 AND deleted_at IS NULL AND confirmed_cycle=$1
            """, cycle_key),
            "removed": await conn.fetchval("""
                SELECT COUNT(*) FROM users WHERE deleted_at IS NOT NULL
            """),
            "groups_week": await conn.fetchval("""
                SELECT
                    (SELECT COUNT(*) FROM match_groups WHERE cycle_key=$1)
                    +
                    (SELECT COUNT(*) FROM meetings
                     WHERE created_at >= date_trunc('week', NOW()))
            """, cycle_key),
            "groups_total": await conn.fetchval("""
                SELECT
                    (SELECT COUNT(*) FROM match_groups)
                    +
                    (SELECT COUNT(*) FROM meetings)
            """),
            "thanks_week": await conn.fetchval("""
                SELECT COUNT(*) FROM thanks WHERE cycle_key=$1
            """, cycle_key),
            "thanks_public_week": await conn.fetchval("""
                SELECT COUNT(*) FROM thanks
                WHERE cycle_key=$1 AND include_in_digest=1
            """, cycle_key),
        }


@router.message(Command("stats"))
async def stats_command(message: Message) -> None:
    if not await require_admin(message):
        return

    cycle_key = current_cycle()
    stats = await get_stats(cycle_key)

    await message.answer(
        "Статистика Random Ristretto\n\n"
        f"Неделя: {cycle_key}\n"
        f"Зарегистрировано: {stats['registered']}\n"
        f"Активно: {stats['active']}\n"
        f"Подтвердили участие: {stats['confirmed']}\n"
        f"Удалено или отключено: {stats['removed']}\n"
        f"Пар/троек на этой неделе: {stats['groups_week']}\n"
        f"Пар/троек за всё время: {stats['groups_total']}\n"
        f"Благодарностей на этой неделе: {stats['thanks_week']}\n"
        f"Для общего дайджеста: {stats['thanks_public_week']}"
    )


@router.message(Command("checkin_now"))
async def checkin_now_command(message: Message) -> None:
    if not await require_admin(message):
        return

    cycle_key = current_cycle()
    manual_job = f"random_checkin_manual_{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}"
    await send_weekly_checkin(cycle_key, manual_job)
    await message.answer("Тестовый check-in отправлен активным пользователям.")


@router.message(Command("match_now"))
async def match_now_command(message: Message) -> None:
    if not await require_admin(message):
        return

    cycle_key = current_cycle()
    try:
        started = await run_job_once(
            "random_match",
            cycle_key,
            lambda: run_matching(cycle_key),
        )
        if started:
            await message.answer("Мэтчинг выполнен.")
        else:
            await message.answer("Мэтчинг за эту неделю уже запускался или сейчас выполняется.")
    except Exception:
        await message.answer("Во время мэтчинга произошла ошибка. Проверьте Railway Logs.")


async def send_profile_migration_requests() -> int:
    assert pool is not None
    job_name = "profile_migration_v2"
    cycle_key = "v2"
    async with pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT u.user_id
            FROM users u
            WHERE u.status='active'
              AND u.active=1
              AND u.deleted_at IS NULL
              AND u.removed_by_admin=0
              AND u.delivery_available=1
              AND u.profile_migration_required=1
              AND NOT EXISTS (
                  SELECT 1
                  FROM broadcast_deliveries d
                  WHERE d.job_name=$1
                    AND d.cycle_key=$2
                    AND d.user_id=u.user_id
              )
            ORDER BY u.user_id
        """, job_name, cycle_key)

    sent = 0
    for user in users:
        try:
            await safe_send_message(
                user["user_id"],
                "Random Ristretto немного подрос.\n\n"
                "Нас становится больше, поэтому теперь перед встречей я буду показывать "
                "не только имя коллеги, но и команду и пару слов о том, чем человек занимается.\n\n"
                "Нужно один раз дополнить профиль. Всего три коротких шага.",
                reply_markup=profile_migration_start_keyboard(),
            )
            await mark_broadcast_delivered(job_name, cycle_key, user["user_id"])
            sent += 1
        except TelegramForbiddenError:
            await deactivate_unreachable_user(user["user_id"])
        except Exception:
            logging.exception(
                "Не удалось отправить запрос профиля пользователю %s",
                user["user_id"],
            )

    return sent


@router.message(Command("request_profiles_now"))
async def request_profiles_now_command(message: Message) -> None:
    if not await require_admin(message):
        return

    sent = await send_profile_migration_requests()
    await message.answer(f"Запрос обновить профиль отправлен: {sent} пользователям.")


@router.message(Command("request_names_now"))
async def request_names_now_command(message: Message) -> None:
    # Старое имя команды оставляем как безопасный алиас на время перехода.
    if not await require_admin(message):
        return

    sent = await send_profile_migration_requests()
    await message.answer(f"Запрос обновить профиль отправлен: {sent} пользователям.")


@router.message(Command("thanks_reminder_now"))
async def thanks_reminder_now_command(message: Message) -> None:
    if not await require_admin(message):
        return

    cycle_key = await get_open_thanks_cycle()
    manual_job = f"thanks_reminder_manual_{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}"
    await send_thanks_reminder(cycle_key, manual_job)
    await message.answer("Напоминание о благодарностях отправлено.")


# =========================================================
# ОБРАБОТКА ТЕКСТА ВНУТРИ ДИАЛОГА
# =========================================================
@router.message()
async def flow_message_handler(message: Message) -> None:
    if not message.text:
        await message.answer("Пожалуйста, отправь текстовое сообщение.")
        return

    text = message.text.strip()
    user = await get_user(message.from_user.id)

    # Новый онбординг и одноразовая миграция профиля хранят текущий шаг в users,
    # поэтому переживают рестарт бота без потери прогресса.
    if user and (
        user["status"] == "pending_activation"
        or user["profile_migration_required"] == 1
    ):
        step = user["onboarding_step"]

        if step == "name":
            if len(text) < 2 or len(text) > 80 or "\n" in text:
                await message.answer(
                    "Имя должно содержать от 2 до 80 символов. Попробуй ещё раз."
                )
                return

            await set_display_name(message.from_user.id, text)
            await set_onboarding_step(message.from_user.id, "team")
            user = await get_user(message.from_user.id)
            await send_profile_step(message, user)
            return

        if step == "responsibility":
            if len(text) < 3:
                await message.answer(
                    "Напиши хотя бы несколько слов о том, за что ты отвечаешь."
                )
                return
            if len(text) > 150:
                await message.answer(
                    f"Получилось {len(text)} знаков. Здесь максимум 150 — сократи немного."
                )
                return

            await finish_profile_after_responsibility(
                message,
                message.from_user.id,
                text,
            )
            return

        if step in {"name_confirm", "team"}:
            await send_profile_step(message, user)
            return

    flow = await get_flow(message.from_user.id)

    if not flow:
        if user and user["status"] == "access_denied":
            await message.answer(access_denied_message())
            return

        await message.answer(user_help_text(user))
        return

    if flow["flow_type"] == "profile_edit" and flow["step"] == "waiting_name":
        if len(text) < 2 or len(text) > 80 or "\n" in text:
            await message.answer(
                "Имя должно содержать от 2 до 80 символов. Попробуй ещё раз."
            )
            return

        await set_display_name(message.from_user.id, text)
        await clear_flow(message.from_user.id)
        user = await get_user(message.from_user.id)
        await message.answer("Имя обновлено.")
        await show_profile(message, user)
        return

    if (
        flow["flow_type"] == "profile_edit"
        and flow["step"] == "waiting_responsibility"
    ):
        if len(text) < 3:
            await message.answer("Напиши хотя бы несколько слов.")
            return
        if len(text) > 150:
            await message.answer(
                f"Получилось {len(text)} знаков. Здесь максимум 150 — сократи немного."
            )
            return

        await set_responsibility_text(message.from_user.id, text)
        await clear_flow(message.from_user.id)
        user = await get_user(message.from_user.id)
        await message.answer("Описание обновлено.")
        await show_profile(message, user)
        return

    if flow["flow_type"] == "set_name" and flow["step"] == "waiting_name":
        # Совместимость со старым незавершённым диалогом изменения имени.
        if len(text) < 2 or len(text) > 80 or "\n" in text:
            await message.answer(
                "Имя должно содержать от 2 до 80 символов. Попробуй ещё раз."
            )
            return

        await set_display_name(message.from_user.id, text)
        await clear_flow(message.from_user.id)
        await message.answer(f"Имя сохранено: {text}")
        return

    if flow["flow_type"] == "thanks" and flow["step"] == "waiting_recipient_query":
        matches, matched_blocked = await search_thanks_recipients(
            message.from_user.id,
            text,
        )
        if not matches:
            if matched_blocked:
                await message.answer(
                    "Сейчас этому человеку нельзя отправить благодарность через бот."
                )
            else:
                await message.answer(
                    "Упс, такого человека не нашлось. Возможно, коллега ещё не знает "
                    "про Ristretto или имя в профиле записано немного иначе.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="Попробовать ещё раз",
                                callback_data="thanks_search_again",
                            )],
                            [InlineKeyboardButton(
                                text="Выбрать по команде",
                                callback_data="thanks_browse_teams",
                            )],
                            [InlineKeyboardButton(
                                text="Отмена",
                                callback_data="thanks_cancel",
                            )],
                        ]
                    ),
                )
            return

        if len(matches) == 1:
            recipient = matches[0]
            cycle_key = await get_open_thanks_cycle()
            await set_flow(
                message.from_user.id,
                "thanks",
                "waiting_text",
                cycle_key=cycle_key,
                recipient_id=recipient["user_id"],
            )
            await message.answer(
                f"Нашёл: {clean_name(recipient['display_name'])}.\n\n"
                "Напиши текст благодарности. Получатель увидит твоё имя "
                "в личном сообщении."
            )
            return

        await message.answer(
            "Нашлось несколько вариантов. Выбери коллегу:",
            reply_markup=thanks_search_results_keyboard(matches),
        )
        return

    if flow["flow_type"] == "thanks" and flow["step"] == "waiting_text":
        if len(text) < 3:
            await message.answer("Текст слишком короткий. Напиши хотя бы несколько слов.")
            return
        if len(text) > 3500:
            await message.answer("Текст должен быть не длиннее 3500 символов.")
            return

        await set_flow(
            message.from_user.id,
            "thanks",
            "choose_mode",
            cycle_key=flow["cycle_key"],
            recipient_id=flow["recipient_id"],
            draft_text=text,
        )
        await message.answer(
            "Как отправить спасибо?\n\n"
            "В любом случае получатель увидит твоё имя в личном сообщении.",
            reply_markup=thanks_visibility_keyboard(),
        )
        return

    await clear_flow(message.from_user.id)
    await message.answer("Предыдущий диалог сброшен. Начни снова нужной командой.")


# =========================================================
# ПЛАНИРОВЩИК: CATCH-UP ПОСЛЕ РЕСТАРТА
# =========================================================
def week_minute(moment: datetime) -> int:
    return moment.weekday() * 24 * 60 + moment.hour * 60 + moment.minute


def scheduler_new_run_due(job_name: str, moment: datetime) -> bool:
    """
    Window for a job that has never started in the current cycle.
    A restart after the exact scheduled minute therefore does not lose the job.
    Windows that would become confusing when very late are deliberately bounded.
    """
    minute = week_minute(moment)
    checkin_start = CHECKIN_WEEKDAY * 24 * 60 + CHECKIN_HOUR * 60
    match_start = MATCH_WEEKDAY * 24 * 60 + MATCH_HOUR * 60
    reminder_start = THANKS_REMINDER_WEEKDAY * 24 * 60 + THANKS_REMINDER_HOUR * 60
    review_start = THANKS_DIGEST_WEEKDAY * 24 * 60 + THANKS_REVIEW_HOUR * 60
    delivery_start = THANKS_DIGEST_WEEKDAY * 24 * 60 + THANKS_DIGEST_HOUR * 60

    if job_name == "random_checkin":
        return checkin_start <= minute < match_start
    if job_name == "random_match":
        return match_start <= minute < reminder_start
    if job_name == "thanks_reminder":
        return reminder_start <= minute < review_start
    if job_name == "thanks_digest_prepare":
        return minute >= review_start
    if job_name == "thanks_friday_delivery":
        return minute >= delivery_start
    return False


def scheduler_retry_due(job_name: str, moment: datetime) -> bool:
    """A failed/stale job may retry after its start, while check-in still closes at matching."""
    minute = week_minute(moment)
    checkin_start = CHECKIN_WEEKDAY * 24 * 60 + CHECKIN_HOUR * 60
    match_start = MATCH_WEEKDAY * 24 * 60 + MATCH_HOUR * 60
    reminder_start = THANKS_REMINDER_WEEKDAY * 24 * 60 + THANKS_REMINDER_HOUR * 60
    review_start = THANKS_DIGEST_WEEKDAY * 24 * 60 + THANKS_REVIEW_HOUR * 60
    delivery_start = THANKS_DIGEST_WEEKDAY * 24 * 60 + THANKS_DIGEST_HOUR * 60

    if job_name == "random_checkin":
        return checkin_start <= minute < match_start
    if job_name == "random_match":
        return minute >= match_start
    if job_name == "thanks_reminder":
        return reminder_start <= minute < review_start
    if job_name == "thanks_digest_prepare":
        return minute >= review_start
    if job_name == "thanks_friday_delivery":
        return minute >= delivery_start
    return False


async def scheduler_should_attempt(job_name: str, cycle_key: str, moment: datetime) -> bool:
    status = await get_job_status(job_name, cycle_key)
    if status == "completed":
        return False
    if status in {"failed", "running"}:
        return scheduler_retry_due(job_name, moment)
    return scheduler_new_run_due(job_name, moment)


async def run_due_scheduler_jobs(moment: datetime | None = None) -> None:
    now = moment or datetime.now(TZ)
    cycle_key = current_cycle(now)

    jobs = [
        ("random_checkin", lambda: send_weekly_checkin(cycle_key)),
        ("random_match", lambda: run_matching(cycle_key)),
        ("thanks_reminder", lambda: send_thanks_reminder(cycle_key)),
        ("thanks_digest_prepare", lambda: prepare_friday_highlights(cycle_key)),
        ("thanks_friday_delivery", lambda: friday_delivery(cycle_key)),
    ]

    for job_name, action in jobs:
        if await scheduler_should_attempt(job_name, cycle_key, now):
            await run_job_once(job_name, cycle_key, action)


async def scheduler_loop() -> None:
    while True:
        try:
            await run_due_scheduler_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка в планировщике")

        await asyncio.sleep(30)

# =========================================================
# ЗАПУСК
# =========================================================
async def main() -> None:
    print("Random Ristretto running")

    await init_db_pool()
    await init_db()
    await configure_bot_commands()

    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)

    scheduler_task = asyncio.create_task(scheduler_loop())

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task

        if pool is not None:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
