import asyncio
import logging
import os

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram import executor

from database.db import init_db
from telegram.handlers import register_handlers


async def on_startup(dp):
    await init_db()

    from aiogram.types import BotCommand
    await dp.bot.set_my_commands([
        BotCommand("start", "Запустить бота"),
        BotCommand("limit", "Мой лимит запросов"),
    ])

    print("Database: OK")
    print("Kasper AI is running.")


def main():
    load_dotenv(override=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    print("Kasper AI starting...")

    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    bot = Bot(token=token)
    dp = Dispatcher(bot)

    register_handlers(dp)

    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
    )


if __name__ == "__main__":
    main()
