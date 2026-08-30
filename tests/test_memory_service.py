import asyncio

from database import init_db, get_or_create_user
from memory.service import (
    remember,
    get_user_memory,
    forget_all,
)


async def main():
    await init_db()

    user_id = await get_or_create_user(
        telegram_id=999999992,
        username="memory_service_test",
    )

    await forget_all(user_id)

    await remember(
        user_id,
        "Пользователь изучает Python.",
    )

    memories = await get_user_memory(user_id)

    print("Memory service: OK")
    print("Memories:", len(memories))

    for item in memories:
        print(item)


asyncio.run(main())
