from database import save_message, get_history


async def add_user_message(user_id, content):
    await save_message(user_id, "user", content)


async def add_assistant_message(user_id, content):
    await save_message(user_id, "assistant", content)


async def get_context(user_id, limit=20):
    history = await get_history(user_id, limit)

    return [
        {
            "role": role,
            "content": content,
        }
        for role, content in history
    ]
