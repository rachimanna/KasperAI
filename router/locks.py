"""Serialize conversations; remove locks after their last waiter."""
import asyncio
from contextlib import asynccontextmanager
from functools import wraps

_entries = {}


@asynccontextmanager
async def conversation_lock(key):
    entry = _entries.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _entries.pop(key, None)


def serialized_message(func):
    @wraps(func)
    async def wrapper(message, *args, **kwargs):
        if message.from_user is None:
            return
        async with conversation_lock(("chat", message.chat.id)):
            return await func(message, *args, **kwargs)
    return wrapper


def serialized_summary(func):
    @wraps(func)
    async def wrapper(user_id, chat_id):
        scope = chat_id if chat_id is not None else ("user", user_id)
        async with conversation_lock(("summary", scope)):
            return await func(user_id, chat_id)
    return wrapper


def serialized_lobby(func):
    @wraps(func)
    async def wrapper(event, *args, **kwargs):
        message = getattr(event, "message", None) or event
        async with conversation_lock(("lobby", message.chat.id)):
            return await func(event, *args, **kwargs)
    return wrapper


def serialized_game(func):
    @wraps(func)
    async def wrapper(bot, game_id, *args, **kwargs):
        async with conversation_lock(("game", game_id)):
            return await func(bot, game_id, *args, **kwargs)
    return wrapper
