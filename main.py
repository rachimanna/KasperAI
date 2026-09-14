import asyncio
import logging
import os
import resource
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram import executor

from database.db import init_db
from router.ai_router import init_http_session, close_http_session
from router.game_logic import phase_checker_loop
from router.business_raw_diag import patch_check_result_for_business_diag
from telegram.handlers import register_handlers
from config.settings import ADMIN_IDS


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def _current_memory_mb() -> float:
    """
    Пиковое потребление памяти процесса в МБ через resource.getrusage —
    без сторонних зависимостей (psutil не установлена, добавлять её ради
    одной метрики незачем). На Linux ru_maxrss в килобайтах.

    Нужно, потому что Render Metrics -> Memory на free-тарифе недоступен
    (это платная фича, "Application Metrics" под платным планом) — так
    единственный способ понять, упирается ли процесс в лимит 512MB,
    это логировать это самостоятельно и смотреть в обычных логах Render.
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


async def memory_logger_loop(interval_seconds: int = 60):
    """
    Раз в interval_seconds логирует текущее (пиковое) потребление памяти
    процесса. Позволяет по логам Render сопоставить моменты рестартов
    (TerminatedByOtherGetUpdates и т.п.) с уровнем памяти прямо перед этим —
    без доступа к платным Application Metrics.
    """
    while True:
        try:
            mb = _current_memory_mb()
            print(f"[memory] RSS peak = {mb:.1f} MB (limit 512 MB on free tier)", flush=True)
        except Exception as e:
            print(f"[memory] logger error: {e}", flush=True)
        await asyncio.sleep(interval_seconds)


async def on_startup(dp):
    await init_http_session()
    await init_db()

    from aiogram.types import BotCommand, BotCommandScopeChat

    # Полностью очищаем старые команды Telegram.
    try:
        await dp.bot.delete_my_commands()
    except Exception as e:
        print(f"[Commands] default scope cleanup error: {e}", flush=True)

    # Устанавливаем актуальные команды для всех пользователей.
    await dp.bot.set_my_commands([
        BotCommand("start", "Запустить бота"),
        BotCommand("limit", "Мой лимит запросов"),
        BotCommand("status", "Состояние бота"),
        BotCommand("agent", "AI-агент: найти/сравнить/сделать сайт"),
        BotCommand("shadowcity", "Начать игру «Теневой город»"),
        BotCommand("stopshadowcity", "Остановить текущую игру"),
        BotCommand("gamestats", "Моя статистика «Теневого города»"),
    ])

    # Админские команды показываем в подсказках только самим админам —
    # через scope=BotCommandScopeChat(chat_id=admin_id), а не в общем
    # меню, иначе их увидят и смогут попытаться вызвать все пользователи.
    admin_commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("limit", "Мой лимит запросов"),
        BotCommand("status", "Состояние бота"),
        BotCommand("agent", "AI-агент: найти/сравнить/сделать сайт"),
        BotCommand("shadowcity", "Начать игру «Теневой город»"),
        BotCommand("stopshadowcity", "Остановить текущую игру"),
        BotCommand("gamestats", "Моя статистика «Теневого города»"),
        BotCommand("stats", "Статистика бота (админ)"),
        BotCommand("ban", "Забанить пользователя (ответом/@username/id)"),
        BotCommand("unban", "Разбанить пользователя (ответом/@username/id)"),
        BotCommand("broadcast", "Рассылка всем пользователям"),
    ]
    for admin_id in ADMIN_IDS:
        try:
            await dp.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            print(f"[Commands] admin scope error for {admin_id}: {e}", flush=True)

    # Фоновая задача мини-игры "Теневой город": раз в несколько секунд
    # проверяет БД на игры с истёкшей фазой (ночь/голосование) и
    # продвигает их. Живёт в games.phase_ends_at, поэтому переживает
    # пересыпание/передеплой Render.
    asyncio.create_task(phase_checker_loop(dp.bot))

    # Логирование памяти каждую минуту — см. _current_memory_mb выше:
    # единственный способ следить за потреблением на free-тарифе Render,
    # где графики памяти скрыты за платным планом.
    asyncio.create_task(memory_logger_loop())

    print("Database: OK")
    print("Kasper AI is running.")


async def on_shutdown(dp):
    try:
        print(f"[memory] RSS at shutdown = {_current_memory_mb():.1f} MB", flush=True)
    except Exception as e:
        print(f"[memory] shutdown logging error: {e}", flush=True)
    await close_http_session()


def main():

    load_dotenv(override=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    print("Kasper AI starting...")

    threading.Thread(target=run_health_server, daemon=True).start()

    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    bot = Bot(token=token)
    dp = Dispatcher(bot)

    register_handlers(dp)

    # Telegram Business Mode: патчит aiogram.bot.api.check_result (см.
    # router/business_raw_diag.py) — единственная точка, где ещё виден
    # сырой JSON апдейта до того, как aiogram отбросит незнакомые ему поля
    # business_connection/business_message при типизации в types.Update.
    # Оттуда же теперь реально диспетчеризуется обработка business-сообщений
    # (router/business.py: триггер "Каспер" -> AI -> ответ в тот же чат).
    # Прежняя диагностика в BusinessDiagnosticMiddleware (telegram/handlers.py)
    # смотрит на уже готовый Update и структурно не может найти эти поля —
    # оставлена как есть, но полагаться на неё для этой цели не стоит.
    patch_check_result_for_business_diag(bot)

    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        loop=loop,
    )


if __name__ == "__main__":
    main()
