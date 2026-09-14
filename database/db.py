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

        # Миграция: бан пользователей (для админ-панели). CREATE TABLE
        # IF NOT EXISTS не добавляет колонки в уже существующую таблицу,
        # поэтому добавляем через ALTER TABLE и глушим ошибку, если
        # колонка уже была добавлена раньше.
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER,
                summary TEXT NOT NULL DEFAULT '',
                last_summarized_message_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'lobby',
                lobby_message_id INTEGER,
                phase_ends_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                username TEXT,
                role TEXT,
                is_alive INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(game_id, user_id),
                FOREIGN KEY (game_id) REFERENCES games(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Миграция: номер фазы (0 = лобби, 1 = первая ночь, ...).
        # CREATE TABLE IF NOT EXISTS не добавляет колонки в уже существующую
        # таблицу games, поэтому добавляем через ALTER TABLE и глушим ошибку,
        # если колонка уже была добавлена раньше.
        try:
            await db.execute("ALTER TABLE games ADD COLUMN phase_number INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                phase_number INTEGER NOT NULL,
                actor_user_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                target_user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(game_id, phase_number, actor_user_id),
                FOREIGN KEY (game_id) REFERENCES games(id)
            )
        """)

        # Агрегированная статистика игрока по игре "Теневой город" — одна
        # строка на пользователя, обновляется по факту завершения игры
        # (см. record_game_result). Отдельная таблица, а не подсчёт по
        # game_players на лету, потому что она нужна часто (команда
        # /gamestats) и не должна пересчитываться каждый раз со сканом
        # всех прошлых игр пользователя.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS player_stats (
                user_id INTEGER PRIMARY KEY,
                games_played INTEGER NOT NULL DEFAULT 0,
                games_won INTEGER NOT NULL DEFAULT 0,
                role_shadow_count INTEGER NOT NULL DEFAULT 0,
                role_detective_count INTEGER NOT NULL DEFAULT 0,
                role_doctor_count INTEGER NOT NULL DEFAULT 0,
                role_civilian_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Дневные лимиты запросов. РАНЬШЕ эта таблица создавалась только
        # внутри check_and_increment_limit(), поэтому get_limit_status()
        # (команда /limit) на свежей базе падала с "no such table:
        # usage_limits". Теперь она есть сразу после init_db.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS usage_limits (
                user_id INTEGER PRIMARY KEY,
                request_count INTEGER DEFAULT 0,
                last_date TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Индексы под реальные запросы истории: get_history и
        # get_messages_after всегда фильтруют по chat_id или user_id и
        # сортируют по id. Без индексов каждое сообщение бота = полный
        # скан таблицы messages, и чем дольше живёт бот, тем медленнее.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id, id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id, id)"
        )

        # Лог обращений к AI-провайдерам (для /status и /stats): каждая
        # попытка ask_provider() внутри router.ai_router.ask() пишет сюда
        # успех или ошибку. Без этого нельзя ответить, кто реально отвечал
        # и какой провайдер чаще падает — раньше это было видно только
        # в логах Render.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS provider_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                success INTEGER NOT NULL,
                error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_log_created_at ON provider_log(created_at)"
        )

        # WAL: читатели не блокируют писателя. Бот открывает соединение на
        # каждый запрос, и при нескольких активных чатах в обычном режиме
        # journal ловятся "database is locked".
        try:
            await db.execute("PRAGMA journal_mode=WAL")
        except Exception as e:
            print(f"[db] WAL enable error (non-fatal): {e}", flush=True)

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


async def find_telegram_id_by_username(username):
    username = username.lstrip("@").strip().lower()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT telegram_id FROM users
            WHERE LOWER(username) = ?
            LIMIT 1
            """,
            (username,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_user_banned(telegram_id, is_banned):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET is_banned = ? WHERE telegram_id = ?",
            (1 if is_banned else 0, telegram_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def is_user_banned(telegram_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT is_banned FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])


async def get_all_telegram_ids():
    """Список telegram_id всех известных боту пользователей — для /broadcast."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT telegram_id FROM users")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


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


async def get_conversation_summary(user_id, chat_id=None):
    """
    Возвращает (summary: str, last_summarized_message_id: int) для пользователя/чата.
    Если записи ещё нет — ("", 0).
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if chat_id is not None:
            cursor = await db.execute(
                """
                SELECT summary, last_summarized_message_id
                FROM conversation_summaries
                WHERE chat_id = ?
                """,
                (chat_id,),
            )
        else:
            cursor = await db.execute(
                """
                SELECT summary, last_summarized_message_id
                FROM conversation_summaries
                WHERE user_id = ? AND chat_id IS NULL
                """,
                (user_id,),
            )
        row = await cursor.fetchone()
        return row if row else ("", 0)


async def save_conversation_summary(user_id, summary, last_summarized_message_id, chat_id=None):
    """
    Апсерт вручную (SELECT -> UPDATE/INSERT), т.к. SQLite не дедуплицирует
    строки через UNIQUE/ON CONFLICT когда chat_id IS NULL (личка).
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if chat_id is not None:
            cursor = await db.execute(
                "SELECT id FROM conversation_summaries WHERE chat_id = ?",
                (chat_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT id FROM conversation_summaries WHERE user_id = ? AND chat_id IS NULL",
                (user_id,),
            )
        row = await cursor.fetchone()

        if row:
            await db.execute(
                """
                UPDATE conversation_summaries
                SET summary = ?, last_summarized_message_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (summary, last_summarized_message_id, row[0]),
            )
        else:
            await db.execute(
                """
                INSERT INTO conversation_summaries (user_id, chat_id, summary, last_summarized_message_id)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, chat_id, summary, last_summarized_message_id),
            )

        await db.commit()


async def get_messages_after(user_id, after_message_id, chat_id=None):
    """
    Возвращает [(id, role, content), ...] для сообщений с id > after_message_id,
    в хронологическом порядке (старые -> новые).
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if chat_id is not None:
            cursor = await db.execute(
                """
                SELECT id, role, content
                FROM messages
                WHERE chat_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (chat_id, after_message_id),
            )
        else:
            cursor = await db.execute(
                """
                SELECT id, role, content
                FROM messages
                WHERE user_id = ? AND chat_id IS NULL AND id > ?
                ORDER BY id ASC
                """,
                (user_id, after_message_id),
            )
        return await cursor.fetchall()


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


# Админы берутся ТОЛЬКО из переменных окружения (config/settings.py читает
# ADMIN_ID / ADMIN_IDS). Раньше здесь стоял захардкоженный id, из-за чего
# безлимит по запросам и права на /ban /unban /broadcast могли расходиться:
# один список в коде, другой в env. Теперь источник истины один.
# ВАЖНО: на Render в Environment должна быть переменная ADMIN_ID со твоим
# telegram_id, иначе безлимита не будет ни у кого.
from config.settings import ADMIN_IDS as _ADMIN_IDS

ADMIN_TELEGRAM_IDS = set(_ADMIN_IDS)


async def get_limit_status(user_id, daily_limit=20):
    """Возвращает (used: int, remaining: int) без увеличения счётчика."""
    from datetime import date
    today = str(date.today())

    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Страховка: если бот почему-то запустился без init_db (или база
        # подменена), /limit всё равно не должен падать с ошибкой.
        try:
            cursor = await db.execute(
                "SELECT request_count, last_date FROM usage_limits WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        except Exception as e:
            print(f"[db] get_limit_status error: {e}", flush=True)
            return 0, daily_limit

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
        # CREATE TABLE отсюда убран: таблица создаётся один раз в init_db.
        # Раньше DDL выполнялся на КАЖДОЕ сообщение любого пользователя.
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


async def create_game(chat_id):
    """
    Создаёт новую игру в статусе 'lobby' для чата.
    Возвращает id созданной игры.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO games (chat_id, status) VALUES (?, 'lobby')",
            (chat_id,),
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_game(chat_id):
    """
    Возвращает (id, chat_id, status, lobby_message_id, phase_ends_at,
    phase_number) для незавершённой игры в чате, или None если такой нет.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, chat_id, status, lobby_message_id, phase_ends_at, phase_number
            FROM games
            WHERE chat_id = ? AND status != 'finished'
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id,),
        )
        return await cursor.fetchone()


async def get_game_by_id(game_id):
    """
    То же самое, что get_active_game, но по id игры (не важно, завершена
    она или нет). Используется фоновым проверятелем таймеров.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, chat_id, status, lobby_message_id, phase_ends_at, phase_number
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        )
        return await cursor.fetchone()


async def get_games_with_expired_phase(now_iso):
    """
    Возвращает id всех игр в статусе 'night' или 'voting', у которых
    phase_ends_at уже наступил (<= now_iso, время в UTC ISO-строке).
    Используется фоновой задачей, которая переживает перезапуск бота,
    т.к. ничего не хранится в памяти процесса — только в БД.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id
            FROM games
            WHERE status IN ('night', 'voting')
              AND phase_ends_at IS NOT NULL
              AND phase_ends_at <= ?
            """,
            (now_iso,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def set_game_lobby_message(game_id, message_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE games SET lobby_message_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (message_id, game_id),
        )
        await db.commit()


async def set_game_status(game_id, status):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE games SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, game_id),
        )
        await db.commit()


async def add_game_player(game_id, user_id, telegram_id, username=None):
    """
    Добавляет игрока в игру. Если он уже там — ничего не делает.
    Возвращает True, если игрок был реально добавлен (не состоял раньше).
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO game_players (game_id, user_id, telegram_id, username)
            VALUES (?, ?, ?, ?)
            """,
            (game_id, user_id, telegram_id, username),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_game_players(game_id):
    """
    Возвращает список (id, user_id, telegram_id, username, role, is_alive)
    для всех игроков указанной игры.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, user_id, telegram_id, username, role, is_alive
            FROM game_players
            WHERE game_id = ?
            ORDER BY id ASC
            """,
            (game_id,),
        )
        return await cursor.fetchall()


