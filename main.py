print("🔥 RUNNING FILE: MAIN.PY NEW VERSION")
import os
import logging
import asyncio
import random
import sqlite3

from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command

# -----------------------------
# ENV
# -----------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")

# ВСТАВЬ СВОЙ TELEGRAM ID
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
# DATABASE
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
        username TEXT,
        first_name TEXT,
        active INTEGER DEFAULT 1,
        confirmed INTEGER DEFAULT 0,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meetings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user1 INTEGER,
        user2 INTEGER,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


# -----------------------------
# USERS
# -----------------------------
def add_user(user_id, username, first_name):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO users (
        user_id,
        username,
        first_name,
        active,
        confirmed,
        created_at
    )
    VALUES (?, ?, ?, 1, 0, ?)

    ON CONFLICT(user_id)
    DO UPDATE SET
        username=excluded.username,
        first_name=excluded.first_name
    """, (
        user_id,
        username,
        first_name,
        datetime.now().isoformat()
    ))

    conn.commit()
    conn.close()


def set_active(user_id, active):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    UPDATE users
    SET active=?
    WHERE user_id=?
    """, (
        active,
        user_id
    ))

    conn.commit()
    conn.close()


def set_confirmed(user_id, confirmed):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    UPDATE users
    SET confirmed=?
    WHERE user_id=?
    """, (
        confirmed,
        user_id
    ))

    conn.commit()
    conn.close()


def reset_confirmations():

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    UPDATE users
    SET confirmed=0
    """)

    conn.commit()
    conn.close()


def delete_user(user_id):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    DELETE FROM users
    WHERE user_id=?
    """, (user_id,))

    conn.commit()
    conn.close()


def get_confirmed_users():

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT user_id, username, first_name
    FROM users
    WHERE active=1
    AND confirmed=1
    """)

    users = cur.fetchall()

    conn.close()

    return users


def get_all_users():

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT user_id, username, active, confirmed
    FROM users
    """)

    users = cur.fetchall()

    conn.close()

    return users


# -----------------------------
# MEETINGS
# -----------------------------
def save_meeting(user1, user2):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO meetings (
        user1,
        user2,
        created_at
    )
    VALUES (?, ?, ?)
    """, (
        user1,
        user2,
        datetime.now().isoformat()
    ))

    conn.commit()
    conn.close()


