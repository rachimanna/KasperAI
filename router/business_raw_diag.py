"""
Перехват сырого JSON апдейтов Telegram Business Mode + диспетчеризация
в реальную обработку (router.business.handle_business_message).

КОНТЕКСТ / почему диагностика на уровне types.Update молчала:
aiogram.types.Update в aiogram==2.15 — модель с жёстко описанным списком
полей (update_id, message, edited_message, channel_post, ...,
poll_answer), написанная ДО того, как Telegram добавил business_connection
/ business_message / edited_business_message в Bot API 7.2 (март 2024).
Разбирая JSON, aiogram просто отбрасывает ключи, которых не знает —
поэтому смотреть на уже готовый types.Update бесполезно, эти данные там
физически отсутствуют.

ГДЕ РЕАЛЬНО ЕЩЁ ЖИВ СЫРОЙ JSON:
aiogram.bot.api.make_request() делает единственный HTTP-запрос и передаёт
сырой текст ответа в модульную функцию
check_result(method_name, content_type, status_code, body). Она через
json.loads(body) получает чистый dict Telegram-ответа и возвращает
result_json.get('result') — ДО какой-либо типизации в types.Update. Для
getUpdates result — список сырых dict'ов апдейтов, где business_connection
/ business_message ещё на месте, если Telegram их прислал.

Патчим именно check_result (не get_updates, не Bot.request) — функция
уровня модуля, вызывается ровно один раз на каждый реальный HTTP-ответ,
поэтому патч не создаёт никаких дополнительных запросов к Telegram API и
не может спровоцировать TerminatedByOtherGetUpdates.

ПОЧЕМУ ЗДЕСЬ ИСПОЛЬЗУЕТСЯ asyncio.create_task, А НЕ await:
check_result — синхронная функция (нет async/await в её сигнатуре в самом
aiogram). Настоящая обработка business-сообщения асинхронна (спрашивает
AI, шлёт ответ через bot.request) — значит её нельзя просто await'нуть
внутри синхронного патча. Вместо этого планируем её как отдельную задачу
в уже существующем event loop через asyncio.create_task: это не блокирует
check_result и не задерживает обработку остальных апдейтов, а вопрос к AI
и ответ уходят своим чередом в фоне.

Подключение (в main.py, один раз при старте, после создания bot, до
executor.start_polling):

    from router.business_raw_diag import patch_check_result_for_business_diag
    patch_check_result_for_business_diag(bot)
"""

import asyncio
import json as _json

_BUSINESS_KEYS = (
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
)

_patched = False


def patch_check_result_for_business_diag(bot) -> None:
    """
    Патчит aiogram.bot.api.check_result глобально для процесса.
    Принимает bot, чтобы держать его в замыкании и передавать в
    handle_business_message для отправки ответов через
    business_connection_id (сырой check_result этого объекта не имеет).
    """
    global _patched
    if _patched:
        return

    from aiogram.bot import api as aiogram_api
    from router.business import handle_business_message

    original_check_result = aiogram_api.check_result

    def patched_check_result(method_name, content_type, status_code, body):
        if method_name == "getUpdates":
            try:
                raw = _json.loads(body)
                for raw_update in raw.get("result", []) or []:
                    if not isinstance(raw_update, dict):
                        continue

                    found_keys = [k for k in _BUSINESS_KEYS if k in raw_update]
                    if found_keys:
                        print(
                            f"[business] RAW DIAG update_id={raw_update.get('update_id')} "
                            f"keys={found_keys} raw={_json.dumps(raw_update, ensure_ascii=False)}",
                            flush=True,
                        )

                    # business_message — новое сообщение в подключённом
                    # business-чате (от владельца аккаунта или его
                    # собеседника, оба приходят через один и тот же
                    # business_connection владельца). Реальную обработку
                    # (проверка триггера "Каспер", вызов AI, ответ) не
                    # делаем здесь синхронно — планируем как задачу.
                    business_message = raw_update.get("business_message")
                    if isinstance(business_message, dict):
                        asyncio.create_task(
                            handle_business_message(bot, business_message)
                        )
            except Exception as e:
                # Диагностика/диспетчеризация не должна ронять реальную
                # обработку ответа — логируем и передаём управление дальше.
                print(f"[business] RAW DIAG error (non-fatal): {e}", flush=True)

        # Настоящий разбор ответа — без него сломается вообще всё, не
        # только диагностика: сюда же приходят ответы на все остальные
        # методы (sendMessage, editMessageText и т.д.)
        return original_check_result(method_name, content_type, status_code, body)

    aiogram_api.check_result = patched_check_result
    _patched = True
