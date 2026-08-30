import aiosqlite
from config.settings import DATABASE_PATH


async def add_memory(user_id, content):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO memory (user_id, content)
            VALUES (?, ?)
            """,
            (user_id, content.strip()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_memories(user_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, content, created_at
            FROM memory
            WHERE user_id = ?
            ORDER BY id ASC
            """,
            (user_id,),
        )
        return await cursor.fetchall()


async def delete_memory(user_id, memory_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            DELETE FROM memory
            WHERE id = ? AND user_id = ?
            """,
            (memory_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def clear_memory(user_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM memory WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()
