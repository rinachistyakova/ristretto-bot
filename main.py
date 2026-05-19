import os
import logging
import sqlite3

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from aiogram.filters import Command
from dotenv import load_dotenv

# -----------------------------
# INIT
# -----------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing in environment variables")

ADMIN_ID = 123456789  # <-- вставь свой Telegram ID

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

router = Router()
admin_router = Router()

# -----------------------------
# DB
# -----------------------------
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


def get_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, active FROM users")
    rows = cur.fetchall()
    conn.close()
    return rows


def set_active(user_id: int, active: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET active=? WHERE user_id=?",
        (active, user_id)
    )
    conn.commit()
    conn.close()


def delete_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# -----------------------------
# HELPERS
# -----------------------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


# -----------------------------
# USER HANDLERS
# -----------------------------
@router.message(Command("start"))
async def start(message: Message):
    add_user(message.from_user.id)

    await message.answer(
        "☕️ Random Ristretto\n"
        "Короткие разговоры. Сильный кофе.\n\n"
        "Я буду предлагать тебе случайные встречи раз в 2 недели."
    )


@router.message(Command("pause"))
async def pause(message: Message):
    set_active(message.from_user.id, 0)
    await message.answer("☕️ You are paused")


@router.message(Command("resume"))
async def resume(message: Message):
    set_active(message.from_user.id, 1)
    await message.answer("☕️ You are back in the flow")


@router.message(Command("delete_me"))
async def delete_me(message: Message):
    delete_user(message.from_user.id)
    await message.answer("☕️ Your profile was deleted")


# -----------------------------
# FALLBACK (catch-all)
# -----------------------------
@router.message()
async def fallback(message: Message):
    await message.answer("☕️ I didn't understand that. Try /start or /admin")


# -----------------------------
# ADMIN
# -----------------------------
@admin_router.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "☕️ ADMIN PANEL\n\n"
        "/stats\n"
        "/users\n"
        "/pause_user <id>\n"
        "/resume_user <id>\n"
        "/delete_user <id>"
    )


@admin_router.message(Command("stats"))
async def stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    users = get_users()
    active = sum(1 for u in users if u[1] == 1)

    await message.answer(
        f"☕️ STATS\n\n"
        f"Total users: {len(users)}\n"
        f"Active users: {active}"
    )


@admin_router.message(Command("users"))
async def users_list(message: Message):
    if not is_admin(message.from_user.id):
        return

    users = get_users()[:30]

    text = "☕️ USERS\n\n"
    for u in users:
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


# -----------------------------
# STARTUP
# -----------------------------
async def on_startup():
    init_db()
    print("☕️ Random Ristretto started")


# -----------------------------
# MAIN
# -----------------------------
async def main():
    dp.include_router(router)
    dp.include_router(admin_router)

    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())