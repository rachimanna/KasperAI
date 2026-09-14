"""
Логика мини-игры "Теневой город" (этап 2-3): раздача ролей, ночная фаза,
голосование, "последние слова" погибших, проверка условий победы,
фоновый таймер фаз.

Роли:
- "shadow"    — Тень (ночью выбирает жертву)
- "detective" — Детектив (ночью выбирает, кого проверить)
- "doctor"    — Доктор (ночью выбирает, кого спасти от Тени, можно себя)
- "civilian"  — мирный житель

Фазы игры (поле games.status):
- "lobby"    — сбор игроков (этап 1, уже реализован в handlers.py)
- "night"    — Тень выбирает жертву, Детектив проверяет, Доктор лечит
- "voting"   — открытое голосование в группе за исключение подозреваемого
- "finished" — игра завершена

Таймеры фаз хранятся в БД (games.phase_ends_at, UTC ISO-строка), а не в
asyncio.sleep — это единственный способ пережить пересыпание/передеплой
free-инстанса Render. Фоновая задача phase_checker_loop() раз в несколько
секунд спрашивает у БД, у каких игр истекло время фазы, и продвигает их
дальше. Она безопасна к перезапуску: если бот перезапустится посреди
цикла, при следующем старте она просто продолжит проверять games.

"Последние слова": когда игрок погибает (ночью от Тени или днём по
голосованию), ему в личку даётся немного времени написать прощальное
сообщение, которое потом публикуется в группе вместе с объявлением о
смерти. Захват текста происходит через PENDING_LAST_WORDS — общий
словарь {telegram_id: {"text": None|str}}, в который handlers.py кладёт
текст, если у пользователя в личке ожидается "последнее слово".
"""

import asyncio
import random
from datetime import datetime, timedelta, timezone

from database.db import (
    get_game_by_id,
    get_game_players,
    set_game_phase,
    assign_game_roles,
    set_player_alive,
    save_game_action,
    get_game_action,
    get_actions_by_type,
    get_games_with_expired_phase,
)

NIGHT_DURATION_SECONDS = 60
VOTING_DURATION_SECONDS = 60
PHASE_CHECK_INTERVAL_SECONDS = 7
LAST_WORDS_WINDOW_SECONDS = 20

# action_type в таблице game_actions
ACTION_KILL = "kill"
ACTION_CHECK = "check"
ACTION_HEAL = "heal"
ACTION_VOTE = "vote"

SKIP_TARGET_ID = 0  # условный "пропустить голос"

