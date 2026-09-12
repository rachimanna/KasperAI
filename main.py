import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram import executor

from database.db import init_db
from router.ai_router import init_http_session, close_http_session
from router.game_logic import phase_checker_loop
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


async def on_startup(dp):
    await init_http_session()
    await init_db()

    from aiogram.types import BotCommand, BotCommandScopeChat

    # Полностью очищаем старые команды Telegram,
    # включая оставшийся /osint в меню.
    try:
        await dp.bot.delete_my_commands()
    except Exception as e:
        print(f"[Commands] default scope cleanup error: {e}", flush=True)

    # Устанавливаем актуальные команды для всех пользователей.
    await dp.bot.set_my_commands([
        BotCommand("start", "Запустить бота"),
        BotCommand("limit", "Мой лимит запросов"),
        BotCommand("game", "Начать игру «Теневой город»"),
        BotCommand("stopgame", "Остановить текущую игру"),
    ])

    # Админские команды показываем в подсказках только самим админам —
    # через scope=BotCommandScopeChat(chat_id=admin_id), а не в общем
    # меню, иначе их увидят и смогут попытаться вызвать все пользователи.
    admin_commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("limit", "Мой лимит запросов"),
        BotCommand("game", "Начать игру «Теневой город»"),
        BotCommand("stopgame", "Остановить текущую игру"),
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

    print("Database: OK")
    print("Kasper AI is running.")


async def on_shutdown(dp):
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

    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        loop=loop,
    )


if __name__ == "__main__":
    main()