def get_total_meetings():

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT COUNT(*)
    FROM meetings
    """)

    total = cur.fetchone()[0]

    conn.close()

    return total


def get_this_week_meetings():

    conn = db()
    cur = conn.cursor()

    cur.execute("""
    SELECT COUNT(*)
    FROM meetings
    WHERE datetime(created_at) >= datetime('now', '-7 days')
    """)

    total = cur.fetchone()[0]

    conn.close()

    return total


# -----------------------------
# HELPERS
# -----------------------------
def is_admin(user_id):
    return user_id == ADMIN_ID


def display_name(username, first_name):

    if username:
        return f"@{username}"

    return first_name or "Colleague"


# -----------------------------
# ICEBREAKERS
# -----------------------------
ICEBREAKERS = [
    "Какой рабочий ритуал ты никому не отдашь?",
    "Какой проект дал неожиданный инсайт?",
    "Что сейчас в твоей сфере переоценено?",
    "Какой маленький ритуал помогает тебе работать?",
    "Какой хороший рабочий разговор был у тебя недавно?",
]

# -----------------------------
# KEYBOARD
# -----------------------------
confirm_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Да ☕️",
                callback_data="confirm_yes"
            ),
            InlineKeyboardButton(
                text="Пропустить неделю",
                callback_data="confirm_no"
            )
        ]
    ]
)

# -----------------------------
# START
# -----------------------------
@router.message(Command("start"))
async def start(message: Message):

    if not message.from_user.username:

        await message.answer(
            "☕️ Для участия нужен Telegram username.\n\n"
            "Его можно добавить в Telegram Settings → Username."
        )

        return

    add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )

    await message.answer(
        "☕️ Random Ristretto\n"
        "Короткие разговоры. Сильный кофе.\n\n"
        "Раз в неделю я буду спрашивать, "
        "хочешь ли ты участвовать "
        "в Random Ristretto на этой неделе."
    )


# -----------------------------
# USER COMMANDS
# -----------------------------
@router.message(Command("leave"))
async def leave(message: Message):

    set_active(message.from_user.id, 0)

    await message.answer(
        "☕️ Участие поставлено на паузу"
    )


@router.message(Command("resume"))
async def resume(message: Message):

    set_active(message.from_user.id, 1)

    await message.answer(
        "☕️ Ты снова в Random Ristretto"
    )


@router.message(Command("delete_me"))
async def delete_me(message: Message):

    delete_user(message.from_user.id)

    await message.answer(
        "☕️ Профиль удалён"
    )


# -----------------------------
# ADMIN
# -----------------------------
@router.message(Command("stats"))
async def stats(message: Message):

    if not is_admin(message.from_user.id):
        return

    users = get_all_users()

    active = len([u for u in users if u[2] == 1])
    confirmed = len([u for u in users if u[3] == 1])

    total_meetings = get_total_meetings()
    week_meetings = get_this_week_meetings()

    await message.answer(
        "☕️ Random Ristretto Stats\n\n"
        f"👥 Users: {len(users)}\n"
        f"🟢 Active: {active}\n"
        f"☕️ Confirmed this week: {confirmed}\n\n"
        f"🤝 Meetings this week: {week_meetings}\n"
        f"📈 Total meetings: {total_meetings}"
    )


@router.message(Command("checkin_now"))
async def checkin_now(message: Message):

    if not is_admin(message.from_user.id):
        return

    await send_weekly_checkin()

    await message.answer(
        "☕️ Weekly check-in sent"
    )


@router.message(Command("match_now"))
async def match_now(message: Message):

    if not is_admin(message.from_user.id):
        return

    await run_matching()

    await message.answer(
        "☕️ Matching completed"
    )


# -----------------------------
# CONFIRMATION CALLBACKS
# -----------------------------
@router.callback_query(F.data == "confirm_yes")
async def confirm_yes(callback: CallbackQuery):

    set_confirmed(callback.from_user.id, 1)

    await callback.message.edit_text(
        "☕️ Отлично. Учту тебя в Random Ristretto на этой неделе."
    )

    await callback.answer()


@router.callback_query(F.data == "confirm_no")
async def confirm_no(callback: CallbackQuery):

    set_confirmed(callback.from_user.id, 0)

    await callback.message.edit_text(
        "☕️ Хорошо, пропустим эту неделю."
    )

    await callback.answer()


# -----------------------------
# WEEKLY CHECK-IN
# -----------------------------
async def send_weekly_checkin():

    reset_confirmations()

    users = get_all_users()

    for user in users:

        user_id = user[0]
        active = user[2]

        if active != 1:
            continue

        try:
            await bot.send_message(
                user_id,
                "☕️ Готов(а) к Random Ristretto на этой неделе?",
                reply_markup=confirm_keyboard
            )

        except Exception as e:
            print("checkin error:", e)


# -----------------------------
# MATCHING
# -----------------------------
async def run_matching():

    users = get_confirmed_users()

    if len(users) < 2:
        return

    random.shuffle(users)

    groups = []

    # Формируем пары
    while len(users) >= 2:

        user1 = users.pop()
        user2 = users.pop()

        groups.append([user1, user2])

    # Если остался один —
    # добавляем в последнюю группу
    if users:

        leftover = users.pop()

        if groups:
            groups[-1].append(leftover)

    # Рассылка
    for group in groups:

        topic = random.choice(ICEBREAKERS)

        names = [
            display_name(u[1], u[2])
            for u in group
        ]

        for current_user in group:

            current_name = display_name(
                current_user[1],
                current_user[2]
            )

            others = [
                n for n in names
                if n != current_name
            ]

            people_text = "\n".join(others)

            text = (
                "☕️ Твой Random Ristretto на этой неделе\n\n"
                f"{people_text}\n\n"
                "Напишите друг другу напрямую в Telegram ☕️\n\n"
                "Тема для старта:\n"
                f"— {topic}"
            )

            try:
                await bot.send_message(
                    current_user[0],
                    text
                )

            except Exception as e:
                print("matching error:", e)

        # Сохраняем встречи
        if len(group) == 2:

            save_meeting(
                group[0][0],
                group[1][0]
            )

        elif len(group) == 3:

            save_meeting(group[0][0], group[1][0])
            save_meeting(group[0][0], group[2][0])
            save_meeting(group[1][0], group[2][0])

    # Сбрасываем confirmations
    reset_confirmations()


# -----------------------------
# SCHEDULER
# -----------------------------
async def scheduler():

    last_checkin_day = None
    last_match_day = None

    while True:

        now = datetime.now()

        # Monday 10:00 → check-in
        if (
            now.weekday() == 0
            and now.hour == 10
            and last_checkin_day != now.date()
        ):

            last_checkin_day = now.date()

            await send_weekly_checkin()

        # Tuesday 11:00 → matching
        if (
            now.weekday() == 1
            and now.hour == 11
            and last_match_day != now.date()
        ):

            last_match_day = now.date()

            await run_matching()

        await asyncio.sleep(60)


# -----------------------------
# FALLBACK
# -----------------------------
@router.message()
async def fallback(message: Message):

    await message.answer(
        "☕️ Используй /start чтобы подключиться к Random Ristretto"
    )


# -----------------------------
# MAIN
# -----------------------------
async def main():

    print("☕️ Random Ristretto running")

    init_db()

    dp.include_router(router)

    asyncio.create_task(scheduler())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())