async def is_player_in_game(game_id, user_id):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM game_players WHERE game_id = ? AND user_id = ?",
            (game_id, user_id),
        )
        row = await cursor.fetchone()
        return row is not None


async def set_game_phase(game_id, status, phase_ends_at, phase_number=None):
    """
    Переводит игру в новую фазу (night / voting / finished и т.д.) и
    записывает время окончания фазы в БД (используется фоновым таймером,
    который переживает перезапуск/пересыпание бота на Render).
    Если phase_number передан — обновляет и его (иначе оставляет как есть).
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if phase_number is None:
            await db.execute(
                """
                UPDATE games
                SET status = ?, phase_ends_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, phase_ends_at, game_id),
            )
        else:
            await db.execute(
                """
                UPDATE games
                SET status = ?, phase_ends_at = ?, phase_number = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, phase_ends_at, phase_number, game_id),
            )
        await db.commit()


async def assign_game_roles(game_id, role_by_user_id):
    """
    role_by_user_id: словарь {user_id: role} ('shadow' / 'detective' / 'civilian').
    Раздаёт роли всем игрокам игры за один заход.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for user_id, role in role_by_user_id.items():
            await db.execute(
                "UPDATE game_players SET role = ? WHERE game_id = ? AND user_id = ?",
                (role, game_id, user_id),
            )
        await db.commit()


async def set_player_alive(game_id, user_id, is_alive):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE game_players SET is_alive = ? WHERE game_id = ? AND user_id = ?",
            (1 if is_alive else 0, game_id, user_id),
        )
        await db.commit()


