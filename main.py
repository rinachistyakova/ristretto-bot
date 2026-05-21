print("🔥 RUNNING FILE: MAIN.PY POSTGRES VERSION")

import os
import logging
import asyncio
import random
from datetime import datetime

from dotenv import load_dotenv

import asyncpg

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

# -----------------------------
# ENV
# -----------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL missing")

ADMIN_ID = 75734295

# -----------------------------
# LOGGING
# -----------------------------
logging.basicConfig(level=logging.INFO)

# -----------------------------
# BOT
# -----------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# -----------------------------
# DB POOL
# -----------------------------
pool = None


async def init_db_pool():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)


async def init_db():
    async with pool.acquire() as conn:

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

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS meetings (
            id SERIAL PRIMARY KEY,
            user1 BIGINT,
            user2 BIGINT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)

# -----------------------------
# USERS
# -----------------------------
async def add_user(user_id, username, first_name):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO users (user_id, username, first_name, active, confirmed, created_at)
        VALUES ($1, $2, $3, 1, 0, NOW())
        ON CONFLICT (user_id)
        DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name
        """, user_id, username, first_name)


async def set_active(user_id, active):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET active=$1 WHERE user_id=$2",
            active, user_id
        )


async def set_confirmed(user_id, confirmed):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET confirmed=$1 WHERE user_id=$2",
            confirmed, user_id
        )


async def reset_confirmations():
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET confirmed=0")


async def delete_user(user_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE user_id=$1", user_id)


async def get_all_users():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, username, first_name, active, confirmed FROM users")


async def get_confirmed_users():
    async with pool.acquire() as conn:
        return await conn.fetch("""
        SELECT user_id, username, first_name
        FROM users
        WHERE active=1 AND confirmed=1
        """)

# -----------------------------
# MEETINGS
# -----------------------------
async def save_meeting(u1, u2):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO meetings (user1, user2, created_at) VALUES ($1, $2, NOW())",
            u1, u2
        )


async def get_total_meetings():
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM meetings")


async def get_this_week_meetings():
    async with pool.acquire() as conn:
        return await conn.fetchval("""
        SELECT COUNT(*)
        FROM meetings
        WHERE created_at >= NOW() - INTERVAL '7 days'
        """)

# -----------------------------
# UX
# -----------------------------
ICEBREAKERS = [
    "Какой рабочий ритуал ты никому не отдашь?",
    "Какой проект дал неожиданный инсайт?",
    "Что сейчас в твоей сфере переоценено?",
    "Какой маленький ритуал помогает тебе работать?",
    "Какой хороший рабочий разговор был у тебя недавно?",
]

confirm_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Да ☕️", callback_data="yes"),
            InlineKeyboardButton(text="Пропустить", callback_data="no"),
        ]
    ]
)

def name(username, first_name):
    return f"@{username}" if username else first_name or "Colleague"

def is_admin(uid):
    return uid == ADMIN_ID

# -----------------------------
# START
# -----------------------------
@router.message(Command("start"))
async def start(message: Message):

    if not message.from_user.username:
        await message.answer("☕️ Нужен username в Telegram Settings")
        return

    await add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )

    await message.answer(
        "☕️ Random Ristretto\n"
        "Короткие разговоры. Сильный кофе.\n\n"
        "Раз в неделю я спрошу, хочешь ли ты участвовать."
    )

# -----------------------------
# COMMANDS
# -----------------------------
@router.message(Command("leave"))
async def leave(message: Message):
    await set_active(message.from_user.id, 0)
    await message.answer("☕️ Пауза")

@router.message(Command("resume"))
async def resume(message: Message):
    await set_active(message.from_user.id, 1)
    await message.answer("☕️ Ты снова в игре")

@router.message(Command("delete_me"))
async def delete_me(message: Message):
    await delete_user(message.from_user.id)
    await message.answer("☕️ Удалено")

# -----------------------------
# STATS
# -----------------------------
@router.message(Command("stats"))
async def stats(message: Message):

    if not is_admin(message.from_user.id):
        return

    users = await get_all_users()

    active = len([u for u in users if u["active"] == 1])
    confirmed = len([u for u in users if u["confirmed"] == 1])

    await message.answer(
        "☕️ STATS\n\n"
        f"Users: {len(users)}\n"
        f"Active: {active}\n"
        f"Confirmed: {confirmed}\n"
        f"Week meetings: {await get_this_week_meetings()}\n"
        f"Total meetings: {await get_total_meetings()}"
    )

# -----------------------------
# CHECKIN
# -----------------------------
async def checkin():

    await reset_confirmations()
    users = await get_all_users()

    for u in users:
        if u["active"] != 1:
            continue

        try:
            await bot.send_message(
                u["user_id"],
                "☕️ Готов к Random Ristretto на этой неделе?",
                reply_markup=confirm_keyboard
            )
        except:
            pass

# -----------------------------
# MATCHING
# -----------------------------
async def match():

    users = await get_confirmed_users()

    if len(users) < 2:
        return

    users = list(users)
    random.shuffle(users)

    groups = []

    while len(users) >= 2:
        groups.append([users.pop(), users.pop()])

    if users and groups:
        groups[-1].append(users.pop())

    for g in groups:

        topic = random.choice(ICEBREAKERS)

        for u in g:

            others = [
                name(x["username"], x["first_name"])
                for x in g
                if x["user_id"] != u["user_id"]
            ]

            text = "☕️ Ристретто\n\n" + "\n".join(others) + f"\n\n— {topic}"

            await bot.send_message(u["user_id"], text)

        if len(g) >= 2:
            await save_meeting(g[0]["user_id"], g[1]["user_id"])

    await reset_confirmations()

# -----------------------------
# CALLBACKS
# -----------------------------
@router.callback_query(F.data == "yes")
async def yes(c: CallbackQuery):
    await set_confirmed(c.from_user.id, 1)
    await c.message.edit_text("☕️ Отлично, ты в списке")
    await c.answer()

@router.callback_query(F.data == "no")
async def no(c: CallbackQuery):
    await set_confirmed(c.from_user.id, 0)
    await c.message.edit_text("☕️ Ок, пропускаем")
    await c.answer()

# -----------------------------
# LOOP
# -----------------------------
async def loop():

    while True:

        now = datetime.now()

        if now.weekday() == 0 and now.hour == 10:
            await checkin()

        if now.weekday() == 1 and now.hour == 11:
            await match()

        await asyncio.sleep(60)

# -----------------------------
# MAIN
# -----------------------------
async def main():

    print("☕️ Random Ristretto running")

    await init_db_pool()
    await init_db()

    dp.include_router(router)

    asyncio.create_task(loop())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())