"""Pure helpers for Random Ristretto iteration 2, patch 1."""
from __future__ import annotations
from datetime import datetime, timedelta

NAME_PROMPT = (
    'Напиши, пожалуйста, свои имя и фамилию. Это нужно, чтобы коллеги '
    'могли легко найти тебя в боте и отправить благодарность.'
)
NAME_ERROR = 'Пожалуйста, укажи имя и фамилию. Например: Анна Иванова. (До 80 символов, одной строкой.)'
SUGGESTION_MAX_LENGTH = 1500
SUGGESTION_HOUR = 10


def valid_full_name(value: str | None) -> bool:
    """Basic completeness only; the moderator checks the actual identity."""
    value = (value or '').strip()
    return 2 <= len(value) <= 80 and '\n' not in value and '\r' not in value and len(value.split()) >= 2


def latest_suggestion_due(now: datetime) -> datetime:
    """Latest scheduled first/15th at 10:00, in the supplied local timezone."""
    first = now.replace(day=1, hour=SUGGESTION_HOUR, minute=0, second=0, microsecond=0)
    fifteenth = first.replace(day=15)
    if now >= fifteenth:
        return fifteenth
    if now >= first:
        return first
    return (first - timedelta(days=1)).replace(day=15)


def telegram_units(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def suggestion_batches(entries: list[tuple[int, str]], limit: int = 3000) -> list[list[tuple[int, str]]]:
    """Reserve space for summary headers; never split a suggestion."""
    batches = []
    current = []
    size = 0
    for entry in entries:
        units = telegram_units(entry[1]) + 2
        if units > limit:
            # Input is capped at 1500 characters, but astral characters can take two units.
            # A single long entry still fits Telegram's 4096 limit with the header.
            if current:
                batches.append(current)
                current, size = [], 0
            if units > 3800:
                raise ValueError('Suggestion entry exceeds safe Telegram length')
            batches.append([entry])
            continue
        if current and size + units > limit:
            batches.append(current)
            current, size = [], 0
        current.append(entry)
        size += units
    if current:
        batches.append(current)
    return batches
