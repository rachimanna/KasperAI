import asyncio

from database import (
    init_db,
    get_or_create_user,
    save_message,
    get_history,
)


async def main():
    await init_db()

    user_id = await get_or_create_user(
        telegram_id=999999999,
        username="test_user",
    )

    await save_message(
        user_id,
        "user",
        "Тестовое сообщение",
    )

    history = await get_history(user_id)

    print("Database: OK")
    print("User ID:", user_id)
    print("History:", history)


asyncio.run(main())
