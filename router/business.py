"""
Диагностика и (в перспективе) обработка Telegram Business Mode
(Secretary Mode) апдейтов.

aiogram==2.15 (на котором написан весь остальной бот) вышла раньше, чем
Telegram добавил Business-апдейты в Bot API (версия 7.2, март 2024).
Библиотека их не описывает как отдельные типизированные поля, поэтому
неизвестно заранее, сохраняет ли она их вообще при разборе апдейта от
Telegram, и в каком виде (может быть None, может быть сырой dict).

ЭТАП 1 (текущий): просто логируем всё, что видим, через
BusinessDiagnosticMiddleware в telegram/handlers.py — чтобы по логам
Render понять, долетают ли такие апдейты вообще и что именно в них
лежит. Как только это будет понятно — здесь появится реальная логика
ответа (см. TODO ниже).

TODO после диагностики: разобрать структуру business_message
(business_connection_id, chat, from, text) и отвечать через сырой HTTP
запрос sendMessage с business_connection_id (aiogram==2.15 не поддерживает
этот параметр в типизированных методах).
"""


def inspect_update_for_business_fields(update) -> bool:
    """
    Проверяет, "прилип" ли к объекту update что-то похожее на
    business_connection / business_message, и если да — логирует сырое
    содержимое для дальнейшей диагностики по логам Render.

    Возвращает True, если что-то бизнес-подобное найдено (пока только
    для логирования — обработка апдейта дальше не блокируется).
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
