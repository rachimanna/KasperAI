from database.db import get_db

# ВАЖНО: раньше этот модуль открывал собственное соединение через
# aiosqlite.connect(config.settings.DATABASE_PATH). database/db.py при
# этом использует свою переменную окружения DB_PATH (по умолчанию
# "kasper.db"), а не DATABASE_PATH (по умолчанию "data/kasper.db") —
# из-за этого memory.py фактически писал/читал из ДРУГОГО файла базы,
# в котором таблица "memory" из init_db() никогда не создавалась.
# Теперь используем общее соединение get_db() из database/db.py — там
# же живёт и CREATE TABLE IF NOT EXISTS memory (см. init_db()).


async def add_memory(user_id, content):
    db = await get_db()
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
    db = await get_db()
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
    db = await get_db()
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
    db = await get_db()
    await db.execute(
        "DELETE FROM memory WHERE user_id = ?",
        (user_id,),
    )
    await db.commit()
