import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from dotenv import load_dotenv

# -----------------
# ENV
# -----------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing")

ADMIN_ID = 75734295

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

router = Router()
admin_router = Router()

# -----------------
# DB
# -----------------
DB_PATH = "ristretto.db"


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            active INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()


def add_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, active) VALUES (?, 1)",
        (user_id,)
    )
    conn.commit()
    conn.close()


def set_active(user_id: int, active: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET active=? WHERE user_id=?", (active, user_id))
    conn.commit()
    conn.close()


def delete_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, active FROM users")
    rows = cur.fetchall()
    conn.close()
    return rows


# -----------------
# UX
# -----------------
ICEBREAKERS = [
    "Какой город идеально подходит для одного ristretto?",
    "Какой рабочий ритуал ты никому не отдашь?",
    "Какой проект дал неожиданный инсайт?",
    "Что в работе ты считаешь недооценённым?",
    "Какой внутренний мем должен жить вечно?",
    "Какой хороший рабочий разговор был у тебя недавно?",
    "Что сейчас в твоей сфере переоценено?",
    "Какой маленький ритуал помогает тебе работать?"
]

keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Да ☕️", callback_data="yes")],
    [InlineKeyboardButton(text="Пауза", callback_data="no")]
])


# -----------------
# START
# -----------------
@router.message(Command("start"))
async def start(message: Message):
    add_user(message.from_user.id)

    await message.answer(
        "☕️ Random Ristretto\n"
        "Короткие разговоры. Сильный кофе.\n\n"
        "Я буду предлагать тебе случайные встречи раз в 2 недели.\n\n"
        "/match_now — запустить матч\n"
        "/leave — пауза\n"
        "/delete_me — удалить профиль"
    )


# -----------------
# USER ACTIONS
# -----------------
@router.message(Command("leave"))
async def leave(message: Message):
    set_active(message.from_user.id, 0)
    await message.answer("⏸ на паузе")


@router.message(Command("delete_me"))
async def delete_me(message: Message):
    delete_user(message.from_user.id)
    await message.answer("🗑 профиль удалён")


@router.message(Command("match_now"))
async def match_now(message: Message):
    await run_match()
    await message.answer("☕️ матч запущен")


# -----------------
# CALLBACKS
# -----------------
@router.callback_query(F.data == "yes")
async def yes(call: CallbackQuery):
    set_active(call.from_user.id, 1)
    await call.message.edit_text("☕️ активен")
    await call.answer()


@router.callback_query(F.data == "no")
async def no(call: CallbackQuery):
    set_active(call.from_user.id, 0)
    await call.message.edit_text("⏸ на паузе")
    await call.answer()


# -----------------
# FALLBACK
# -----------------
@router.message()
async def fallback(message: Message):
    await message.answer("☕️ Не понял. Попробуй /start")


# -----------------
# ADMIN
# -----------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@admin_router.message(Command("admin"))
async def admin(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "☕️ ADMIN\n\n"
        "/stats\n"
        "/users\n"
        "/pause_user <id>\n"
        "/resume_user <id>\n"
        "/delete_user <id>"
    )


@admin_router.message(Command("users"))
async def users(message: Message):
    if not is_admin(message.from_user.id):
        return

    rows = get_users()
    text = "☕️ USERS\n\n"

    for u in rows:
        text += f"{u[0]} | active={u[1]}\n"

    await message.answer(text)


@admin_router.message(Command("pause_user"))
async def pause_user(message: Message):
    if not is_admin(message.from_user.id):
        return

    try:
        user_id = int(message.text.split()[1])
        set_active(user_id, 0)
        await message.answer(f"☕️ paused {user_id}")
    except:
        await message.answer("Usage: /pause_user <id>")


@admin_router.message(Command("resume_user"))
async def resume_user(message: Message):
    if not is_admin(message.from_user.id):
        return

    try:
        user_id = int(message.text.split()[1])
        set_active(user_id, 1)
        await message.answer(f"☕️ resumed {user_id}")
    except:
        await message.answer("Usage: /resume_user <id>")


@admin_router.message(Command("delete_user"))
async def delete_user_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return

    try:
        user_id = int(message.text.split()[1])
        delete_user(user_id)
        await message.answer(f"☕️ deleted {user_id}")
    except:
        await message.answer("Usage: /delete_user <id>")


# -----------------
# MATCH ENGINE
# -----------------
async def run_match():
    users = get_users()
    active = [u for u in users if u[1] == 1]

    if len(active) < 2:
        return

    random.shuffle(active)

    pairs = []
    while len(active) > 1:
        a = active.pop()
        b = active.pop()
        pairs.append((a, b))

    for a, b in pairs:
        topic = random.choice(ICEBREAKERS)

        for u in (a, b):
            try:
                await bot.send_message(
                    u[0],
                    f"☕️ Ristretto match\n\n{topic}"
                )
            except:
                pass


# -----------------
# SCHEDULER
# -----------------
async def scheduler():
    while True:
        now = datetime.now()

        if now.weekday() == 0 and now.hour == 11 and now.minute == 0:
            await run_match()

        await asyncio.sleep(30)


# -----------------
# STARTUP
# -----------------
async def main():
    init_db()

    dp.include_router(router)
    dp.include_router(admin_router)

    asyncio.create_task(scheduler())

    print("☕️ Random Ristretto running")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())