async def save_game_action(game_id, phase_number, actor_user_id, action_type, target_user_id):
    """
    Сохраняет выбор игрока за фазу (ночное действие Тени/Детектива или
    дневной голос). Если игрок передумал и жмёт другую кнопку в той же
    фазе — выбор просто перезаписывается (INSERT OR REPLACE).
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO game_actions
                (game_id, phase_number, actor_user_id, action_type, target_user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (game_id, phase_number, actor_user_id, action_type, target_user_id),
        )
        await db.commit()


async def get_game_action(game_id, phase_number, actor_user_id):
    """Возвращает target_user_id выбора игрока в этой фазе, или None."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT target_user_id FROM game_actions
            WHERE game_id = ? AND phase_number = ? AND actor_user_id = ?
            """,
            (game_id, phase_number, actor_user_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_actions_by_type(game_id, phase_number, action_type):
    """Возвращает список (actor_user_id, target_user_id) для этой фазы/типа."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT actor_user_id, target_user_id FROM game_actions
            WHERE game_id = ? AND phase_number = ? AND action_type = ?
            """,
            (game_id, phase_number, action_type),
        )
        return await cursor.fetchall()


_ROLE_STATS_COLUMN = {
    "shadow": "role_shadow_count",
    "detective": "role_detective_count",
    "doctor": "role_doctor_count",
    "civilian": "role_civilian_count",
}


async def record_player_game_result(user_id, role, won):
    """
    Обновляет агрегированную статистику одного игрока после завершения
    партии: +1 к сыгранным играм, +1 к победам (если won=True), и +1 к
    счётчику той роли, в которой он играл. INSERT OR IGNORE создаёт
    строку при первой игре пользователя, затем UPDATE инкрементирует —
    без этого UPDATE на несуществующей строке молча ничего не изменит.
    """
    role_column = _ROLE_STATS_COLUMN.get(role)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO player_stats (user_id) VALUES (?)",
            (user_id,),
        )

        set_clauses = [
            "games_played = games_played + 1",
            "games_won = games_won + ?",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        params = [1 if won else 0]

        if role_column:
            set_clauses.append(f"{role_column} = {role_column} + 1")

        params.append(user_id)

        await db.execute(
            f"UPDATE player_stats SET {', '.join(set_clauses)} WHERE user_id = ?",
            params,
        )
        await db.commit()


async def get_player_stats(user_id):
    """
    Возвращает dict со статистикой игрока, либо None, если он ещё ни разу
    не доигрывал партию "Теневой город" до конца.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT games_played, games_won, role_shadow_count,
                   role_detective_count, role_doctor_count, role_civilian_count
            FROM player_stats WHERE user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "games_played": row[0],
            "games_won": row[1],
            "role_shadow_count": row[2],
            "role_detective_count": row[3],
            "role_doctor_count": row[4],
            "role_civilian_count": row[5],
        }


