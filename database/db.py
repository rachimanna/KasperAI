import os
import aiosqlite
from dotenv import load_dotenv

load_dotenv(override=True)

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/kasper.db")


async def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                username TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                provider TEXT DEFAULT 'auto',
                model TEXT,
                language TEXT DEFAULT 'auto',
                api_key TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        await db.commit()


async def get_or_create_user(telegram_id, username=None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO users (telegram_id, username)
            VALUES (?, ?)
            """,
            (telegram_id, username),
        )

        await db.execute(
            """
            UPDATE users
            SET username = ?
            WHERE telegram_id = ?
            """,
            (username, telegram_id),
        )

        await db.commit()

        cursor = await db.execute(
            "SELECT id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def save_message(user_id, role, content, chat_id=None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO messages (user_id, chat_id, role, content)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, chat_id, role, content),
        )
        await db.commit()


async def get_history(user_id, limit=20, chat_id=None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if chat_id is not None:
            cursor = await db.execute(
                """
                SELECT role, content
                FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            )
        else:
            cursor = await db.execute(
                """
                SELECT role, content
                FROM messages
                WHERE user_id = ? AND (chat_id IS NULL)
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )

        rows = await cursor.fetchall()
        rows.reverse()
        return rows


async def create_project(user_id, name, description=""):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO projects (user_id, name, description)
            VALUES (?, ?, ?)
            """,
            (user_id, name.strip(), description.strip()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_projects(user_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, name, description, created_at, updated_at
            FROM projects
            WHERE user_id = ?
            ORDER BY id DESC
            """,
            (user_id,),
        )
        return await cursor.fetchall()


async def get_project(user_id, name):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, name, description, created_at, updated_at
            FROM projects
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name.strip()),
        )
        return await cursor.fetchone()


async def delete_project(user_id, name):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            DELETE FROM projects
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name.strip()),
        )
        await db.commit()
        return cursor.rowcount > 0


async def set_user_api_key(user_id, provider, api_key):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (user_id, provider, api_key)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                provider = excluded.provider,
                api_key = excluded.api_key,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, provider, api_key),
        )
        await db.commit()


async def get_user_api_key(user_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT api_key, provider FROM settings WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row if row else (None, None)


ADMIN_TELEGRAM_IDS = {8957436007}


async def get_limit_status(user_id, daily_limit=20):
    """Возвращает (used: int, remaining: int) без увеличения счётчика."""
    from datetime import date
    today = str(date.today())

    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT request_count, last_date FROM usage_limits WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            return 0, daily_limit

        count, last_date = row
        if last_date != today:
            return 0, daily_limit

        return count, max(0, daily_limit - count)


async def check_and_increment_limit(user_id, daily_limit=20, telegram_id=None):
    """
    Проверяет дневной лимит запросов пользователя.
    Возвращает (allowed: bool, remaining: int).
    Если лимит не исчерпан — увеличивает счётчик и разрешает запрос.
    Сбрасывается автоматически каждый день (по дате last_date).
    Админы (ADMIN_TELEGRAM_IDS) не ограничены.
    """
    if telegram_id in ADMIN_TELEGRAM_IDS:
        return True, 999999

    from datetime import date
    today = str(date.today())

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usage_limits (
                user_id INTEGER PRIMARY KEY,
                request_count INTEGER DEFAULT 0,
                last_date TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.commit()

        cursor = await db.execute(
            "SELECT request_count, last_date FROM usage_limits WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

        if row is None:
            await db.execute(
                "INSERT INTO usage_limits (user_id, request_count, last_date) VALUES (?, 1, ?)",
                (user_id, today),
            )
            await db.commit()
            return True, daily_limit - 1

        count, last_date = row

        if last_date != today:
            await db.execute(
                "UPDATE usage_limits SET request_count = 1, last_date = ? WHERE user_id = ?",
                (today, user_id),
            )
            await db.commit()
            return True, daily_limit - 1

        if count >= daily_limit:
            return False, 0

        await db.execute(
            "UPDATE usage_limits SET request_count = request_count + 1 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()
        return True, daily_limit - count - 1
