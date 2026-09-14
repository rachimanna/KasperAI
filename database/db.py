import aiosqlite
import os
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "kasper.db")

_db_conn = None


async def get_db():
    """
    Возвращает единственное соединение с БД (singleton).
    """
    global _db_conn
    if _db_conn is None:
        _db_conn = await aiosqlite.connect(DB_PATH)
        _db_conn.row_factory = aiosqlite.Row
    return _db_conn


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
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            request_count INTEGER DEFAULT 0,
            last_date TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

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

    await db.commit()


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
        "INSERT INTO messages (user_id, chat_id, role, content) VALUES (?, ?, ?, ?)",
        (user_id, chat_id, role, content),
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
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
    else:
        # Личка: фильтруем только сообщения без chat_id
        cursor = await db.execute(
            "SELECT role, content FROM messages WHERE user_id = ? AND (chat_id IS NULL) ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )

    rows = await cursor.fetchall()
    rows.reverse()

    # Поддержка старого формата (tuple) и нового (dict)
    result = []
    for row in rows:
        if isinstance(row, dict) or hasattr(row, "keys"):
            result.append({"role": row["role"], "content": row["content"]})
        else:
            result.append({"role": row[0], "content": row[1]})

    return result


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
    """
    Сохраняет или обновляет саммари разговора.
    
    ФИКС #2: для групп теперь проверка по (user_id + chat_id),
    чтобы не перезаписывать саммари других юзеров.
    """
    db = await get_db()

    if chat_id is not None:
        # БЫЛО: WHERE chat_id = ?
        # СТАЛО: WHERE user_id = ? AND chat_id = ?
        cursor = await db.execute(
            "SELECT id FROM conversation_summaries WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
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
            SET summary = ?, last_summarized_message_id = ?
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


async def get_conversation_summary(user_id, chat_id=None):
    """
    Возвращает саммари разговора.
    """
    db = await get_db()

    if chat_id is not None:
        # ФИКС #2: проверка по (user_id + chat_id)
        cursor = await db.execute(
            "SELECT summary, last_summarized_message_id FROM conversation_summaries WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
    else:
        cursor = await db.execute(
            "SELECT summary, last_summarized_message_id FROM conversation_summaries WHERE user_id = ? AND chat_id IS NULL",
            (user_id,),
        )

    row = await cursor.fetchone()
    if row:
        return {"summary": row[0], "last_summarized_message_id": row[1]}
    return None


async def check_and_increment_limit(user_id, daily_limit=20, telegram_id=None):
    """
    Проверяет лимит запросов пользователя.
    Возвращает (можно_ли_запросить, оставшиеся_запросы).
    
    ПРИМЕЧАНИЕ: бот бесплатный, лимиты не критичны,
    но race condition всё равно присутствует (если 2 запроса одновременно).
    """
    db = await get_db()
    today = datetime.now().strftime("%Y-%m-%d")

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

    count = row[0]
    last_date = row[1]

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


async def get_usage_count(user_id):
    """
    Возвращает количество запросов за сегодня.
    """
    db = await get_db()
    today = datetime.now().strftime("%Y-%m-%d")

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


# ==================== ИГРА ====================

async def create_game(chat_id):
    """
    Создаёт новую игру в чате.
    """
    db = await get_db()
    await db.execute(
        "INSERT INTO games (chat_id, phase, day_number, phase_end_time) VALUES (?, 'night', 1, ?)",
        (chat_id, (datetime.now() + timedelta(seconds=180)).timestamp()),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT id FROM games WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def get_active_game(chat_id):
    """
    Возвращает активную игру в чате.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, phase, day_number, phase_end_time FROM games WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
        (chat_id,),
    )
    row = await cursor.fetchone()

    if row:
        return {
            "game_id": row[0],
            "phase": row[1],
            "day_number": row[2],
            "phase_end_time": row[3],
        }
    return None


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


async def record_player_game_result(user_id, won, role):
    """
    Записывает результат игры для игрока.
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
    if role == "Тень":
        role_column = "role_shadow_count"
    elif role == "Охотник":
        role_column = "role_hunter_count"
    elif role == "Провидец":
        role_column = "role_seer_count"
    elif role == "Целитель":
        role_column = "role_healer_count"

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
        SELECT games_played, games_won, role_shadow_count, role_hunter_count, role_seer_count, role_healer_count
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
            "role_hunter_count": row[3],
            "role_seer_count": row[4],
            "role_healer_count": row[5],
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