async def log_provider_attempt(provider, success, error=None):
    """
    Записывает одну попытку обращения к AI-провайдеру (успех/ошибка).
    Вызывается из router.ai_router.ask() на каждую попытку — используется
    командами /status (кто отвечал последним) и /stats (кто чаще падает).
    Ошибка записи в лог не должна ронять сам ответ пользователю, поэтому
    вызывающий код оборачивает это в try/except.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO provider_log (provider, success, error) VALUES (?, ?, ?)",
            (provider, 1 if success else 0, error),
        )
        await db.commit()


async def get_last_successful_provider():
    """
    Возвращает (provider, created_at) последнего успешного ответа AI,
    либо None, если провайдеры ещё ни разу не отвечали.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT provider, created_at FROM provider_log
            WHERE success = 1
            ORDER BY id DESC LIMIT 1
            """
        )
        row = await cursor.fetchone()
        return (row[0], row[1]) if row else None


async def get_provider_stats(hours=24):
    """
    Возвращает список dict {provider, total, failed} за последние `hours`
    часов, отсортированный по числу ошибок по убыванию — используется в
    /stats, чтобы увидеть, какой провайдер чаще падает.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT provider,
                   COUNT(*) AS total,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed
            FROM provider_log
            WHERE created_at >= datetime('now', ?)
            GROUP BY provider
            ORDER BY failed DESC, total DESC
            """,
            (f"-{hours} hours",),
        )
        rows = await cursor.fetchall()
        return [
            {"provider": r[0], "total": r[1], "failed": r[2]}
            for r in rows
        ]


async def get_total_users_count():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_active_users_count(hours=24):
    """Число уникальных пользователей, написавших сообщение за последние `hours` часов."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(DISTINCT user_id) FROM messages
            WHERE role = 'user' AND created_at >= datetime('now', ?)
            """,
            (f"-{hours} hours",),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_total_messages_count(hours=None):
    """
    Общее число сообщений (role='user'). Если передан `hours` — только
    за последние `hours` часов, иначе за всё время.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if hours is None:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE role = 'user'"
            )
            row = await cursor.fetchone()
        else:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE role = 'user' AND created_at >= datetime('now', ?)
                """,
                (f"-{hours} hours",),
            )
            row = await cursor.fetchone()
        return row[0] if row else 0
