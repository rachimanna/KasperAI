from memory.memory import (
    add_memory,
    get_memories,
    delete_memory,
    clear_memory,
)


async def remember(user_id, text):
    return await add_memory(
        user_id,
        text,
    )


async def get_user_memory(user_id):
    return await get_memories(user_id)


async def forget(user_id, memory_id):
    return await delete_memory(
        user_id,
        memory_id,
    )


async def forget_all(user_id):
    return await clear_memory(user_id)
