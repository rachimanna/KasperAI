import os

from aiogram import Bot, Dispatcher

from telegram.handlers import router


def create_bot():
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    return Bot(token=token)


def create_dispatcher(bot):
    dp = Dispatcher(bot)

    dp.include_router(router)

    return dp
