import logging
import asyncio
import random
import os

from datetime import datetime
from dotenv import load_dotenv

from database import (
    init_db,
    add_user,
    set_active,
    get_users,
    delete_user,
    save_meeting,
    last_meeting_days_ago,
    get_stats
)

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton


# ---------- ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO)
print("🔥 LEVEL 5 SOCIAL ENGINE STARTED")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


ICEBREAKERS = [
    "Какой город идеально подходит для одного ristretto?",
    "Какой рабочий ритуал ты никому не отдашь?",
    "Какой проект дал неожиданный инсайт?",
    "Что в работе ты считаешь недооценённым?",
    "Какой внутренний мем должен жить вечно?"
]


keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Да ☕️", callback_data="yes")],
    [InlineKeyboardButton(text="Пауза", callback_data="no")]
])


# ---------- USER ----------
@dp.message(Command("start"))
async def start(message: Message):
    add_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name
    )

    set_active(message.from_user.id, True)

    await message.answer(
        "☕️ Ristretto Level 5\n"
        "Ты в системе.\n"
        "/stats — аналитика\n"
        "/match_now — запуск матча\n"
        "/leave — пауза\n"
        "/delete_me — удаление"
    )


@dp.message(Command("leave"))
async def leave(message: Message):
    set_active(message.from_user.id, False)
    await message.answer("⏸ пауза")


@dp.message(Command("delete_me"))
async def delete_me(message: Message):
    delete_user(message.from_user.id)
    await message.answer("🗑 удалено")


# ---------- ADMIN ANALYTICS ----------
@dp.message(Command("stats"))
async def stats(message: Message):
    data = get_stats()

    await message.answer(
        "📊 SOCIAL ENGINE STATS\n\n"
        f"👥 users: {data['users']}\n"
        f"⚡ active: {data['active']}\n"
        f"☕ meetings: {data['meetings']}\n"
        f"🔥 avg inactivity: {data['avg_inactivity']}"
    )


# ---------- MANUAL TRIGGER ----------
@dp.message(Command("match_now"))
async def match_now(message: Message):
    await run_match()
    await message.answer("☕️ матч выполнен")


# ---------- CALLBACKS ----------
@dp.callback_query(F.data == "yes")
async def yes(call: CallbackQuery):
    set_active(call.from_user.id, True)
    await call.message.edit_text("☕️ активен")
    await call.answer()


@dp.callback_query(F.data == "no")
async def no(call: CallbackQuery):
    set_active(call.from_user.id, False)
    await call.message.edit_text("⏸ пауза")
    await call.answer()


# ---------- MATCH ENGINE ----------
async def run_match():
    users = get_users(active_only=True)
    random.shuffle(users)

    pairs = []
    used = set()

    users.sort(key=lambda u: last_meeting_days_ago(u[0], u[0]), reverse=True)

    while len(users) > 1:
        a = users.pop(0)

        best = None
        best_score = -1

        for i, b in enumerate(users):
            score = last_meeting_days_ago(a[0], b[0])

            if score > best_score:
                best_score = score
                best = i

        if best is None:
            b = users.pop(0)
        else:
            b = users.pop(best)

        pairs.append((a, b))

    if users:
        pairs.append((users.pop(),))

    for pair in pairs:
        topic = random.choice(ICEBREAKERS)

        if len(pair) == 2:
            save_meeting(pair[0][0], pair[1][0])

        for u in pair:
            try:
                await bot.send_message(
                    u[0],
                    f"☕️ Ristretto match\n\n{topic}"
                )
            except:
                pass


# ---------- SCHEDULER ----------
async def scheduler():
    while True:
        now = datetime.now()

        if now.weekday() == 0 and now.hour == 11 and now.minute == 0:
            await run_match()

        await asyncio.sleep(30)


# ---------- MAIN ----------
async def main():
    print("Bot running ☕️")
    init_db()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())