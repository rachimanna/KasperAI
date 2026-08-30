import asyncio

from database import init_db, get_or_create_user
from router.context import (
    add_user_message,
    add_assistant_message,
    get_context,
)


async def main():
    await init_db()

    user_id = await get_or_create_user(
        telegram_id=999999991,
        username="context_test",
    )

    await add_user_message(
        user_id,
        "Создай Python-бота",
    )

    await add_assistant_message(
        user_id,
        "Хорошо, создадим Python-бота.",
    )

    await add_user_message(
        user_id,
        "Добавь туда базу данных.",
    )

    context = await get_context(user_id)

    print("Context: OK")

    for message in context:
        print(
            message["role"],
            ":",
            message["content"],
        )


asyncio.run(main())
