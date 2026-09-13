"""
Диагностика Telegram Business Mode (Secretary Mode) на уровне сырого JSON.

КОНТЕКСТ / почему прежняя диагностика в router/business.py молчала:
BusinessDiagnosticMiddleware.on_pre_process_update(update, data) получает
уже готовый объект aiogram.types.Update. Но types.Update в aiogram==2.15 —
модель с жёстко описанным списком полей (update_id, message,
edited_message, channel_post, ..., poll_answer), написанная ДО того, как
Telegram добавил business_connection / business_message /
edited_business_message в Bot API 7.2 (март 2024). Разбирая JSON, aiogram
просто отбрасывает ключи, которых не знает — значит
getattr(update, "business_message", None) физически не может найти эти
данные, сколько угодно бизнес-сообщений ни присылай собеседнику. Дело не в
том, что Telegram их не шлёт, а в том, что aiogram отбрасывает их раньше,
чем мидлварь увидит апдейт.

ГДЕ РЕАЛЬНО ЕЩЁ ЖИВ СЫРОЙ JSON:
aiogram.bot.api.make_request() делает единственный HTTP-запрос и передаёт
сырой текст ответа (await response.text()) в модульную функцию
check_result(method_name, content_type, status_code, body). Именно она
через json.loads(body) получает чистый dict Telegram-ответа и возвращает
result_json.get('result') — ДО какой-либо типизации в types.Update. Для
метода getUpdates result — это список сырых dict'ов апдейтов, и если
Telegram прислал business_connection/business_message, эти ключи в этом
dict будут на месте.

Патчим именно check_result (не get_updates, не Bot.request) — это функция
уровня модуля, вызывается ровно один раз на каждый реальный HTTP-ответ,
поэтому патч не создаёт никаких дополнительных запросов к Telegram API и
не может спровоцировать TerminatedByOtherGetUpdates.

Подключение (в main.py, один раз при импорте, до executor.start_polling):

    from router.business_raw_diag import patch_check_result_for_business_diag
    patch_check_result_for_business_diag()

После того как в логах Render появится реальная структура
business_connection / business_message (ключи business_connection_id,
chat, from, message, date и т.д.) — можно писать финальную логику ответа
через сырой HTTP-запрос sendMessage с business_connection_id (aiogram==2.15
не поддерживает этот параметр в типизированных методах). После этого
данный модуль можно удалить или оставить как safety-net логирование.
"""

import json as _json

_BUSINESS_KEYS = (
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
)

_patched = False


def patch_check_result_for_business_diag() -> None:
    """
    Патчит aiogram.bot.api.check_result глобально для процесса (это
    свободная функция модуля, не метод инстанса — переопределяем один
    раз при старте, безопасно вызывать многократно благодаря _patched).
    """
    global _patched
    if _patched:
        return

    from aiogram.bot import api as aiogram_api

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
            except Exception as e:
                # Диагностика никогда не должна ронять реальную обработку
                # ответа — просто логируем и передаём управление дальше.
                print(f"[business] RAW DIAG error (non-fatal): {e}", flush=True)

        # Настоящий разбор ответа — без него сломается вообще всё, не
        # только диагностика: сюда же приходят ответы на все остальные
        # методы (sendMessage, editMessageText и т.д.)
        return original_check_result(method_name, content_type, status_code, body)

    aiogram_api.check_result = patched_check_result
    _patched = True
