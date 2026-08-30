import asyncio

from database import init_db, get_or_create_user
from memory import (
    add_memory,
    get_memories,
    delete_memory,
    clear_memory,
)


async def main():
    await init_db()

    user_id = await get_or_create_user(
        telegram_id=888888888,
        username="memory_test",
    )

    memory_id = await add_memory(
        user_id,
        "Пользователь тестирует Kasper AI в Termux.",
    )

    memories = await get_memories(user_id)

    print("Memory add: OK")
    print("Memory ID:", memory_id)
    print("Memories:", memories)

    deleted = await delete_memory(user_id, memory_id)

    print("Memory delete:", "OK" if deleted else "ERROR")

    await add_memory(user_id, "Временная память для теста.")
    await clear_memory(user_id)

    remaining = await get_memories(user_id)

    print("Memory clear:", "OK" if not remaining else "ERROR")


asyncio.run(main())
