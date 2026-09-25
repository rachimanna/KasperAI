import aiosqlite
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
import asyncio

from config.settings import ADMIN_IDS
from router.time_awareness import now_in, utc_now_str

# DATABASE_PATH — новый основной параметр. DB_PATH поддержан для старых
# деплоев, чтобы существующая БД на Render не потерялась при обновлении.
DB_PATH = os.getenv("DATABASE_PATH") or os.getenv("DB_PATH") or "data/kasper.db"

_db_conn = None
_db_lock = asyncio.Lock()


async def get_db():
    """
    Возвращает единственное соединение с БД (singleton).
    """
    global _db_conn
    if _db_conn is None:
        Path(DB_PATH).expanduser().parent.mkdir(parents=True, exist_ok=True)
        _db_conn = await aiosqlite.connect(DB_PATH)
        _db_conn.row_factory = aiosqlite.Row
        await _db_conn.execute("PRAGMA foreign_keys = ON")
        await _db_conn.execute("PRAGMA busy_timeout = 5000")
        await _db_conn.execute("PRAGMA journal_mode = WAL")
    return _db_conn


async def close_db():
    """Корректно закрывает SQLite-соединение при остановке процесса."""
    global _db_conn
    if _db_conn is not None:
        try:
            await _db_conn.close()
        finally:
            _db_conn = None


