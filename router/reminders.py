"""
Напоминания Kasper AI: «напомни через 20 минут выключить духовку»,
«напомни завтра в 9 позвонить маме», «разбуди в 6:30».

Хранятся в SQLite (таблица reminders), поэтому переживают рестарт и
передеплой на Render. Фоновая задача reminder_loop раз в 15 секунд
отправляет наступившие напоминания. Если бот «спал» (free-тариф Render
засыпает без трафика), просроченные напоминания уйдут сразу после
пробуждения — с пометкой, на сколько опоздали.
"""

import asyncio
import html
import random
import re
from datetime import datetime, timezone

from database.db import (
    add_reminder,
    get_due_reminders,
    mark_reminder_sent,
    get_user_reminders,
    count_user_reminders,
    cancel_reminder,
    get_user_timezone,
)
from router.time_awareness import (
    parse_reminder,
    parse_db_ts,
    get_zone,
    format_dt_ru,
    humanize_delta,
    describe_zone,
    WEEKDAYS_SHORT,
)

MAX_ACTIVE_REMINDERS = 30
CHECK_INTERVAL_SECONDS = 15

# «напомни, как называется тот фильм?» — это вопрос, а не напоминание.
_QUESTION_AFTER_REMIND = re.compile(
    r"напомни\w*[\s,]+(?:мне[\s,]+|пожалуйста[\s,]+)?"
    r"(как|что\s+за|какой|какая|какое|какие|кто|где|сколько|почему|зачем|"
    r"о\s+ч[её]м|про\s+что|что\s+я|что\s+мы|что\s+ты|что\s+было|что\s+такое)\b",
    re.IGNORECASE,
)

_CONFIRM_TEMPLATES = [
    "⏰ Ладно, запомнил. Напомню {when}: «{text}».",
    "⏰ Записал. {when_cap} пну тебя насчёт «{text}».",
    "⏰ Окей, {when} напомню: «{text}». Не благодари.",
    "⏰ Принято. {when_cap} — «{text}». Я не забуду, в отличие от некоторых.",
]
_FIRE_TEMPLATES = [
    "⏰ Эй, напоминаю: {text}",
    "⏰ Ты просил напомнить: {text}",
    "⏰ Время пришло: {text}",
    "⏰ Напоминалка: {text}",
]


def detect_reminder(text, tz_name, now=None):
    """
    Возвращает dict из parse_reminder, если это реальная просьба напомнить
    с понятным временем в будущем. Иначе None — сообщение уйдёт в обычный
    чат с ИИ (например «напомни, как решать квадратные уравнения»).
    """
    parsed = parse_reminder(text, tz_name, now=now)
    if not parsed or parsed["due_utc"] is None:
        return None
    has_through = re.search(r"\b(через|спустя)\b", text, re.IGNORECASE)
    if not has_through and (_QUESTION_AFTER_REMIND.search(text) or text.strip().endswith("?")):
        return None
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if parsed["due_utc"] <= now_utc:
        return None
    return parsed


def _when_phrase(local_dt, now_local):
    days = (local_dt.date() - now_local.date()).days
    delta = (local_dt - now_local).total_seconds()
    if delta < 3 * 3600 and days == 0:
        return f"через {humanize_delta(delta)} (в {local_dt:%H:%M})"
    if days == 0:
        return f"сегодня в {local_dt:%H:%M}"
    if days == 1:
        return f"завтра в {local_dt:%H:%M}"
    if days == 2:
        return f"послезавтра в {local_dt:%H:%M}"
    return f"{format_dt_ru(local_dt)}"


async def create_reminder_from_text(message, user_id, parsed, tz_name):
    """Сохраняет напоминание и отвечает пользователю подтверждением."""
    if await count_user_reminders(user_id) >= MAX_ACTIVE_REMINDERS:
        await message.reply(
            f"⛔ У тебя уже {MAX_ACTIVE_REMINDERS} активных напоминаний. "
            "Удали лишние: /reminders"
        )
        return
    reminder_id = await add_reminder(
        user_id, message.chat.id, parsed["text"], parsed["due_utc"]
    )
    tz = get_zone(tz_name)
    local = parsed["due_utc"].astimezone(tz)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    when = _when_phrase(local, now_local)
    text = random.choice(_CONFIRM_TEMPLATES).format(
        when=when, when_cap=when[:1].upper() + when[1:], text=parsed["text"]
    )
    text += f"\n\nЧасовой пояс: {describe_zone(tz_name)} · отменить: /delreminder {reminder_id}"
    await message.reply(text)


async def reminders_list_text(user_id, tz_name):
    rows = await get_user_reminders(user_id)
    if not rows:
        return (
            "📭 Активных напоминаний нет.\n\n"
            "Просто напиши, например: «напомни через 20 минут выключить духовку» "
            "или «напомни завтра в 9:00 позвонить маме»."
        )
    tz = get_zone(tz_name)
    now_local = datetime.now(timezone.utc).astimezone(tz)
    lines = ["⏰ <b>Твои напоминания</b>", ""]
    for row in rows:
        rid, text, due_at = row[0], row[1], row[2]
        local = parse_db_ts(due_at).astimezone(tz)
        left = humanize_delta((local - now_local).total_seconds())
        lines.append(
            f"#{rid} · {WEEKDAYS_SHORT[local.weekday()]} {local:%d.%m %H:%M} "
            f"(через {left}) — {html.escape(text)}"
        )
    lines.append("")
    lines.append("Удалить: /delreminder номер")
    return "\n".join(lines)


async def delete_reminder_cmd(user_id, arg):
    arg = (arg or "").strip().lstrip("#")
    if not arg.isdigit():
        return "Использование: /delreminder номер (номера смотри в /reminders)"
    ok = await cancel_reminder(user_id, int(arg))
    return "🗑 Напоминание удалено." if ok else "Не нашёл такое активное напоминание."


async def reminder_loop(bot):
    """Фоновая задача: раз в CHECK_INTERVAL_SECONDS шлёт наступившие напоминания."""
    while True:
        try:
            for row in await get_due_reminders():
                rid, _user_id, chat_id, text, due_at = row[0], row[1], row[2], row[3], row[4]
                due = parse_db_ts(due_at)
                late = (datetime.now(timezone.utc) - due).total_seconds() if due else 0
                body = random.choice(_FIRE_TEMPLATES).format(text=text)
                if late > 120:
                    body += f"\n(с опозданием на {humanize_delta(late)} — я отключался, извиняй)"
                try:
                    await bot.send_message(chat_id, body)
                except Exception as e:
                    print(f"[reminders] send #{rid} error: {e}", flush=True)
                # помечаем в любом случае, чтобы не спамить при заблокированном боте
                await mark_reminder_sent(rid)
        except Exception as e:
            print(f"[reminders] loop error: {e}", flush=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


__all__ = [
    "detect_reminder",
    "create_reminder_from_text",
    "reminders_list_text",
    "delete_reminder_cmd",
    "reminder_loop",
    "get_user_timezone",
]
