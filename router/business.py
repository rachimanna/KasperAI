"""
Обработка Telegram Business Mode (Secretary Mode): бот следит за личным
чатом, подключённым через business_connection, и когда кто-либо из двух
собеседников пишет слово-триггер "Каспер" — отвечает в этот же чат
обычным сообщением, видимым обоим (владельцу аккаунта и его собеседнику).

КОНТЕКСТ:
aiogram==2.15 вышла до Bot API 7.2 (март 2024, где Telegram добавил
Business Mode) и не имеет типизированной поддержки апдейтов
business_connection / business_message — types.Update просто отбрасывает
эти поля при разборе JSON. Реальный сырой JSON перехватывается на уровне
ниже, в router/business_raw_diag.py (патч aiogram.bot.api.check_result).
Этот модуль переиспользует тот же принцип: работает с обычными dict,
а не с типизированными объектами aiogram.

ПОЧЕМУ ТРИГГЕР РАБОТАЕТ ДЛЯ ОБОИХ УЧАСТНИКОВ ЧАТА:
Per документации Telegram Business (и подтверждено в grammY docs),
business-подключение стоит один раз на аккаунте владельца и покрывает
ВЕСЬ его личный чат целиком — бот получает business_message на КАЖДОЕ
сообщение в этом чате, независимо от того, кто из двух собеседников его
написал (сверяется через message.from.id). Отдельное подключение
собеседнику НЕ требуется. Поэтому проверка триггера тут не завязана на
конкретного отправителя — реагируем на слово "Каспер" от любого автора
внутри business-чата.

ОТПРАВКА ОТВЕТА:
Bot API 7.2 добавил параметр business_connection_id в обычный метод
sendMessage (это не отдельный метод!) — значит достаточно вызвать
низкоуровневый bot.request(Methods.SEND_MESSAGE, data) с этим полем,
в обход типизированного bot.send_message(), которого в aiogram==2.15
не хватает для этого параметра.
"""

import re

from router.ai_router import ask

TRIGGER_WORD = "каспер"

# Разделяет "Каспер <вопрос>" на само слово-триггер и текст после него —
# регистронезависимо, с необязательными знаками препинания/пробелами
# сразу после триггера (","/":"/" "/"!" и т.п.), напр. "Каспер, сколько 8+8".
_TRIGGER_PATTERN = re.compile(
    r"^\s*каспер[\s,:!\-]*", re.IGNORECASE,
)


def extract_trigger_question(text: str):
    """
    Если text начинается со слова-триггера "Каспер" — возвращает часть
    после него (сам вопрос к AI). Если триггера нет — возвращает None.

    Специально ищет триггер только в начале сообщения (не в середине),
    чтобы случайное упоминание слова "каспер" в обычной фразе разговора
    не срабатывало как команда к AI.
    """
    if not text:
        return None

    match = _TRIGGER_PATTERN.match(text)
    if not match:
        return None

    question = text[match.end():].strip()
    return question


async def handle_business_message(bot, raw_message: dict) -> None:
    """
    Обрабатывает один сырой business_message dict (как он приходит от
    Telegram, до всякой типизации aiogram). Если в тексте есть триггер
    "Каспер" — спрашивает AI и отвечает в тот же чат через
    business_connection_id.

    Ничего не делает (тихо возвращается), если:
    - в апдейте нет business_connection_id (защита от вызова не для того
      сообщения);
    - в сообщении нет текста (фото/стикер/голосовое без подписи);
    - текст не начинается с триггера.
    """
    business_connection_id = raw_message.get("business_connection_id")
    if not business_connection_id:
        return

    text = raw_message.get("text") or raw_message.get("caption")
    question = extract_trigger_question(text) if text else None
    if question is None:
        return

    chat = raw_message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        print("[business] handle_business_message: no chat.id in raw_message, skip", flush=True)
        return

    if not question:
        # Написали просто "Каспер" без вопроса — просим уточнить,
        # но не дёргаем AI впустую.
        await _send_business_message(
            bot,
            business_connection_id,
            chat_id,
            "Слушаю! Задай вопрос после слова «Каспер».",
        )
        return

    try:
        result = await ask(question)
        answer = result.get("answer", "") if isinstance(result, dict) else str(result)
        answer = str(answer).strip() or "⚠️ AI вернул пустой ответ."
    except Exception as e:
        print(f"[business] ask() error: {e}", flush=True)
        answer = "⚠️ Не получилось получить ответ от AI, попробуй ещё раз."

    await _send_business_message(bot, business_connection_id, chat_id, answer)


async def _send_business_message(bot, business_connection_id: str, chat_id, text: str) -> None:
    """
    Отправляет сообщение от имени business-аккаунта через сырой
    bot.request — типизированный bot.send_message() в aiogram==2.15 не
    принимает business_connection_id, этого параметра не существовало на
    момент выхода библиотеки (появился в Bot API 7.2).
    """
    from aiogram.bot import api as aiogram_api

    try:
        await bot.request(
            aiogram_api.Methods.SEND_MESSAGE,
            {
                "chat_id": chat_id,
                "text": text,
                "business_connection_id": business_connection_id,
            },
        )
    except Exception as e:
        print(f"[business] send_message error: {e}", flush=True)


def inspect_update_for_business_fields(update) -> bool:
    """
    ЭТАП 1 диагностики (оставлено для обратной совместимости с
    telegram/handlers.py, где эта функция всё ещё вызывается из
    BusinessDiagnosticMiddleware).

    ВАЖНО: этот путь технически не может увидеть business-поля — aiogram
    types.Update их не типизирует и отбрасывает при разборе JSON задолго
    до того, как update дойдёт до мидлвари. Реальная диагностика и вся
    рабочая логика ответа теперь идут через router/business_raw_diag.py
    (патч на уровне сырого JSON) и handle_business_message() выше.
    Функция ниже оставлена только чтобы не ломать существующий импорт.
    """
    found = False

    business_connection = getattr(update, "business_connection", None)
    if business_connection is not None:
        found = True
        print(f"[business] DIAG business_connection = {business_connection!r}", flush=True)

    business_message = getattr(update, "business_message", None)
    if business_message is not None:
        found = True
        print(f"[business] DIAG business_message = {business_message!r}", flush=True)

    edited_business_message = getattr(update, "edited_business_message", None)
    if edited_business_message is not None:
        found = True
        print(f"[business] DIAG edited_business_message = {edited_business_message!r}", flush=True)

    return found