async def init_db():
    """
    Создаёт таблицы, если их нет.
    """
    db = await get_db()

    # Таблица пользователей
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Таблица сообщений
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    # Таблица саммари
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER,
            summary TEXT,
            last_summarized_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    # Таблица лимитов
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_limits (
            user_id INTEGER PRIMARY KEY,
            request_count INTEGER DEFAULT 0,
            last_date TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    # Дневные лимиты AI-агента — теперь тоже в SQLite и переживают рестарт.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_usage_limits (
            user_id INTEGER PRIMARY KEY,
            request_count INTEGER NOT NULL DEFAULT 0,
            last_date TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    # Ускоряем выборки сообщений.
    # Индексы игровых таблиц создаются ниже, после CREATE TABLE.
    await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_id_id ON messages(chat_id, id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_private ON messages(user_id, id)")

    # Таблица игр
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            phase TEXT DEFAULT 'night',
            day_number INTEGER DEFAULT 1,
            phase_end_time REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Таблица игроков
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS game_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            role TEXT,
            is_alive INTEGER DEFAULT 1,
            votes_received INTEGER DEFAULT 0,
            FOREIGN KEY(game_id) REFERENCES games(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    # Таблица статистики игроков
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS player_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            games_played INTEGER DEFAULT 0,
            games_won INTEGER DEFAULT 0,
            role_shadow_count INTEGER DEFAULT 0,
            role_hunter_count INTEGER DEFAULT 0,
            role_seer_count INTEGER DEFAULT 0,
            role_healer_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    # Таблица действий в игре (ходы Тени/Детектива/Доктора, голоса)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS game_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            phase_number INTEGER NOT NULL,
            actor_user_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            target_user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(game_id) REFERENCES games(id)
        )
        """
    )

    # Индексы игровых таблиц создаём только после создания самих таблиц.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_players_game_user "
        "ON game_players(game_id, user_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_game_actions_lookup "
        "ON game_actions(game_id, phase_number, actor_user_id, action_type)"
    )

    # Таблица попыток обращения к AI-провайдерам (для /status и /stats)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            success INTEGER NOT NULL,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Таблица заметок "памяти" пользователя (memory/memory.py)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    # Таблица проектов пользователя (projects/service.py)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    # ---- Миграции существующих таблиц (ALTER TABLE ADD COLUMN) ----
    # Нужны, потому что на проде (Render) уже может существовать kasper.db
    # со старой схемой games/game_players/users/player_stats — CREATE TABLE
    # IF NOT EXISTS их не тронет, а новой логике (router/game_logic.py,
    # telegram/handlers.py) нужны дополнительные колонки.
    await _ensure_column(db, "games", "status", "TEXT DEFAULT 'lobby'")
    await _ensure_column(db, "games", "lobby_message_id", "INTEGER")
    await _ensure_column(db, "games", "phase_ends_at", "TEXT")
    await _ensure_column(db, "games", "phase_number", "INTEGER DEFAULT 1")

    await _ensure_column(db, "game_players", "telegram_id", "INTEGER")

    await _ensure_column(db, "users", "is_banned", "INTEGER DEFAULT 0")

    await _ensure_column(db, "player_stats", "role_detective_count", "INTEGER DEFAULT 0")
    await _ensure_column(db, "player_stats", "role_doctor_count", "INTEGER DEFAULT 0")
    await _ensure_column(db, "player_stats", "role_civilian_count", "INTEGER DEFAULT 0")

    # Время сообщений: в старой (боевой) БД колонка называется created_at,
    # а в свежей схеме раньше была timestamp — из-за этого /stats падал.
    # Теперь везде created_at; save_message пишет её явно (UTC).
    await _ensure_column(db, "messages", "created_at", "TEXT")

    # Часовой пояс пользователя (IANA-имя), NULL -> BOT_TIMEZONE.
    await _ensure_column(db, "users", "timezone", "TEXT")

    await _fix_usage_limits_schema(db)

    # Напоминания («напомни через 20 минут …»). due_at — UTC.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            telegram_chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            due_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            sent INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, due_at)"
    )

    await db.commit()


async def _fix_usage_limits_schema(db):
    """
    БАГ: в свежей схеме usage_limits был id AUTOINCREMENT, а user_id НЕ
    уникальный. check_and_increment_limit делает ON CONFLICT(user_id), и
    SQLite на такой таблице кидает ошибку -> на новой БД бот падал на
    каждом сообщении. Если таблица уже создана с этой схемой — пересобираем
    её с user_id PRIMARY KEY (берём максимальный счётчик на пользователя).
    """
    cursor = await db.execute("PRAGMA table_info(usage_limits)")
    cols = {row[1]: row[5] for row in await cursor.fetchall()}  # name -> pk
    if cols.get("user_id"):
        return
    await db.execute("ALTER TABLE usage_limits RENAME TO usage_limits_old")
    await db.execute(
        """
        CREATE TABLE usage_limits (
            user_id INTEGER PRIMARY KEY,
            request_count INTEGER DEFAULT 0,
            last_date TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    await db.execute(
        """
        INSERT OR REPLACE INTO usage_limits(user_id, request_count, last_date)
        SELECT user_id, MAX(request_count), MAX(last_date)
        FROM usage_limits_old GROUP BY user_id
        """
    )
    await db.execute("DROP TABLE usage_limits_old")


async def _ensure_column(db, table, column, definition):
    """
    Добавляет колонку в таблицу, если её ещё нет (SQLite не поддерживает
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS, поэтому проверяем вручную
    через PRAGMA table_info). Идемпотентно — безопасно вызывать при
    каждом старте бота.
    """
    cursor = await db.execute(f"PRAGMA table_info({table})")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if column not in existing_columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def get_or_create_user(telegram_id, username=None):
    """
    Возвращает user_id из БД, создаёт юзера если его нет.
    """
    db = await get_db()

    # ФИКС #3: один запрос вместо двух (INSERT OR IGNORE + UPDATE)
    await db.execute(
        """
        INSERT INTO users (telegram_id, username) VALUES (?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
        """,
        (telegram_id, username),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def save_message(user_id, role, content, chat_id=None):
    """
    Сохраняет сообщение в БД.
    """
    db = await get_db()
    await db.execute(
        "INSERT INTO messages (user_id, chat_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, chat_id, role, content, utc_now_str()),
    )
    await db.commit()


async def get_history(user_id, limit=20, chat_id=None):
    """
    Возвращает последние N сообщений пользователя.
    Если chat_id указан — для группы, иначе — личка.
    """
    db = await get_db()

    if chat_id is not None:
        cursor = await db.execute(
            """
            SELECT m.role,
                   CASE
                       WHEN m.role = 'user' AND u.username IS NOT NULL AND u.username != ''
                       THEN '[' || u.username || ']: ' || m.content
                       ELSE m.content
                   END AS content,
                   m.created_at AS created_at
            FROM messages m
            JOIN users u ON u.id = m.user_id
            WHERE m.chat_id = ?
            ORDER BY m.id DESC LIMIT ?
            """,
            (chat_id, limit),
        )
    else:
        # Личка: фильтруем только сообщения без chat_id
        cursor = await db.execute(
            "SELECT role, content, created_at FROM messages WHERE user_id = ? AND (chat_id IS NULL) ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    rows = await cursor.fetchall()
    rows.reverse()

    # ВНИМАНИЕ: возвращается список СЛОВАРЕЙ {"role", "content", "created_at"}.
    # Раньше telegram/handlers.py распаковывал их как кортежи
    # (`for role, content in history`) — а распаковка dict даёт КЛЮЧИ, и в
    # модель улетали сообщения role="role", content="content". Из-за этого
    # бот фактически не видел последние сообщения диалога.
    result = []
    for row in rows:
        if isinstance(row, dict) or hasattr(row, "keys"):
            keys = row.keys()
            result.append({
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"] if "created_at" in keys else None,
            })
        else:
            result.append({
                "role": row[0],
                "content": row[1],
                "created_at": row[2] if len(row) > 2 else None,
            })

    return result


async def get_last_user_message_time(user_id, chat_id=None):
    """Время (строка UTC) последнего сообщения ЭТОГО пользователя в диалоге."""
    db = await get_db()
    if chat_id is not None:
        cursor = await db.execute(
            "SELECT created_at FROM messages WHERE chat_id = ? AND user_id = ? AND role = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, user_id),
        )
    else:
        cursor = await db.execute(
            "SELECT created_at FROM messages WHERE user_id = ? AND chat_id IS NULL AND role = 'user' "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
    row = await cursor.fetchone()
    return row[0] if row else None


async def get_last_message_id(user_id, chat_id=None):
    """
    Возвращает ID последнего сообщения пользователя.
    """
    db = await get_db()

    if chat_id is not None:
        cursor = await db.execute(
            "SELECT id FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT id FROM messages WHERE user_id = ? AND (chat_id IS NULL) ORDER BY id DESC LIMIT 1",
            (user_id,),
        )

    row = await cursor.fetchone()
    return row[0] if row else None


async def save_conversation_summary(user_id, summary, last_summarized_message_id, chat_id=None):
    """Сохраняет summary. Для групп summary общий на chat_id."""
    db = await get_db()
    if chat_id is not None:
        cursor = await db.execute(
            "SELECT id FROM conversation_summaries WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT id FROM conversation_summaries WHERE user_id = ? AND chat_id IS NULL LIMIT 1",
            (user_id,),
        )
    row = await cursor.fetchone()
    if row:
        await db.execute(
            "UPDATE conversation_summaries SET summary = ?, last_summarized_message_id = ? WHERE id = ?",
            (summary, last_summarized_message_id, row[0]),
        )
    else:
        await db.execute(
            "INSERT INTO conversation_summaries (user_id, chat_id, summary, last_summarized_message_id) VALUES (?, ?, ?, ?)",
            (user_id, chat_id, summary, last_summarized_message_id),
        )
    await db.commit()


async def get_conversation_summary(user_id, chat_id=None):
    db = await get_db()
    if chat_id is not None:
        cursor = await db.execute(
            "SELECT summary, last_summarized_message_id FROM conversation_summaries WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT summary, last_summarized_message_id FROM conversation_summaries WHERE user_id = ? AND chat_id IS NULL LIMIT 1",
            (user_id,),
        )
    row = await cursor.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _limit_day():
    """
    «Сегодня» для дневных лимитов. Раньше check_and_increment_limit считал
    день по UTC, а get_usage_count (/limit) — по локальному времени сервера,
    и около полуночи /limit показывал не то. Теперь везде один источник —
    часовой пояс бота (BOT_TIMEZONE, по умолчанию Москва): лимит
    сбрасывается в полночь по этому поясу.
    """
    return now_in().strftime("%Y-%m-%d")


async def check_and_increment_limit(user_id, daily_limit=20, telegram_id=None):
    """Атомарно проверяет и увеличивает дневной лимит. Админы — без лимита."""
    # БАГ: параметр telegram_id раньше вообще не использовался, и админы
    # упирались в лимит, хотя /limit писал им «безлимит».
    if telegram_id is not None and telegram_id in ADMIN_IDS:
        return True, daily_limit
    db = await get_db()
    today = _limit_day()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO usage_limits(user_id, request_count, last_date)
            VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, today),
        )
        await db.execute(
            """
            UPDATE usage_limits
            SET request_count = CASE WHEN last_date = ? THEN request_count ELSE 0 END,
                last_date = ?
            WHERE user_id = ?
            """,
            (today, today, user_id),
        )
        cursor = await db.execute(
            """
            UPDATE usage_limits
            SET request_count = request_count + 1
            WHERE user_id = ? AND last_date = ? AND request_count < ?
            RETURNING request_count
            """,
            (user_id, today, daily_limit),
        )
        row = await cursor.fetchone()
        await db.commit()
    if row is None:
        return False, 0
    used = int(row[0])
    return True, max(0, daily_limit - used)


async def get_usage_count(user_id):
    """
    Возвращает количество запросов за сегодня.
    """
    db = await get_db()
    today = _limit_day()

    cursor = await db.execute(
        "SELECT request_count, last_date FROM usage_limits WHERE user_id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()

    if row is None:
        return 0

    if row[1] != today:
        return 0

    return row[0]


async def check_and_increment_agent_limit(user_id, daily_limit=15):
    """Атомарный дневной лимит агента, сохраняемый в SQLite."""
    db = await get_db()
    today = _limit_day()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO agent_usage_limits(user_id, request_count, last_date)
            VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, today),
        )
        await db.execute(
            """
            UPDATE agent_usage_limits
            SET request_count = CASE WHEN last_date = ? THEN request_count ELSE 0 END,
                last_date = ?
            WHERE user_id = ?
            """,
            (today, today, user_id),
        )
        cursor = await db.execute(
            """
            UPDATE agent_usage_limits
            SET request_count = request_count + 1
            WHERE user_id = ? AND last_date = ? AND request_count < ?
            RETURNING request_count
            """,
            (user_id, today, daily_limit),
        )
        row = await cursor.fetchone()
        await db.commit()
    if row is None:
        return False, 0
    return True, max(0, daily_limit - int(row[0]))


# ==================== ИГРА ====================

GAME_ROW_COLUMNS = "id, chat_id, status, lobby_message_id, phase_ends_at, phase_number"


async def create_game(chat_id):
    """
    Создаёт новую игру в чате (лобби, фаза 1).
    """
    db = await get_db()
    await db.execute(
        """
        INSERT INTO games (chat_id, status, lobby_message_id, phase_ends_at, phase_number)
        VALUES (?, 'lobby', NULL, NULL, 1)
        """,
        (chat_id,),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT id FROM games WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def get_game_by_id(game_id):
    """
    Возвращает игру по id как (id, chat_id, status, lobby_message_id,
    phase_ends_at, phase_number), либо None.
    """
    db = await get_db()
    cursor = await db.execute(
        f"SELECT {GAME_ROW_COLUMNS} FROM games WHERE id = ?",
        (game_id,),
    )
    return await cursor.fetchone()


async def get_active_game(chat_id):
    """
    Возвращает последнюю не завершённую игру в чате (lobby/night/voting),
    в том же формате, что и get_game_by_id. None, если такой нет.
    """
    db = await get_db()
    cursor = await db.execute(
        f"""
        SELECT {GAME_ROW_COLUMNS} FROM games
        WHERE chat_id = ? AND status != 'finished'
        ORDER BY id DESC LIMIT 1
        """,
        (chat_id,),
    )
    return await cursor.fetchone()


async def set_game_status(game_id, status):
    """
    Обновляет статус игры (lobby/night/voting/finished).
    """
    db = await get_db()
    await db.execute(
        "UPDATE games SET status = ? WHERE id = ?",
        (status, game_id),
    )
    await db.commit()


async def set_game_lobby_message(game_id, message_id):
    """
    Сохраняет id сообщения с лобби (для редактирования по мере захода игроков).
    """
    db = await get_db()
    await db.execute(
        "UPDATE games SET lobby_message_id = ? WHERE id = ?",
        (message_id, game_id),
    )
    await db.commit()


async def set_game_phase(game_id, status=None, phase_ends_at=None, phase_number=None):
    """
    Обновляет фазу игры: любую комбинацию status/phase_ends_at/phase_number.
    Поля, не переданные явно (оставленные None), не трогаются — кроме
    phase_ends_at, для которого None -> явный сброс (используется при
    завершении игры, см. router/game_logic.py: status="finished", phase_ends_at=None).
    """
    db = await get_db()

    set_clauses = []
    params = []

    if status is not None:
        set_clauses.append("status = ?")
        params.append(status)

    set_clauses.append("phase_ends_at = ?")
    params.append(phase_ends_at)

    if phase_number is not None:
        set_clauses.append("phase_number = ?")
        params.append(phase_number)

    params.append(game_id)

    await db.execute(
        f"UPDATE games SET {', '.join(set_clauses)} WHERE id = ?",
        params,
    )
    await db.commit()


async def get_games_with_expired_phase(now_iso):
    """
    Возвращает id всех игр в фазе night/voting, у которых phase_ends_at
    уже наступил (ISO-строка UTC, сравнение лексикографическое работает
    для этого формата).
    """
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT id FROM games
        WHERE status IN ('night', 'voting')
          AND phase_ends_at IS NOT NULL
          AND phase_ends_at <= ?
        """,
        (now_iso,),
    )
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def add_game_player(game_id, user_id, telegram_id, username=None):
    """
    Добавляет игрока в игру, если его там ещё нет.
    Возвращает True, если добавлен, False — если уже был в игре.
    """
    if await is_player_in_game(game_id, user_id):
        return False

    db = await get_db()
    await db.execute(
        """
        INSERT INTO game_players (game_id, user_id, telegram_id, username, role, is_alive)
        VALUES (?, ?, ?, ?, NULL, 1)
        """,
        (game_id, user_id, telegram_id, username),
    )
    await db.commit()
    return True


async def is_player_in_game(game_id, user_id):
    """
    Проверяет, участвует ли пользователь в игре.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM game_players WHERE game_id = ? AND user_id = ? LIMIT 1",
        (game_id, user_id),
    )
    row = await cursor.fetchone()
    return row is not None


async def get_game_players(game_id):
    """
    Возвращает игроков игры как список (id, user_id, telegram_id,
    username, role, is_alive), упорядоченных по id.
    """
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT id, user_id, telegram_id, username, role, is_alive
        FROM game_players WHERE game_id = ? ORDER BY id
        """,
        (game_id,),
    )
    return await cursor.fetchall()


async def assign_game_roles(game_id, role_by_user_id):
    """
    Проставляет роли игрокам. role_by_user_id: {user_id: role}.
    """
    db = await get_db()
    for user_id, role in role_by_user_id.items():
        await db.execute(
            "UPDATE game_players SET role = ? WHERE game_id = ? AND user_id = ?",
            (role, game_id, user_id),
        )
    await db.commit()


async def set_player_alive(game_id, user_id, alive):
    """
    Устанавливает жив/мёртв для игрока.
    """
    db = await get_db()
    await db.execute(
        "UPDATE game_players SET is_alive = ? WHERE game_id = ? AND user_id = ?",
        (1 if alive else 0, game_id, user_id),
    )
    await db.commit()


async def save_game_action(game_id, phase_number, actor_user_id, action_type, target_user_id):
    """
    Сохраняет ночное действие/голос игрока. Если этот же игрок уже
    делал выбор этого типа в этой фазе — старый выбор заменяется новым
    (например, если игрок нажал другую кнопку до истечения таймера).
    """
    db = await get_db()
    await db.execute(
        """
        DELETE FROM game_actions
        WHERE game_id = ? AND phase_number = ? AND actor_user_id = ? AND action_type = ?
        """,
        (game_id, phase_number, actor_user_id, action_type),
    )
    await db.execute(
        """
        INSERT INTO game_actions (game_id, phase_number, actor_user_id, action_type, target_user_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (game_id, phase_number, actor_user_id, action_type, target_user_id),
    )
    await db.commit()


async def get_game_action(game_id, phase_number, actor_user_id, action_type):
    """
    Возвращает target_user_id конкретного действия одного игрока, либо None.
    """
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT target_user_id FROM game_actions
        WHERE game_id = ? AND phase_number = ? AND actor_user_id = ? AND action_type = ?
        """,
        (game_id, phase_number, actor_user_id, action_type),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def get_actions_by_type(game_id, phase_number, action_type):
    """
    Возвращает все действия данного типа в фазе как список
    (actor_user_id, target_user_id), в порядке совершения.
    """
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT actor_user_id, target_user_id FROM game_actions
        WHERE game_id = ? AND phase_number = ? AND action_type = ?
        ORDER BY id
        """,
        (game_id, phase_number, action_type),
    )
    return await cursor.fetchall()


async def add_player(game_id, user_id, username, role):
    """
    Добавляет игрока в игру.
    """
    db = await get_db()
    await db.execute(
        "INSERT INTO game_players (game_id, user_id, username, role) VALUES (?, ?, ?, ?)",
        (game_id, user_id, username, role),
    )
    await db.commit()


async def get_players(game_id, alive_only=False):
    """
    Возвращает игроков игры.
    """
    db = await get_db()

    if alive_only:
        cursor = await db.execute(
            "SELECT user_id, username, role, is_alive FROM game_players WHERE game_id = ? AND is_alive = 1",
            (game_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT user_id, username, role, is_alive FROM game_players WHERE game_id = ?",
            (game_id,),
        )

    rows = await cursor.fetchall()
    return [
        {
            "user_id": row[0],
            "username": row[1],
            "role": row[2],
            "is_alive": row[3],
        }
        for row in rows
    ]


async def get_player_role(game_id, user_id):
    """
    Возвращает роль игрока.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT role FROM game_players WHERE game_id = ? AND user_id = ?",
        (game_id, user_id),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def kill_player(game_id, user_id):
    """
    Убивает игрока.
    """
    db = await get_db()
    await db.execute(
        "UPDATE game_players SET is_alive = 0 WHERE game_id = ? AND user_id = ?",
        (game_id, user_id),
    )
    await db.commit()


async def update_game_phase(game_id, new_phase, day_number=None, phase_end_time=None):
    """
    Обновляет фазу игры.
    """
    db = await get_db()

    if day_number is not None and phase_end_time is not None:
        await db.execute(
            "UPDATE games SET phase = ?, day_number = ?, phase_end_time = ? WHERE id = ?",
            (new_phase, day_number, phase_end_time, game_id),
        )
    elif phase_end_time is not None:
        await db.execute(
            "UPDATE games SET phase = ?, phase_end_time = ? WHERE id = ?",
            (new_phase, phase_end_time, game_id),
        )
    else:
        await db.execute(
            "UPDATE games SET phase = ? WHERE id = ?",
            (new_phase, game_id),
        )

    await db.commit()


async def delete_game(game_id):
    """
    Удаляет игру и всех игроков.
    """
    db = await get_db()
    await db.execute("DELETE FROM game_players WHERE game_id = ?", (game_id,))
    await db.execute("DELETE FROM games WHERE id = ?", (game_id,))
    await db.commit()


async def record_player_game_result(user_id, role, won):
    """
    Записывает результат игры для игрока. role — одна из ролей
    router/game_logic.py: "shadow", "detective", "doctor", "civilian".
    """
    db = await get_db()

    await db.execute(
        """
        INSERT INTO player_stats (user_id, games_played, games_won, role_shadow_count, role_hunter_count, role_seer_count, role_healer_count)
        VALUES (?, 0, 0, 0, 0, 0, 0)
        ON CONFLICT(user_id) DO NOTHING
        """,
        (user_id,),
    )
    await db.commit()

    role_column = None
    if role == "shadow":
        role_column = "role_shadow_count"
    elif role == "detective":
        role_column = "role_detective_count"
    elif role == "doctor":
        role_column = "role_doctor_count"
    elif role == "civilian":
        role_column = "role_civilian_count"

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
    Возвращает статистику игрока.
    """
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT games_played, games_won, role_shadow_count, role_detective_count, role_doctor_count, role_civilian_count
        FROM player_stats WHERE user_id = ?
        """,
        (user_id,),
    )
    row = await cursor.fetchone()

    if row:
        return {
            "games_played": row[0],
            "games_won": row[1],
            "role_shadow_count": row[2],
            "role_detective_count": row[3],
            "role_doctor_count": row[4],
            "role_civilian_count": row[5],
        }
    return None


async def reset_votes(game_id):
    """
    Сбрасывает голоса всех игроков.
    """
    db = await get_db()
    await db.execute(
        "UPDATE game_players SET votes_received = 0 WHERE game_id = ?",
        (game_id,),
    )
    await db.commit()


async def add_vote(game_id, user_id):
    """
    Добавляет голос игроку.
    """
    db = await get_db()
    await db.execute(
        "UPDATE game_players SET votes_received = votes_received + 1 WHERE game_id = ? AND user_id = ?",
        (game_id, user_id),
    )
    await db.commit()


async def get_vote_results(game_id):
    """
    Возвращает результаты голосования.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT user_id, username, votes_received FROM game_players WHERE game_id = ? AND is_alive = 1 ORDER BY votes_received DESC",
        (game_id,),
    )
    rows = await cursor.fetchall()

    return [
        {
            "user_id": row[0],
            "username": row[1],
            "votes_received": row[2],
        }
        for row in rows
    ]


async def get_all_active_games():
    """
    Возвращает все активные игры для фазового чекера.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, chat_id, phase, day_number, phase_end_time FROM games"
    )
    rows = await cursor.fetchall()

    return [
        {
            "game_id": row[0],
            "chat_id": row[1],
            "phase": row[2],
            "day_number": row[3],
            "phase_end_time": row[4],
        }
        for row in rows
    ]


# ==================== БАНЫ ====================

async def set_user_banned(telegram_id, banned):
    """
    Банит/разбанивает пользователя по telegram_id.
    """
    db = await get_db()
    await db.execute(
        "UPDATE users SET is_banned = ? WHERE telegram_id = ?",
        (1 if banned else 0, telegram_id),
    )
    await db.commit()


async def is_user_banned(telegram_id):
    """
    Проверяет, забанен ли пользователь.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT is_banned FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = await cursor.fetchone()
    return bool(row and row[0])


async def find_telegram_id_by_username(username):
    """
    Находит telegram_id по @username (регистр не важен).
    """
    db = await get_db()
    clean = username.lstrip("@")
    cursor = await db.execute(
        "SELECT telegram_id FROM users WHERE LOWER(username) = LOWER(?)",
        (clean,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def get_all_telegram_ids():
    """
    Возвращает telegram_id всех известных боту пользователей (для рассылки).
    """
    db = await get_db()
    cursor = await db.execute("SELECT telegram_id FROM users")
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


# ==================== ЛИМИТЫ / СООБЩЕНИЯ ====================

async def get_limit_status(user_id, daily_limit=20):
    """
    Возвращает (использовано_сегодня, осталось_на_сегодня) без изменения счётчика.
    """
    used = await get_usage_count(user_id)
    remaining = max(daily_limit - used, 0)
    return used, remaining


async def get_messages_after(user_id, last_message_id, chat_id=None):
    """
    Возвращает сообщения пользователя с id > last_message_id (или все,
    если last_message_id is None) как список (id, role, content, created_at), по
    возрастанию id. chat_id=None -> личка (только сообщения без chat_id),
    иначе — сообщения конкретного группового чата.
    """
    db = await get_db()
    after_id = last_message_id if last_message_id is not None else 0

    if chat_id is not None:
        cursor = await db.execute(
            """
            SELECT id, role, content, created_at FROM messages
            WHERE chat_id = ? AND id > ?
            ORDER BY id ASC
            """,
            (chat_id, after_id),
        )
    else:
        cursor = await db.execute(
            """
            SELECT id, role, content, created_at FROM messages
            WHERE user_id = ? AND chat_id IS NULL AND id > ?
            ORDER BY id ASC
            """,
            (user_id, after_id),
        )

    return await cursor.fetchall()


# ==================== ПРОВАЙДЕРЫ ====================

async def log_provider_attempt(provider, success, error=None):
    """
    Логирует попытку обращения к AI-провайдеру (для /status и /stats).
    """
    db = await get_db()
    await db.execute(
        "INSERT INTO provider_attempts (provider, success, error) VALUES (?, ?, ?)",
        (provider, 1 if success else 0, error),
    )
    await db.commit()


async def get_last_successful_provider():
    """
    Возвращает (provider, created_at) последнего успешного обращения,
    либо None, если успешных обращений ещё не было.
    """
    db = await get_db()
    cursor = await db.execute(
        """
        SELECT provider, created_at FROM provider_attempts
        WHERE success = 1
        ORDER BY id DESC LIMIT 1
        """
    )
    row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


async def get_provider_stats(hours=24):
    """
    Возвращает статистику по провайдерам за последние `hours` часов:
    список {"provider":, "total":, "failed":}, отсортированный по total.
    """
    db = await get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = await db.execute(
        """
        SELECT provider,
               COUNT(*) AS total,
               SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed
        FROM provider_attempts
        WHERE created_at >= ?
        GROUP BY provider
        ORDER BY total DESC
        """,
        (since,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "provider": row[0],
            "total": row[1],
            "failed": row[2] or 0,
        }
        for row in rows
    ]


# ==================== СТАТИСТИКА БОТА ====================

async def get_total_users_count():
    """
    Возвращает общее число зарегистрированных пользователей.
    """
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) FROM users")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_active_users_count(hours=24):
    """
    Возвращает число уникальных пользователей, написавших сообщение за
    последние `hours` часов.
    """
    db = await get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = await db.execute(
        "SELECT COUNT(DISTINCT user_id) FROM messages WHERE created_at >= ?",
        (since,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_total_messages_count(hours=None):
    """
    Возвращает общее число сообщений, либо число за последние `hours`
    часов, если указано.
    """
    db = await get_db()

    if hours is not None:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await db.execute(
            "SELECT COUNT(*) FROM messages WHERE created_at >= ?",
            (since,),
        )
    else:
        cursor = await db.execute("SELECT COUNT(*) FROM messages")

    row = await cursor.fetchone()
    return row[0] if row else 0


# ==================== ПРОЕКТЫ ====================

async def create_project(user_id, name, description=""):
    """
    Создаёт проект пользователя. Если проект с таким именем уже есть —
    обновляет его описание и возвращает существующий id.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO projects (user_id, name, description) VALUES (?, ?, ?)",
            (user_id, name, description),
        )
        await db.commit()
        return cursor.lastrowid
    except Exception:
        await db.execute(
            "UPDATE projects SET description = ? WHERE user_id = ? AND name = ?",
            (description, user_id, name),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_projects(user_id):
    """
    Возвращает список проектов пользователя.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, name, description, created_at FROM projects WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "created_at": row[3],
        }
        for row in rows
    ]


async def get_project(user_id, name):
    """
    Возвращает один проект пользователя по имени, либо None.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, name, description, created_at FROM projects WHERE user_id = ? AND name = ?",
        (user_id, name),
    )
    row = await cursor.fetchone()
    if row:
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "created_at": row[3],
        }
    return None


async def delete_project(user_id, name):
    """
    Удаляет проект пользователя по имени. Возвращает True, если что-то
    было удалено, иначе False.
    """
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM projects WHERE user_id = ? AND name = ?",
        (user_id, name),
    )
    await db.commit()
    return cursor.rowcount > 0


# ==================== ЧАСОВОЙ ПОЯС ====================

async def get_user_timezone(telegram_id):
    db = await get_db()
    cursor = await db.execute(
        "SELECT timezone FROM users WHERE telegram_id = ?", (telegram_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def set_user_timezone(telegram_id, tz_name):
    db = await get_db()
    await db.execute(
        "UPDATE users SET timezone = ? WHERE telegram_id = ?", (tz_name, telegram_id)
    )
    await db.commit()


# ==================== НАПОМИНАНИЯ ====================

async def add_reminder(user_id, telegram_chat_id, text, due_at_utc):
    """due_at_utc — aware datetime в UTC. Возвращает id напоминания."""
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO reminders (user_id, telegram_chat_id, text, due_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            user_id,
            telegram_chat_id,
            text,
            due_at_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            utc_now_str(),
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def get_due_reminders(limit=50):
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, user_id, telegram_chat_id, text, due_at FROM reminders "
        "WHERE sent = 0 AND due_at <= ? ORDER BY due_at ASC LIMIT ?",
        (utc_now_str(), limit),
    )
    return await cursor.fetchall()


async def mark_reminder_sent(reminder_id):
    db = await get_db()
    await db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
    await db.commit()


async def get_user_reminders(user_id):
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, text, due_at, telegram_chat_id FROM reminders "
        "WHERE user_id = ? AND sent = 0 ORDER BY due_at ASC",
        (user_id,),
    )
    return await cursor.fetchall()


async def count_user_reminders(user_id):
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND sent = 0", (user_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def cancel_reminder(user_id, reminder_id):
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM reminders WHERE id = ? AND user_id = ? AND sent = 0",
        (reminder_id, user_id),
    )
    await db.commit()
    return cursor.rowcount > 0