# {telegram_id: {"text": str|None}} — пока пользователь в этом словаре,
# handlers.py перехватывает его следующее личное текстовое сообщение как
# "последние слова" вместо обычного AI-чата.
PENDING_LAST_WORDS = {}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _future_iso(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _display_name(username, telegram_id):
    return f"@{username}" if username else f"id{telegram_id}"


def _alive_players(players):
    # players: (id, user_id, telegram_id, username, role, is_alive)
    return [p for p in players if p[5] == 1]


def _by_role(players, role):
    return [p for p in _alive_players(players) if p[4] == role]


def capture_last_words(telegram_id, text):
    """
    Вызывается из handlers.py, когда в личку боту приходит текстовое
    сообщение от пользователя, ожидающего отправки последних слов.
    Возвращает True, если сообщение было перехвачено как последние слова
    (тогда handlers.py не должен передавать текст дальше в AI-чат).
    """
    if telegram_id in PENDING_LAST_WORDS:
        PENDING_LAST_WORDS[telegram_id]["text"] = text
        return True
    return False


async def _await_last_words(bot, telegram_id):
    """
    Просит игрока написать последние слова и ждёт LAST_WORDS_WINDOW_SECONDS
    секунд. Возвращает текст, если игрок успел ответить, иначе None.
    """
    PENDING_LAST_WORDS[telegram_id] = {"text": None}
    try:
        await bot.send_message(
            telegram_id,
            "💀 Ты выбыл(а) из игры.\n"
            f"У тебя есть {LAST_WORDS_WINDOW_SECONDS} секунд, чтобы написать "
            "последние слова — просто ответь сюда текстом. Их увидят в группе.",
        )
    except Exception as e:
        print(f"[game] last words prompt ERROR: {e}", flush=True)
        PENDING_LAST_WORDS.pop(telegram_id, None)
        return None

    await asyncio.sleep(LAST_WORDS_WINDOW_SECONDS)
    entry = PENDING_LAST_WORDS.pop(telegram_id, None)
    return entry["text"] if entry else None


async def start_game(bot, game_id, chat_id):
    """
    Раздаёт роли, переводит игру в ночную фазу №1 и рассылает роли в личку.
    Вызывается из handlers.py по кнопке "Начать игру".
    """
    players = await get_game_players(game_id)

    shadow, detective, doctor, civilians = _pick_roles(players)

    role_by_user_id = {
        shadow[1]: "shadow",
        detective[1]: "detective",
        doctor[1]: "doctor",
    }
    for c in civilians:
        role_by_user_id[c[1]] = "civilian"

    await assign_game_roles(game_id, role_by_user_id)

    # Разослать роли в личку. Если кому-то не удалось отправить (заблокировал
    # бота уже после присоединения к лобби) — просто логируем, не роняем игру.
    role_texts = {
        "shadow": (
            "🕶 Ты — <b>Тень</b>.\n"
            "Каждую ночь выбирай, кого устранить. Твоя цель — сравняться "
            "или превзойти числом мирных жителей."
        ),
        "detective": (
            "🔍 Ты — <b>Детектив</b>.\n"
            "Каждую ночь можешь проверить одного игрока и узнать, Тень он "
            "или нет. Помоги мирным вычислить Тень."
        ),
        "doctor": (
            "💊 Ты — <b>Доктор</b>.\n"
            "Каждую ночь можешь спасти одного игрока (в том числе себя) "
            "от Тени. Если угадаешь, кого выберет Тень — жертва выживет."
        ),
        "civilian": (
            "👤 Ты — <b>мирный житель</b>.\n"
            "Днём обсуждай и голосуй за того, кого подозреваешь в роли Тени."
        ),
    }

    for p in players:
        _pid, user_id, telegram_id, username, _role, _alive = p
        role = role_by_user_id.get(user_id, "civilian")
        try:
            await bot.send_message(telegram_id, role_texts[role], parse_mode="HTML")
        except Exception as e:
            print(f"[game] role DM ERROR user_id={user_id}: {e}", flush=True)

    await set_game_phase(
        game_id,
        status="night",
        phase_ends_at=_future_iso(NIGHT_DURATION_SECONDS),
        phase_number=1,
    )

    try:
        await bot.send_message(
            chat_id,
            "🌙 Наступила ночь. Город засыпает...\n"
            "🕶 Тень вышла на охоту...\n"
            "🔍 Детектив пошёл проверять...\n"
            "💊 Доктор готовится спасать...\n"
            f"⏳ Ждём {NIGHT_DURATION_SECONDS} секунд...",
        )
    except Exception as e:
        print(f"[game] night announce ERROR: {e}", flush=True)

    # Отправить кнопки выбора действия Тени, Детективу и Доктору.
    updated_players = await get_game_players(game_id)
    shadow_row = next((p for p in updated_players if p[1] == shadow[1]), None)
    detective_row = next((p for p in updated_players if p[1] == detective[1]), None)
    doctor_row = next((p for p in updated_players if p[1] == doctor[1]), None)

    if shadow_row:
        await _send_night_action_keyboard(bot, game_id, 1, shadow_row, updated_players, ACTION_KILL)
    if detective_row:
        await _send_night_action_keyboard(bot, game_id, 1, detective_row, updated_players, ACTION_CHECK)
    if doctor_row:
        await _send_night_action_keyboard(bot, game_id, 1, doctor_row, updated_players, ACTION_HEAL, allow_self=True)


def _pick_roles(players):
    pool = list(players)
    random.shuffle(pool)
    shadow = pool[0]
    detective = pool[1]
    doctor = pool[2]
    civilians = pool[3:]
    return shadow, detective, doctor, civilians


ACTION_VERBS = {
    ACTION_KILL: "устранить",
    ACTION_CHECK: "проверить",
    ACTION_HEAL: "спасти",
}


async def _send_night_action_keyboard(bot, game_id, phase_number, actor_row, players, action_type, allow_self=False):
    from aiogram import types

    _pid, actor_user_id, actor_telegram_id, _uname, _role, _alive = actor_row

    targets = [
        p for p in _alive_players(players)
        if allow_self or p[1] != actor_user_id
    ]

    if not targets:
        return

    verb = ACTION_VERBS.get(action_type, "выбрать")
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for t in targets:
        _tpid, target_user_id, target_telegram_id, target_username, _trole, _talive = t
        label = _display_name(target_username, target_telegram_id)
        if allow_self and target_user_id == actor_user_id:
            label += " (себя)"
        keyboard.add(
            types.InlineKeyboardButton(
                text=f"☆ {label}",
                callback_data=f"game_night:{game_id}:{phase_number}:{action_type}:{target_user_id}",
            )
        )

    try:
        await bot.send_message(
            actor_telegram_id,
            f"Кого хочешь {verb} этой ночью?",
            reply_markup=keyboard,
        )
    except Exception as e:
        print(f"[game] night keyboard ERROR user_id={actor_user_id}: {e}", flush=True)


async def handle_night_action(game_id, phase_number, action_type, actor_user_id, target_user_id):
    """
    Вызывается из handlers.py при нажатии кнопки ночного действия.
    Просто сохраняет выбор — резолвом ночи занимается resolve_night(),
    которую дёргает фоновый таймер по истечении phase_ends_at.
    """
    await save_game_action(game_id, phase_number, actor_user_id, action_type, target_user_id)


async def _build_voting_keyboard(game_id, players):
    from aiogram import types

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for p in _alive_players(players):
        _pid, user_id, telegram_id, username, _role, _alive = p
        keyboard.add(
            types.InlineKeyboardButton(
                text=f"☆ {_display_name(username, telegram_id)}",
                callback_data=f"game_vote:{game_id}:{user_id}",
            )
        )
    keyboard.add(
        types.InlineKeyboardButton(
            text="☆ 🤷 Пропустить голос",
            callback_data=f"game_vote:{game_id}:{SKIP_TARGET_ID}",
        )
    )
    return keyboard


async def handle_vote_action(game_id, phase_number, actor_user_id, target_user_id):
    await save_game_action(game_id, phase_number, actor_user_id, ACTION_VOTE, target_user_id)


async def _send_voting_message(bot, chat_id, game_id, players, text):
    keyboard = await _build_voting_keyboard(game_id, players)
    try:
        await bot.send_message(
            chat_id,
            f"{text}\n\n🗳 Голосуйте, кого подозреваете в роли Тени "
            f"({VOTING_DURATION_SECONDS} секунд):",
            reply_markup=keyboard,
        )
    except Exception as e:
        print(f"[game] voting announce ERROR: {e}", flush=True)


async def _finalize_night_announcement(bot, chat_id, game_id, victim_row, base_announce, players):
    """
    Ждёт последние слова погибшего (в фоне, не блокируя фоновый таймер
    других игр), затем публикует итог ночи и открывает голосование.
    """
    last_words = await _await_last_words(bot, victim_row[2])
    text = base_announce
    if last_words:
        name = _display_name(victim_row[3], victim_row[2])
        text += f"\n\n💬 Последние слова {name}:\n«{last_words}»"

    await _send_voting_message(bot, chat_id, game_id, players, text)


async def resolve_night(bot, game_id):
    """
    Подводит итоги ночи: убивает жертву Тени (если Доктор её не спас),
    шлёт Детективу результат проверки в личку, объявляет итог в группе
    (без раскрытия ролей, с возможностью последних слов погибшего) и
    запускает голосование.
    """
    game = await get_game_by_id(game_id)
    if not game or game[2] != "night":
        return

    _id, chat_id, _status, _lobby_msg_id, _phase_ends_at, phase_number = game
    players = await get_game_players(game_id)

    kill_actions = await get_actions_by_type(game_id, phase_number, ACTION_KILL)
    check_actions = await get_actions_by_type(game_id, phase_number, ACTION_CHECK)
    heal_actions = await get_actions_by_type(game_id, phase_number, ACTION_HEAL)

    victim_user_id = kill_actions[0][1] if kill_actions else None
    healed_user_id = heal_actions[0][1] if heal_actions else None
    saved_by_doctor = victim_user_id is not None and victim_user_id == healed_user_id

    victim_row = None
    if victim_user_id and not saved_by_doctor:
        victim_row = next((p for p in players if p[1] == victim_user_id), None)
        if victim_row:
            await set_player_alive(game_id, victim_user_id, False)

    if check_actions:
        detective_user_id, checked_user_id = check_actions[0]
        detective_row = next((p for p in players if p[1] == detective_user_id), None)
        checked_row = next((p for p in players if p[1] == checked_user_id), None)
        if detective_row and checked_row:
            is_shadow = checked_row[4] == "shadow"
            verdict = "Тень 🕶" if is_shadow else "не Тень ✅"
            checked_name = _display_name(checked_row[3], checked_row[2])
            try:
                await bot.send_message(
                    detective_row[2],
                    f"🔍 Результат проверки: {checked_name} — {verdict}",
                )
            except Exception as e:
                print(f"[game] detective result ERROR: {e}", flush=True)

    players = await get_game_players(game_id)  # обновить is_alive

    if saved_by_doctor:
        announce = "💊 Этой ночью Доктор спас жертву Тени! Никто не погиб."
    elif victim_row:
        victim_name = _display_name(victim_row[3], victim_row[2])
        announce = f"☠️ Этой ночью погиб(ла) {victim_name}."
    else:
        announce = "🌤 Этой ночью никто не погиб."

    win_message = _check_win_condition(players)
    if win_message:
        await set_game_phase(game_id, status="finished", phase_ends_at=None)
        try:
            await bot.send_message(
                chat_id,
                f"{announce}\n\n🏁 Игра завершена!\n{win_message}"
                f"{_role_reveal_text(players)}",
                parse_mode="HTML",
            )
        except Exception as e:
            print(f"[game] finish announce ERROR: {e}", flush=True)
        return

    # Если есть настоящая жертва — даём ей немного времени на последние
    # слова в фоне, не блокируя проверку остальных игр таймером.
    extra_delay = LAST_WORDS_WINDOW_SECONDS if victim_row else 0
    await set_game_phase(
        game_id,
        status="voting",
        phase_ends_at=_future_iso(extra_delay + VOTING_DURATION_SECONDS),
    )

    if victim_row:
        asyncio.create_task(
            _finalize_night_announcement(bot, chat_id, game_id, victim_row, announce, players)
        )
    else:
        await _send_voting_message(bot, chat_id, game_id, players, announce)


async def _finalize_voting_announcement(bot, chat_id, game_id, excluded_row, base_lines, players, next_phase_number):
    """
    Ждёт последние слова исключённого голосованием игрока, публикует итог
    и открывает следующую ночь.
    """
    last_words = await _await_last_words(bot, excluded_row[2])
    lines = list(base_lines)
    if last_words:
        name = _display_name(excluded_row[3], excluded_row[2])
        lines.append(f"\n💬 Последние слова {name}:\n«{last_words}»")

    await _announce_next_night(bot, chat_id, game_id, players, lines, next_phase_number)


async def _announce_next_night(bot, chat_id, game_id, players, announce_lines, next_phase_number):
    shadow_alive = any(p[4] == "shadow" for p in _alive_players(players))
    detective_alive = any(p[4] == "detective" for p in _alive_players(players))
    doctor_alive = any(p[4] == "doctor" for p in _alive_players(players))

    night_flavor = ["\n🌙 Наступает следующая ночь. Город засыпает..."]
    if shadow_alive:
        night_flavor.append("🕶 Тень вышла на охоту...")
    if detective_alive:
        night_flavor.append("🔍 Детектив пошёл проверять...")
    if doctor_alive:
        night_flavor.append("💊 Доктор готовится спасать...")
    night_flavor.append(f"⏳ Ждём {NIGHT_DURATION_SECONDS} секунд...")

    announce_lines = announce_lines + ["\n".join(night_flavor)]
    try:
        await bot.send_message(chat_id, "\n".join(announce_lines))
    except Exception as e:
        print(f"[game] next night announce ERROR: {e}", flush=True)

    shadow_row = next((p for p in _alive_players(players) if p[4] == "shadow"), None)
    detective_row = next((p for p in _alive_players(players) if p[4] == "detective"), None)
    doctor_row = next((p for p in _alive_players(players) if p[4] == "doctor"), None)

    if shadow_row:
        await _send_night_action_keyboard(bot, game_id, next_phase_number, shadow_row, players, ACTION_KILL)
    if detective_row:
        await _send_night_action_keyboard(bot, game_id, next_phase_number, detective_row, players, ACTION_CHECK)
    if doctor_row:
        await _send_night_action_keyboard(bot, game_id, next_phase_number, doctor_row, players, ACTION_HEAL, allow_self=True)


async def resolve_voting(bot, game_id):
    """
    Подводит итоги голосования: исключает игрока с наибольшим числом
    голосов (при равенстве — никто не исключается), проверяет условия
    победы, либо запускает следующую ночь (с последними словами
    исключённого, если он есть).
    """
    game = await get_game_by_id(game_id)
    if not game or game[2] != "voting":
        return

    _id, chat_id, _status, _lobby_msg_id, _phase_ends_at, phase_number = game
    players = await get_game_players(game_id)

    votes = await get_actions_by_type(game_id, phase_number, ACTION_VOTE)
    tally = {}
    for _voter_user_id, target_user_id in votes:
        if target_user_id == SKIP_TARGET_ID:
            continue
        tally[target_user_id] = tally.get(target_user_id, 0) + 1

    announce_lines = []
    excluded_row = None

    if tally:
        max_votes = max(tally.values())
        top = [uid for uid, cnt in tally.items() if cnt == max_votes]
        if len(top) == 1:
            excluded_user_id = top[0]
            excluded_row = next((p for p in players if p[1] == excluded_user_id), None)
            if excluded_row:
                await set_player_alive(game_id, excluded_user_id, False)
                announce_lines.append(
                    f"🚪 Жители исключили {_display_name(excluded_row[3], excluded_row[2])}."
                )
        else:
            announce_lines.append("⚖️ Голоса разделились поровну — никто не исключён.")
    else:
        announce_lines.append("🤷 Голосов не было — никто не исключён.")

    players = await get_game_players(game_id)  # обновить is_alive

    win_message = _check_win_condition(players)
    if win_message:
        await set_game_phase(game_id, status="finished", phase_ends_at=None)
        try:
            await bot.send_message(
                chat_id,
                "\n".join(announce_lines)
                + f"\n\n🏁 Игра завершена!\n{win_message}"
                + _role_reveal_text(players),
                parse_mode="HTML",
            )
        except Exception as e:
            print(f"[game] finish announce ERROR: {e}", flush=True)
        return

    next_phase_number = phase_number + 1
    extra_delay = LAST_WORDS_WINDOW_SECONDS if excluded_row else 0
    await set_game_phase(
        game_id,
        status="night",
        phase_ends_at=_future_iso(extra_delay + NIGHT_DURATION_SECONDS),
        phase_number=next_phase_number,
    )

    if excluded_row:
        asyncio.create_task(
            _finalize_voting_announcement(bot, chat_id, game_id, excluded_row, announce_lines, players, next_phase_number)
        )
    else:
        await _announce_next_night(bot, chat_id, game_id, players, announce_lines, next_phase_number)


def _role_reveal_text(players):
    """
    Формирует текст с раскрытием ролей всех участников — показывается
    только когда игра уже завершена (после победы одной из сторон).
    """
    role_labels = {
        "shadow": "🕶 Тень",
        "detective": "🔍 Детектив",
        "doctor": "💊 Доктор",
        "civilian": "👤 Мирный житель",
    }
    lines = ["\n📋 <b>Роли игроков:</b>"]
    for p in players:
        _pid, _user_id, telegram_id, username, role, is_alive = p
        name = _display_name(username, telegram_id)
        status = "" if is_alive else " (выбыл)"
        label = role_labels.get(role, "❓ Неизвестно")
        lines.append(f"• {name} — {label}{status}")
    return "\n".join(lines)


def _check_win_condition(players):
    """
    Возвращает текст объявления победителя, если игра окончена, иначе None.
    - Тень мертва -> мирные победили.
    - Тень жива и её число >= числа остальных живых -> Тень победила.
    """
    alive = _alive_players(players)
    shadow_alive = [p for p in alive if p[4] == "shadow"]
    others_alive = [p for p in alive if p[4] != "shadow"]

    if not shadow_alive:
        return "🏆 Тень найдена и устранена — мирные жители победили!"

    if len(shadow_alive) >= len(others_alive):
        return "🏆 Тень поглотила город — Тень победила!"

    return None


def role_reveal_text(players):
    """Публичная обёртка для использования из handlers.py (например /stopgame)."""
    return _role_reveal_text(players)


async def phase_checker_loop(bot):
    """
    Фоновая задача на всё время жизни процесса: раз в
    PHASE_CHECK_INTERVAL_SECONDS секунд проверяет БД на предмет игр с
    истёкшей фазой и продвигает их дальше.

    Специально не использует asyncio.sleep для самих таймеров фаз —
    только для интервала опроса. Вся "память" о том, когда наступает
    следующая фаза, живёт в games.phase_ends_at в БД, поэтому если бот
    перезапустится (Render "заснул" и передеплоился) — при следующем
    старте цикл просто продолжит проверять и вовремя продвинет фазы.
    """
    while True:
        try:
            expired_game_ids = await get_games_with_expired_phase(_now_iso())
            for game_id in expired_game_ids:
                game = await get_game_by_id(game_id)
                if not game:
                    continue
                status = game[2]
                try:
                    if status == "night":
                        await resolve_night(bot, game_id)
                    elif status == "voting":
                        await resolve_voting(bot, game_id)
                except Exception as e:
                    print(f"[game] phase resolve ERROR game_id={game_id}: {e}", flush=True)
        except Exception as e:
            print(f"[game] phase_checker_loop ERROR: {e}", flush=True)

        await asyncio.sleep(PHASE_CHECK_INTERVAL_SECONDS)
