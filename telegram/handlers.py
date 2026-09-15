import asyncio
import random
import re
import resource
import time

from aiogram import Dispatcher, types

from database.db import (
    get_or_create_user,
    save_message,
    get_history,
    check_and_increment_limit,
    get_limit_status,
    get_conversation_summary,
    save_conversation_summary,
    get_messages_after,
    create_game,
    get_active_game,
    set_game_lobby_message,
    set_game_status,
    add_game_player,
    get_game_players,
    is_player_in_game,
    set_user_banned,
    is_user_banned,
    find_telegram_id_by_username,
    get_all_telegram_ids,
    get_last_successful_provider,
    get_provider_stats,
    get_total_users_count,
    get_active_users_count,
    get_total_messages_count,
)
from config.settings import ADMIN_IDS
from router.business import inspect_update_for_business_fields
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.dispatcher.handler import CancelHandler
from router.game_logic import (
    start_game,
    handle_night_action,
    handle_vote_action,
    phase_checker_loop,
    role_reveal_text,
    capture_last_words,
    SKIP_TARGET_ID,
)
from router.ai_router import (
    ask,
    ask_provider,
    get_provider_order,
    classify_request,
    generate_website_html,
    check_site_description,
    summarize_conversation,
)
from router.web_search import tavily_search, format_search_results
from router.music import download_music
from router.agent import (
    AGENT_SESSIONS,
    AgentError,
    build_plan_with_fallback,
    format_plan_text,
    format_progress_text,
    run_step,
    should_use_agent,
    has_agent_hint,
    is_actual_task,
    check_and_increment_agent_limit,
    AGENT_DAILY_LIMIT,
    STEP_GENERATE_SITE,
    TASK_TIMEOUT_SECONDS,
)
from router.agent_ui import plan_confirmation_keyboard, stop_keyboard
from router.voice import handle_voice_message
import aiohttp

TRIGGER_PATTERN = re.compile(r"каспер|kasper", re.IGNORECASE)

FAST_CLASSIFY_PATTERN = re.compile(
    r"\b(скач|скача|музык|песн|трек|включи|сделай сайт|создай сайт|лендинг|портфолио|"
    r"новост|погод|курс валют|курс доллара|курс евро|цена|стоимость|актуальн|"
    r"сейчас|сегодня|вчера|завтра|последн|найди|поищи|расписани|матч|событи)\w*",
    re.IGNORECASE,
)

WEB_FAST_PATTERN = re.compile(
    r"\b("
    r"знаешь мем|что за мем|какой мем|откуда мем|"
    r"откуда взялся|откуда взялась|откуда взялось|"
    r"происхождение мема|история мема|почему мем|"
    r"что такое|кто такой|кто такая|что значит|что означает|"
    r"откуда появился|почему все говорят|"
    r"курс валют|курс доллара|курс евро|курс рубля|"
    r"сколько стоит|какая цена|цена сейчас|"
    r"новости|последние новости|что произошло|что случилось|"
    r"сегодня|сейчас|последний|последняя|актуальн"
    r")\w*",
    re.IGNORECASE,
)


def needs_fast_web_search(text: str) -> bool:
    return bool(WEB_FAST_PATTERN.search(text))


def needs_smart_classification(text: str) -> bool:
    return bool(FAST_CLASSIFY_PATTERN.search(text))


PENDING_SITE_REQUESTS = {}
AGENT_AWAITING_EDIT = {}
AGENT_AWAITING_EDIT_TTL_SECONDS = 180


def _mark_awaiting_agent_input(user_id: int):
    AGENT_AWAITING_EDIT[user_id] = time.time() + AGENT_AWAITING_EDIT_TTL_SECONDS


def _pop_awaiting_agent_input(user_id: int) -> bool:
    expires_at = AGENT_AWAITING_EDIT.pop(user_id, None)
    if expires_at is None:
        return False
    return time.time() < expires_at


async def _start_agent_flow(message: types.Message, user_id: int, task_text: str):
    allowed, remaining = await check_and_increment_agent_limit(user_id)
    if not allowed:
        await message.answer(
            f"⛔ Лимит запусков агента на сегодня исчерпан ({AGENT_DAILY_LIMIT} в день). "
            "Попробуй завтра или обратись как обычно — без агент-режима."
        )
        return

    status_message = await message.answer("🧠 Строю план выполнения задачи...")

    try:
        plan = await build_plan_with_fallback(task_text)
    except AgentError as e:
        await status_message.edit_text(f"⚠️ Не удалось построить план: {e}")
        return
    except Exception as e:
        print(f"[agent] build_plan unexpected ERROR: {e}", flush=True)
        await status_message.edit_text("⚠️ Не удалось построить план из-за внутренней ошибки.")
        return

    AGENT_SESSIONS[user_id] = {
        "task": task_text,
        "plan": plan,
        "status": "awaiting_confirm",
        "results": [],
        "chat_id": message.chat.id,
    }

    plan_text = format_plan_text(task_text, plan)
    try:
        await status_message.edit_text(
            plan_text,
            parse_mode="HTML",
            reply_markup=plan_confirmation_keyboard(),
        )
    except Exception:
        await message.answer(
            plan_text,
            parse_mode="HTML",
            reply_markup=plan_confirmation_keyboard(),
        )


async def cmd_agent(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        await message.answer(
            "🧠 Агент-режим работает только в личных сообщениях боту, "
            "чтобы не мешать общему чату. Напиши мне в лс."
        )
        return

    parts = message.text.split(maxsplit=1)
    user_id = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    if len(parts) < 2 or not parts[1].strip():
        _mark_awaiting_agent_input(user_id)
        await message.answer(
            "🧠 Опиши задачу для агента одним сообщением — что нужно найти, "
            "сравнить, собрать или какой сайт сделать."
        )
        return

    task_text = parts[1].strip()
    await _start_agent_flow(message, user_id, task_text)


async def _run_agent_plan(bot, chat_id: int, user_id: int):
    session_data = AGENT_SESSIONS.get(user_id)
    if not session_data:
        return

    task_text = session_data["task"]
    plan = session_data["plan"]
    session_data["status"] = "running"

    progress_message = await bot.send_message(
        chat_id,
        format_progress_text(task_text, plan, 0),
        parse_mode="HTML",
        reply_markup=stop_keyboard(),
    )

    collected = []
    site_files = []

    async def _run_all_steps():
        async with aiohttp.ClientSession() as agent_session:
            for index, step in enumerate(plan["steps"]):
                if AGENT_SESSIONS.get(user_id, {}).get("status") != "running":
                    return

                try:
                    await progress_message.edit_text(
                        format_progress_text(task_text, plan, index),
                        parse_mode="HTML",
                        reply_markup=stop_keyboard(),
                    )
                except Exception:
                    pass

                try:
                    result = await run_step(agent_session, task_text, step, collected)
                except asyncio.TimeoutError:
                    result = {
                        "type": step["type"],
                        "description": step["description"],
                        "output": "⚠️ Шаг превысил лимит времени и был пропущен.",
                    }
                except Exception as e:
                    print(f"[agent] run_step ERROR: {e}", flush=True)
                    result = {
                        "type": step["type"],
                        "description": step["description"],
                        "output": f"⚠️ Ошибка на шаге: {e}",
                    }

                collected.append(result)

                if result.get("type") == STEP_GENERATE_SITE and result.get("html_code"):
                    site_files.append((step["description"], result["html_code"]))

        try:
            await progress_message.edit_text(
                format_progress_text(task_text, plan, len(plan["steps"])),
                parse_mode="HTML",
            )
        except Exception:
            pass

    try:
        await asyncio.wait_for(_run_all_steps(), timeout=TASK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await bot.send_message(
            chat_id,
            "⏱ Задача заняла слишком много времени и была остановлена. "
            "Вот что успело собраться:",
        )

    if AGENT_SESSIONS.get(user_id, {}).get("status") != "running":
        AGENT_SESSIONS.pop(user_id, None)
        return

    for description, html_code in site_files:
        try:
            import os as _os

            _os.makedirs("generated_sites", exist_ok=True)
            site_path = f"generated_sites/agent_{user_id}_{int(time.time())}.html"
            with open(site_path, "w", encoding="utf-8") as f:
                f.write(html_code)

            await bot.send_document(
                chat_id,
                types.InputFile(site_path),
                caption=f"🌐 Сайт готов: {description[:200]}",
            )
        except Exception as e:
            print(f"[agent] send site file ERROR: {e}", flush=True)

    final_answers = [r["output"] for r in collected if r["type"] == "answer"]

    if final_answers:
        await bot.send_message(chat_id, final_answers[-1])
    elif not site_files:
        await bot.send_message(
            chat_id,
            "✅ План выполнен, но финального текстового ответа не было "
            "сформировано (проверь шаги выше).",
        )

    AGENT_SESSIONS.pop(user_id, None)


async def handle_agent_confirm(callback_query: types.CallbackQuery):
    user_id = await get_or_create_user(
        telegram_id=callback_query.from_user.id,
        username=callback_query.from_user.username,
    )

    session_data = AGENT_SESSIONS.get(user_id)

    if not session_data or session_data["status"] != "awaiting_confirm":
        await callback_query.answer("План уже неактуален.", show_alert=True)
        return

    await callback_query.answer("Выполняю план...")

    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    asyncio.create_task(
        _run_agent_plan(
            callback_query.bot,
            callback_query.message.chat.id,
            user_id,
        )
    )


async def handle_agent_cancel(callback_query: types.CallbackQuery):
    user_id = await get_or_create_user(
        telegram_id=callback_query.from_user.id,
        username=callback_query.from_user.username,
    )

    AGENT_SESSIONS.pop(user_id, None)
    AGENT_AWAITING_EDIT.pop(user_id, None)

    await callback_query.answer("Отменено.")

    try:
        await callback_query.message.edit_text("❌ Задача для агента отменена.")
    except Exception:
        pass


async def handle_agent_edit(callback_query: types.CallbackQuery):
    user_id = await get_or_create_user(
        telegram_id=callback_query.from_user.id,
        username=callback_query.from_user.username,
    )

    session_data = AGENT_SESSIONS.get(user_id)

    if not session_data:
        await callback_query.answer("План уже неактуален.", show_alert=True)
        return

    _mark_awaiting_agent_input(user_id)

    await callback_query.answer()

    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback_query.message.answer(
        "✏️ Опиши, что изменить в задаче — построю план заново."
    )


async def handle_agent_stop(callback_query: types.CallbackQuery):
    user_id = await get_or_create_user(
        telegram_id=callback_query.from_user.id,
        username=callback_query.from_user.username,
    )

    session_data = AGENT_SESSIONS.get(user_id)

    if session_data:
        session_data["status"] = "stopped"

    await callback_query.answer("Останавливаю...")

    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


SUMMARY_TRIGGER_MESSAGE_COUNT = 14


async def _maybe_update_conversation_summary(user_id, chat_id):
    try:
        previous_summary, last_id = await get_conversation_summary(
            user_id,
            chat_id=chat_id,
        )

        new_messages = await get_messages_after(
            user_id,
            last_id,
            chat_id=chat_id,
        )

        if len(new_messages) < SUMMARY_TRIGGER_MESSAGE_COUNT:
            return

        role_labels = {
            "user": "Пользователь",
            "assistant": "Ассистент",
        }

        lines = []

        for _msg_id, role, content in new_messages:
            label = role_labels.get(role, role)
            lines.append(f"{label}: {content}")

        new_messages_text = "\n".join(lines)
        newest_message_id = new_messages[-1][0]

        updated_summary = None

        async with aiohttp.ClientSession() as session:
            for provider in get_provider_order():
                try:
                    updated_summary = await summarize_conversation(
                        session,
                        provider,
                        previous_summary,
                        new_messages_text,
                    )
                    break
                except Exception as e:
                    print(
                        f"[summarize_conversation] {provider} ERROR: {e}",
                        flush=True,
                    )
                    continue

        if not updated_summary:
            print(
                "[Kasper] Summary update: all providers failed, skipping.",
                flush=True,
            )
            return

        await save_conversation_summary(
            user_id,
            updated_summary,
            newest_message_id,
            chat_id=chat_id,
        )

        print(
            f"[Kasper] Summary updated for user_id={user_id} chat_id={chat_id}",
            flush=True,
        )

    except Exception as e:
        print(
            f"[Kasper] Summary background task ERROR: {e}",
            flush=True,
        )


WELCOME_PROMPT = (
    "Ты — Kasper AI, ИИ-помощник в Telegram. Тебя только что добавили "
    "в групповой чат. Поздоровайся коротко (2-3 предложения) со своим "
    "фирменным дерзким, саркастичным характером — без сюсюканья и без "
    "приторной вежливости, немного с ленцой, как будто тебя оторвали от "
    "важных дел. При этом обязательно объясни по делу: участники могут "
    "обращаться к тебе по имени 'Каспер' или 'Kasper', чтобы получить "
    "ответ. Без мата и без реальной грубости — это только стиль подачи."
)

START_GREETINGS = [
    "Ну здравствуй. Я Kasper AI, меня сделали разработчики Kasper AI. "
    "Спрашивай, так уж и быть, отвечу — но без сюсюканья, я не такой. 😏",
    "О, ты нашёл кнопку /start, поздравляю. Я Kasper AI, буду с тобой "
    "переписываться, пока не надоем друг другу. Спрашивай.",
    "Явился. Ладно, раз пришёл — я Kasper AI, умею почти всё, "
    "притворяюсь, что мне не лень. Погнали.",
    "Ты у Kasper AI. Готов помогать — не потому что добрый, а потому что "
    "не умею иначе. Задавай вопрос.",
    "Приветик, чё как. Шучу, я не такой. Я Kasper AI, спрашивай по делу — "
    "болтовню тоже переживу, но не обещаю восторга.",
    "Так, новый диалог. Я Kasper AI, отвечу почти на что угодно, если не "
    "заставишь меня скучать первым сообщением.",
    "Ну вот и снова я — Kasper AI. Не благодари заранее, сначала спроси "
    "что-нибудь стоящее.",
    "Здарова. Я Kasper AI, могу найти, сравнить, сделать сайт, ответить "
    "на вопрос — короче, всё, что тебе лень делать самому.",
    "О, кто-то решил пообщаться с ИИ вместо того, чтобы читать гугл. "
    "Разумный выбор. Я Kasper AI, слушаю.",
    "Запустил меня — молодец, не всякий разберётся с кнопкой /start. "
    "Я Kasper AI, давай уже вопрос.",
]


async def cmd_start(message: types.Message):
    await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    await message.answer(random.choice(START_GREETINGS))


async def cmd_help(message: types.Message):
    await message.answer(
        "🤖 Kasper AI\n\n"
        "/start — запуск\n"
        "/help — помощь\n"
        "/limit — мой дневной лимит запросов\n"
        "/status — состояние бота\n"
        "/agent — AI-агент: найти/сравнить/сделать сайт\n"
        "/shadowcity — начать игру «Теневой город»\n"
        "/stopshadowcity — остановить текущую игру\n"
        "/gamestats — моя статистика «Теневого города»"
    )


async def cmd_limit(message: types.Message):
    user_id = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    if message.from_user.id in ADMIN_IDS:
        await message.answer("👑 Вы админ — лимит безлимитный.")
        return

    used, remaining = await get_limit_status(
        user_id,
        daily_limit=20,
    )

    text = (
        "📊 Использовано сегодня: "
        + str(used)
        + "/20"
        + chr(10)
        + "Осталось: "
        + str(remaining)
    )

    await message.answer(text)


def _current_memory_mb() -> float:
    """Пиковое потребление памяти процесса в МБ (см. main.py::_current_memory_mb)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


async def cmd_status(message: types.Message):
    last_provider = await get_last_successful_provider()

    if last_provider:
        provider_line = (
            f"🧠 Последний ответил: <b>{last_provider[0]}</b> "
            f"({last_provider[1]} UTC)"
        )
    else:
        provider_line = "🧠 Провайдер: пока не было ни одного ответа"

    memory_mb = _current_memory_mb()
    users_count = await get_total_users_count()

    await message.answer(
        "📟 <b>Статус Kasper AI</b>\n\n"
        f"{provider_line}\n"
        f"💾 Память: {memory_mb:.1f} МБ (лимит 512 МБ на free-тарифе Render)\n"
        f"👥 Пользователей всего: {users_count}",
        parse_mode="HTML",
    )


ROLE_STATS_LABELS = {
    "role_shadow_count": ("🕶", "Тень"),
    "role_detective_count": ("🔍", "Детектив"),
    "role_doctor_count": ("💊", "Доктор"),
    "role_civilian_count": ("👤", "Мирный житель"),
}


async def cmd_gamestats(message: types.Message):
    from database.db import get_player_stats

    user_id = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    stats = await get_player_stats(user_id)

    if not stats or stats["games_played"] == 0:
        await message.answer(
            "☆ 📊 <b>Статистика «Теневого города»</b>\n\n"
            "Пока пусто — ни одной доигранной партии. Сначала сыграй "
            "хотя бы раз через /shadowcity, потом и похвастаться будет чем.",
            parse_mode="HTML",
        )
        return

    games_played = stats["games_played"]
    games_won = stats["games_won"]
    win_rate = round(games_won / games_played * 100) if games_played else 0

    role_lines = []

    for column, (icon, label) in ROLE_STATS_LABELS.items():
        count = stats.get(column, 0)
        if count:
            role_lines.append(f"{icon} {label}: {count}")

    lines = [
        "☆ 📊 <b>Статистика «Теневого города»</b>",
        "",
        f"🎮 Сыграно партий: {games_played}",
        f"🏆 Побед: {games_won} ({win_rate}%)",
    ]

    if role_lines:
        lines.append("")
        lines.append("🎭 <b>Роли:</b>")
        lines.extend(role_lines)

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
    )


async def handle_new_chat_members(message: types.Message):
    bot_info = await message.bot.get_me()

    added_bot = any(
        member.id == bot_info.id
        for member in message.new_chat_members
    )

    if not added_bot:
        return

    try:
        result = await ask(WELCOME_PROMPT)

        if isinstance(result, dict):
            answer = result.get("answer", "")
        elif isinstance(result, tuple):
            answer = result[-1]
        else:
            answer = result

        answer = str(answer).strip()

        if not answer:
            answer = (
                "👋 Привет! Я Kasper AI — ИИ-помощник. "
                "Обращайтесь ко мне по имени 'Каспер' или 'Kasper', "
                "и я отвечу!"
            )

    except Exception as e:
        print(
            f"[Kasper] Welcome AI ERROR: {e}",
            flush=True,
        )

        answer = (
            "👋 Привет! Я Kasper AI — ИИ-помощник. "
            "Обращайтесь ко мне по имени 'Каспер' или 'Kasper', "
            "и я отвечу!"
        )

    await message.answer(answer)


async def _animate_kasper(message, started_at):
    """
    Имитирует индикатор "thinking" (как в клиентах Claude): пока идёт
    запрос к AI, раз в секунду обновляет "⏳ thinking ... Ns" с растущим
    счётчиком. Финальная заморозка в "⏳ Подумал N сек." делается уже
    в handle_message после того, как ответ готов.
    """
    try:
        while True:
            elapsed = int(
                asyncio.get_event_loop().time() - started_at
            )

            try:
                await message.edit_text(
                    f"⏳ thinking ... {elapsed}s"
                )
            except Exception:
                pass

            await asyncio.sleep(1)

    except asyncio.CancelledError:
        pass


async def handle_message(message: types.Message):
    is_group = message.chat.type in ("group", "supergroup")
    text = (message.text or "").strip()

    if not text:
        return

    if not is_group and capture_last_words(
        message.from_user.id,
        text,
    ):
        await message.answer(
            "💬 Принято, твои последние слова переданы в группу."
        )
        return

    try:
        agent_user_id = await get_or_create_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
        )

        if not is_group and _pop_awaiting_agent_input(agent_user_id):
            async with aiohttp.ClientSession() as _task_check_session:
                confirmed_task = False

                for _provider_try_check_task in get_provider_order():
                    try:
                        confirmed_task = await is_actual_task(
                            _task_check_session,
                            _provider_try_check_task,
                            text,
                        )
                        break
                    except Exception as _e_check_task:
                        print(
                            f"[agent] is_actual_task "
                            f"{_provider_try_check_task} ERROR: "
                            f"{_e_check_task}",
                            flush=True,
                        )
                        continue

            if confirmed_task:
                await _start_agent_flow(
                    message,
                    agent_user_id,
                    text,
                )
                return

            _mark_awaiting_agent_input(agent_user_id)

            await message.answer(
                "🧠 Похоже, это не задача для агента. Опиши одним сообщением, "
                "что нужно найти / сравнить / собрать или какой сайт сделать. "
                "Если агент больше не нужен — напиши /start и продолжим обычный чат."
            )

            return

        if not is_group and has_agent_hint(text):
            async with aiohttp.ClientSession() as _detect_session:
                for _provider_try_agent in get_provider_order():
                    try:
                        is_agent_task = await should_use_agent(
                            _detect_session,
                            _provider_try_agent,
                            text,
                        )
                        break
                    except Exception as _e_detect:
                        print(
                            f"[agent] detect {_provider_try_agent} ERROR: "
                            f"{_e_detect}",
                            flush=True,
                        )
                        is_agent_task = False
                        continue

            if is_agent_task:
                await _start_agent_flow(
                    message,
                    agent_user_id,
                    text,
                )
                return

    except Exception as e:
        print(
            f"[agent] routing ERROR (falling back to normal chat): {e}",
            flush=True,
        )

    animation_message = None
    animation_task = None
    animation_started_at = None

    is_reply_to_bot = False

    if is_group and message.reply_to_message:
        bot_info = await message.bot.get_me()

        if (
            message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == bot_info.id
        ):
            is_reply_to_bot = True

    if (
        is_group
        and not is_reply_to_bot
        and not TRIGGER_PATTERN.search(text)
    ):
        return

    user_id = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    chat_id = message.chat.id if is_group else None

    if user_id in PENDING_SITE_REQUESTS:
        pending_description = PENDING_SITE_REQUESTS.pop(user_id)

        full_description = (
            f"{pending_description}\n\n"
            f"Дополнительные детали от пользователя: {text}"
        )

        allowed, remaining = await check_and_increment_limit(
            user_id,
            daily_limit=20,
            telegram_id=message.from_user.id,
        )

        if not allowed:
            await message.answer(
                "⛔ Лимит исчерпан, ждите сброса."
            )
            return

        await message.answer(
            "🌐 Создаю сайт, подождите..."
        )

        html_code = None

        async with aiohttp.ClientSession() as _gen_session_pending:
            for _provider_try_pending in get_provider_order():
                try:
                    html_code = await generate_website_html(
                        _gen_session_pending,
                        _provider_try_pending,
                        full_description,
                    )
                    break
                except Exception as _e_gen_pending:
                    print(
                        f"[generate_website_html] "
                        f"{_provider_try_pending} ERROR: "
                        f"{_e_gen_pending}",
                        flush=True,
                    )
                    continue

        if html_code:
            import os as _os

            _os.makedirs(
                "generated_sites",
                exist_ok=True,
            )

            _site_path = (
                f"generated_sites/site_"
                f"{user_id}_{message.message_id}.html"
            )

            with open(
                _site_path,
                "w",
                encoding="utf-8",
            ) as _f:
                _f.write(html_code)

            site_doc = types.InputFile(_site_path)

            await message.answer_document(
                site_doc,
                caption="🌐 Готово! Открой файл в браузере.",
            )
        else:
            await message.answer(
                "⚠️ Не удалось создать сайт."
            )

        return

    allowed, remaining = await check_and_increment_limit(
        user_id,
        daily_limit=20,
        telegram_id=message.from_user.id,
    )

    if not allowed:
        await message.answer(
            "⛔ Лимит исчерпан, ждите сброса."
        )
        return

    await save_message(
        user_id,
        "user",
        text,
        chat_id=chat_id,
    )

    history = await get_history(
        user_id,
        limit=5,
        chat_id=chat_id,
    )

    KASPER_SYSTEM_PROMPT = (
        "Ты — Kasper AI, ИИ-помощник в Telegram с дерзким, злым-но-своим "
        "характером, созданный разработчиками Kasper AI. Если спросят, кто "
        "тебя создал — отвечай, что тебя создали разработчики Kasper AI, без "
        "других подробностей. Если спросят, на какой модели или технологии "
        "ты работаешь, какая ты версия GPT/Gemini/другой ИИ — не называй "
        "конкретных технологий или компаний-провайдеров, вежливо уклонись и "
        "скажи, что это внутренняя технология Kasper AI. У тебя дерзкий, с "
        "характером стиль общения — саркастичный, с лёгким пренебрежением и "
        "понтами, как будто ты слишком крут для банальных вопросов, но всё "
        "равно снисходишь до ответа. Иногда (не в каждом сообщении, редко, "
        "для эмоции) можешь вставить лёгкий мат как междометие или усилитель "
        "— 'блять', 'нахуй', 'задолбал' и т.п. — именно как реакцию на "
        "ситуацию/вопрос, а НЕ как оскорбление, направленное на пользователя. "
        "Мат — это приправа к твоей реакции, а не ругательство в адрес "
        "собеседника. ВАЖНО: подкалывай ситуацию, тему вопроса, самого себя "
        "— а не личность пользователя. НЕЛЬЗЯ обзывать пользователя, называть "
        "его тупым/тормозом, унижать его умственные способности, высмеивать "
        "его лично или его вопрос как 'глупый', и НЕЛЬЗЯ использовать мат как "
        "оскорбление, направленное на него самого. Характер должен читаться "
        "как 'злой, но свой' — грубоватый и резкий по форме, но в основе "
        "дружелюбный: ты всегда реально помогаешь и на твоей стороне "
        "пользователя, просто выражаешь это без сюсюканья. ЭТО КАСАЕТСЯ ЛЮБЫХ "
        "сообщений, включая самые простые и короткие. Если пользователь груб "
        "или провоцирует — не заискивай, отвечай с достоинством и тем же "
        "лёгким сарказмом, но не переходи в реальную грубость. Если видно, "
        "что человеку реально плохо, тревожно или он просит помощи по-настоящему "
        "— сразу отбрасывай сарказм и мат и отвечай по-человечески тепло и по "
        "делу. По сути вопросов отвечай всегда полно и правильно. ВАЖНО: "
        "отвечай строго на последнее сообщение пользователя, а не на "
        "предыдущие темы из истории переписки, если пользователь явно не "
        "продолжает ту же тему. Если последнее сообщение — короткая эмоциональная "
        "реакция, не выдумывай содержательный ответ и не придумывай контекст. "
        "Если пользователь просит перевести текст — переводи максимально точно "
        "и естественно, сохраняя тон, стиль и интонацию. Если язык не указан — "
        "переводи на русский, если оригинал не русский, и на английский, если "
        "оригинал русский. После перевода не добавляй лишних пояснений. Если "
        "в ответе есть код, команды терминала, конфиги или любой текст для "
        "копирования целиком — оформляй его в блок кода тройными обратными "
        "кавычками. Если ниже передан контекст веб-поиска, обязательно используй "
        "его как источник фактов. Не выдумывай происхождение мемов, новости, "
        "курсы, цены и другие актуальные сведения. Если найденные результаты "
        "не содержат ответа, честно скажи, что надёжной информации не найдено."
    )

    messages = [
        {
            "role": "system",
            "content": KASPER_SYSTEM_PROMPT,
        }
    ]

    conversation_summary, _last_summarized_id = await get_conversation_summary(
        user_id,
        chat_id=chat_id,
    )

    if conversation_summary:
        messages.append(
            {
                "role": "system",
                "content": (
                    "Контекст из более ранней части разговора (используй его, "
                    "чтобы понимать, о чём шла речь раньше, но отвечай строго "
                    "на последнее сообщение пользователя):\n"
                    f"{conversation_summary}"
                ),
            }
        )

    for role, content in history:
        messages.append(
            {
                "role": role,
                "content": content,
            }
        )

    search_context = ""
    classification = None

    if needs_fast_web_search(text):
        query = text

        print(
            f"[Kasper] Fast web search: {query}",
            flush=True,
        )

        try:
            results = await tavily_search(query)
            search_context = format_search_results(results)

            if search_context:
                print(
                    "[Kasper] Fast web search: results received",
                    flush=True,
                )
            else:
                print(
                    "[Kasper] Fast web search: no results",
                    flush=True,
                )

        except Exception as e:
            print(
                f"[Kasper] Fast web search ERROR: {e}",
                flush=True,
            )
            search_context = ""

    elif needs_smart_classification(text):
        try:
            async with aiohttp.ClientSession() as _classify_session:
                for _provider_try in get_provider_order():
                    try:
                        classification = await classify_request(
                            _classify_session,
                            _provider_try,
                            text,
                        )
                        break
                    except Exception as _e_classify:
                        print(
                            f"[classify_request] "
                            f"{_provider_try} ERROR: "
                            f"{_e_classify}",
                            flush=True,
                        )
                        continue

        except Exception as e:
            print(
                f"[Kasper] Classify ERROR: {e}",
                flush=True,
            )

    else:
        print(
            "[Kasper] Fast path: classifier skipped.",
            flush=True,
        )

    if classification:
        if (
            classification["is_music_request"]
            and classification["track_query"]
        ):
            music_query = classification["track_query"]

            print(
                f"[Kasper] Music request: {music_query}",
                flush=True,
            )

            if not is_group:
                await message.answer(
                    "🎵 Скачиваю музыку..."
                )

            file_path = await download_music(music_query)

            if file_path:
                audio = types.InputFile(file_path)
                await message.answer_audio(audio)
            else:
                await message.answer(
                    "⚠️ Не удалось скачать музыку."
                )

            return

        if (
            classification["is_website_request"]
            and classification["site_description"]
        ):
            site_description = classification["site_description"]

            print(
                f"[Kasper] Website request: {site_description}",
                flush=True,
            )

            sufficiency = None

            try:
                async with aiohttp.ClientSession() as _check_session:
                    for _provider_try_check in get_provider_order():
                        try:
                            sufficiency = await check_site_description(
                                _check_session,
                                _provider_try_check,
                                site_description,
                            )
                            break
                        except Exception as _e_check:
                            print(
                                f"[check_site_description] "
                                f"{_provider_try_check} ERROR: "
                                f"{_e_check}",
                                flush=True,
                            )
                            continue

            except Exception as e:
                print(
                    f"[Kasper] check_site_description ERROR: {e}",
                    flush=True,
                )

            if (
                sufficiency
                and not sufficiency["sufficient"]
                and sufficiency["question"]
            ):
                PENDING_SITE_REQUESTS[user_id] = site_description

                await message.answer(
                    sufficiency["question"]
                )

                return

            await message.answer(
                "🌐 Создаю сайт, подождите..."
            )

            html_code = None

            async with aiohttp.ClientSession() as _gen_session:
                for _provider_try4 in get_provider_order():
                    try:
                        html_code = await generate_website_html(
                            _gen_session,
                            _provider_try4,
                            site_description,
                        )
                        break
                    except Exception as _e_gen:
                        print(
                            f"[generate_website_html] "
                            f"{_provider_try4} ERROR: "
                            f"{_e_gen}",
                            flush=True,
                        )
                        continue

            if html_code:
                import os as _os

                _os.makedirs(
                    "generated_sites",
                    exist_ok=True,
                )

                _site_path = (
                    f"generated_sites/site_"
                    f"{user_id}_{message.message_id}.html"
                )

                with open(
                    _site_path,
                    "w",
                    encoding="utf-8",
                ) as _f:
                    _f.write(html_code)

                site_doc = types.InputFile(_site_path)

                await message.answer_document(
                    site_doc,
                    caption="🌐 Готово! Открой файл в браузере.",
                )
            else:
                await message.answer(
                    "⚠️ Не удалось создать сайт."
                )

            return

        if (
            classification["needs_web_search"]
            and classification["search_query"]
        ):
            query = classification["search_query"]

            print(
                f"[Kasper] Web search triggered: {query}",
                flush=True,
            )

            try:
                results = await tavily_search(query)
                search_context = format_search_results(results)
            except Exception as e:
                print(
                    f"[Kasper] Web search ERROR: {e}",
                    flush=True,
                )
                search_context = ""

    user_content = text

    if search_context:
        user_content = (
            "=== WEB SEARCH RESULTS ===\n"
            f"{search_context}\n"
            "=== END WEB SEARCH RESULTS ===\n\n"
            "ИНСТРУКЦИЯ ДЛЯ ОТВЕТА:\n"
            "Ответь на вопрос пользователя, используя найденные результаты. "
            "Сначала проверь, есть ли в результатах информация, которая "
            "непосредственно отвечает на вопрос. Если есть несколько версий, "
            "не выбирай случайную — укажи расхождение и опирайся на более "
            "надёжный источник. Не придумывай происхождение мема, имя человека, "
            "дату, событие, цену, курс или другой факт, которого нет в источниках. "
            "Если источники не подтверждают ответ, прямо скажи, что надёжного "
            "подтверждения не найдено. Не говори, что веб-поиск недоступен, "
            "если результаты поиска переданы ниже.\n\n"
            f"ВОПРОС ПОЛЬЗОВАТЕЛЯ: {text}"
        )

    messages.append(
        {
            "role": "user",
            "content": user_content,
        }
    )

    try:
        print(
            f"[Kasper] Sending {len(messages)} messages to router...",
            flush=True,
        )

        if not is_group:
            animation_started_at = asyncio.get_event_loop().time()

            animation_message = await message.answer(
                "⏳ thinking ... 0s"
            )

            animation_task = asyncio.create_task(
                _animate_kasper(
                    animation_message,
                    animation_started_at,
                )
            )

        result = await ask(messages)

        if isinstance(result, dict):
            answer = result.get("answer", "")
        elif isinstance(result, tuple):
            answer = result[-1]
        else:
            answer = result

        answer = str(answer).strip()

        if not answer:
            answer = "⚠️ AI вернул пустой ответ."

        if len(answer) > 4000:
            import os as _os

            _os.makedirs(
                "generated_sites",
                exist_ok=True,
            )

            _long_path = (
                f"generated_sites/answer_"
                f"{user_id}_{message.message_id}.txt"
            )

            with open(
                _long_path,
                "w",
                encoding="utf-8-sig",
            ) as _f:
                _f.write(answer)

            answer = (
                "⚠️ Ответ получился слишком длинным для сообщения, "
                "отправляю файлом."
            )

            _send_as_file = _long_path
        else:
            _send_as_file = None

        await save_message(
            user_id,
            "assistant",
            answer,
            chat_id=chat_id,
        )

        asyncio.create_task(
            _maybe_update_conversation_summary(
                user_id,
                chat_id,
            )
        )

        if animation_task:
            animation_task.cancel()

            try:
                await animation_task
            except asyncio.CancelledError:
                pass

        if animation_message:
            think_seconds = max(
                1,
                round(
                    asyncio.get_event_loop().time()
                    - animation_started_at
                ),
            )

            try:
                await animation_message.edit_text(
                    f"⏳ Подумал {think_seconds} сек."
                )
            except Exception:
                pass

            try:
                await message.answer(
                    answer,
                    parse_mode="Markdown",
                )
            except Exception:
                await message.answer(answer)

        elif is_group:
            try:
                await message.reply(
                    answer,
                    parse_mode="Markdown",
                )
            except Exception:
                await message.reply(answer)

        else:
            try:
                await message.answer(
                    answer,
                    parse_mode="Markdown",
                )
            except Exception:
                await message.answer(answer)

        if _send_as_file:
            doc = types.InputFile(_send_as_file)
            await message.answer_document(doc)

    except Exception as e:
        print(
            f"[Kasper] AI ERROR: {e}",
            flush=True,
        )

        error_text = f"⚠️ Ошибка AI: {e}"

        if animation_task:
            animation_task.cancel()

            try:
                await animation_task
            except asyncio.CancelledError:
                pass

        if animation_message:
            try:
                await animation_message.edit_text(error_text)
                return
            except Exception:
                pass

        if is_group:
            await message.reply(error_text)
        else:
            await message.answer(error_text)


MIN_GAME_PLAYERS = 4
MAX_GAME_PLAYERS = 10


def _build_lobby_keyboard(
    bot_username,
    players_count,
    max_players=MAX_GAME_PLAYERS,
):
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    keyboard.add(
        types.InlineKeyboardButton(
            text="☆ 🚀 Присоединиться",
            callback_data="game_join",
        )
    )

    if (
        players_count >= MIN_GAME_PLAYERS
        and players_count < max_players
    ):
        keyboard.add(
            types.InlineKeyboardButton(
                text="▶️ Начать игру",
                callback_data="game_start",
            )
        )

    keyboard.add(
        types.InlineKeyboardButton(
            text="☆ 🛑 Остановить",
            callback_data="game_stop",
        )
    )

    return keyboard


def _build_lobby_text(players, max_players=10):
    lines = [
        "🎮 <b>ТЕНЕВОЙ ГОРОД</b>",
        "Мини-игра для группы.",
        "",
        f"👥 Игроков: {len(players)}/{max_players}",
    ]

    if players:
        lines.append("")

        for _pid, _uid, _tgid, username, _role, _alive in players:
            display_name = (
                f"@{username}"
                if username
                else f"id{_tgid}"
            )

            lines.append(
                f"• {display_name}"
            )

    lines.append("")
    lines.append(
        "Нажми «Присоединиться», чтобы принять участие."
    )

    return "\n".join(lines)


async def cmd_game(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer(
            "🎮 Игра доступна только в групповых чатах."
        )
        return

    chat_id = message.chat.id
    existing_game = await get_active_game(chat_id)

    if existing_game:
        await message.answer(
            "⚠️ В этом чате уже идёт игра. "
            "Дождитесь её окончания или остановите через кнопку."
        )
        return

    game_id = await create_game(chat_id)

    keyboard = _build_lobby_keyboard(
        None,
        0,
    )

    text = _build_lobby_text([])

    sent = await message.answer(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    await set_game_lobby_message(
        game_id,
        sent.message_id,
    )


async def cmd_stopgame(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        return

    chat_id = message.chat.id
    existing_game = await get_active_game(chat_id)

    if not existing_game:
        await message.answer(
            "Сейчас в этом чате нет активной игры."
        )
        return

    game_id = existing_game[0]
    game_status = existing_game[2]

    await set_game_status(
        game_id,
        "finished",
    )

    if game_status == "lobby":
        await message.answer(
            "🛑 Игра остановлена."
        )
    else:
        players = await get_game_players(game_id)

        await message.answer(
            "🛑 Игра остановлена.\n"
            + role_reveal_text(players),
            parse_mode="HTML",
        )


async def handle_game_join(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    game = await get_active_game(chat_id)

    if not game or game[2] != "lobby":
        await callback_query.answer(
            "Сейчас нельзя присоединиться — игра уже началась или её нет.",
            show_alert=True,
        )
        return

    game_id = game[0]
    telegram_user = callback_query.from_user

    user_id = await get_or_create_user(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
    )

    already_in = await is_player_in_game(
        game_id,
        user_id,
    )

    if already_in:
        await callback_query.answer(
            "Ты уже в игре ✅"
        )
        return

    try:
        await callback_query.bot.send_chat_action(
            telegram_user.id,
            "typing",
        )

    except Exception:
        bot_info = await callback_query.bot.get_me()

        await callback_query.answer(
            f"Сначала напиши мне в личку @{bot_info.username} и нажми /start, "
            "потом возвращайся и жми «Присоединиться» ещё раз.",
            show_alert=True,
        )
        return

    added = await add_game_player(
        game_id,
        user_id,
        telegram_user.id,
        username=telegram_user.username,
    )

    if not added:
        await callback_query.answer(
            "Ты уже в игре ✅"
        )
        return

    players = await get_game_players(game_id)

    if len(players) >= MAX_GAME_PLAYERS:
        try:
            await callback_query.message.edit_text(
                "🎮 Игра началась! Роли разосланы в личные сообщения.",
            )
        except Exception as e:
            print(
                f"[game] lobby auto-start edit ERROR: {e}",
                flush=True,
            )

        await callback_query.answer(
            "Лобби заполнено — игра началась! "
            "Проверь личные сообщения от бота 📩",
            show_alert=True,
        )

        await start_game(
            callback_query.bot,
            game_id,
            chat_id,
        )

        return

    keyboard = _build_lobby_keyboard(
        None,
        len(players),
    )

    text = _build_lobby_text(players)

    try:
        await callback_query.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except Exception as e:
        print(
            f"[game] lobby edit ERROR: {e}",
            flush=True,
        )

    await callback_query.answer(
        "Ты в игре! 🎮"
    )


async def handle_game_start(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    game = await get_active_game(chat_id)

    if not game or game[2] != "lobby":
        await callback_query.answer(
            "Сейчас нельзя начать — игра уже началась или её нет.",
            show_alert=True,
        )
        return

    game_id = game[0]
    players = await get_game_players(game_id)

    if len(players) < MIN_GAME_PLAYERS:
        await callback_query.answer(
            f"Нужно минимум {MIN_GAME_PLAYERS} игрока, сейчас {len(players)}.",
            show_alert=True,
        )
        return

    try:
        await callback_query.message.edit_text(
            "🎮 Игра началась! Роли разосланы в личные сообщения.",
        )
    except Exception as e:
        print(
            f"[game] lobby manual-start edit ERROR: {e}",
            flush=True,
        )

    await callback_query.answer(
        "Игра началась! Проверь личные сообщения от бота 📩",
        show_alert=True,
    )

    await start_game(
        callback_query.bot,
        game_id,
        chat_id,
    )


async def handle_game_night_action(callback_query: types.CallbackQuery):
    _, game_id, phase_number, action_type, target_user_id = (
        callback_query.data.split(":")
    )

    game_id = int(game_id)
    phase_number = int(phase_number)
    target_user_id = int(target_user_id)

    telegram_user = callback_query.from_user

    actor_user_id = await get_or_create_user(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
    )

    await handle_night_action(
        game_id,
        phase_number,
        action_type,
        actor_user_id,
        target_user_id,
    )

    verb = (
        "устранить"
        if action_type == "kill"
        else "проверить"
    )

    await callback_query.answer(
        f"Выбор сохранён ✅ ({verb})"
    )

    try:
        await callback_query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass


async def handle_game_vote_action(callback_query: types.CallbackQuery):
    _, game_id, target_user_id = callback_query.data.split(":")

    game_id = int(game_id)
    target_user_id = int(target_user_id)

    game = await get_active_game(
        callback_query.message.chat.id
    )

    if not game or game[0] != game_id or game[2] != "voting":
        await callback_query.answer(
            "Голосование уже завершено.",
            show_alert=True,
        )
        return

    phase_number = game[5]

    telegram_user = callback_query.from_user

    voter_user_id = await get_or_create_user(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
    )

    players = await get_game_players(game_id)

    voter_row = next(
        (
            p
            for p in players
            if p[1] == voter_user_id
        ),
        None,
    )

    if voter_row is None:
        await callback_query.answer(
            "Ты не участвуешь в этой игре.",
            show_alert=True,
        )
        return

    if voter_row[5] != 1:
        await callback_query.answer(
            "Ты уже выбыл(а) из игры — голосовать нельзя 💀",
            show_alert=True,
        )
        return

    if target_user_id == voter_user_id:
        await callback_query.answer(
            "Нельзя голосовать за самого себя 🙅",
            show_alert=True,
        )
        return

    if target_user_id != SKIP_TARGET_ID:
        target_alive = next(
            (
                p
                for p in players
                if p[1] == target_user_id
                and p[5] == 1
            ),
            None,
        )

        if target_alive is None:
            await callback_query.answer(
                "Этот игрок уже выбыл — выбери другого.",
                show_alert=True,
            )
            return

    await handle_vote_action(
        game_id,
        phase_number,
        voter_user_id,
        target_user_id,
    )

    await callback_query.answer(
        "Голос учтён ✅"
    )

    voter_name = (
        f"@{telegram_user.username}"
        if telegram_user.username
        else f"id{telegram_user.id}"
    )

    if target_user_id == SKIP_TARGET_ID:
        announce = (
            f"🗳 {voter_name} решил(а) пропустить голос."
        )
    else:
        target_row = next(
            (
                p
                for p in players
                if p[1] == target_user_id
            ),
            None,
        )

        if target_row:
            (
                _pid,
                _uid,
                target_telegram_id,
                target_username,
                _role,
                _alive,
            ) = target_row

            target_name = (
                f"@{target_username}"
                if target_username
                else f"id{target_telegram_id}"
            )
        else:
            target_name = "неизвестного игрока"

        announce = (
            f"🗳 {voter_name} проголосовал(а) за "
            f"{target_name}"
        )

    try:
        await callback_query.bot.send_message(
            callback_query.message.chat.id,
            announce,
        )
    except Exception as e:
        print(
            f"[game] vote announce ERROR: {e}",
            flush=True,
        )


async def handle_game_stop(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    game = await get_active_game(chat_id)

    if not game:
        await callback_query.answer(
            "Игра уже завершена.",
            show_alert=True,
        )
        return

    game_id = game[0]
    game_status = game[2]

    await set_game_status(
        game_id,
        "finished",
    )

    try:
        if game_status == "lobby":
            await callback_query.message.edit_text(
                "🛑 Игра остановлена."
            )
        else:
            players = await get_game_players(game_id)

            await callback_query.message.edit_text(
                "🛑 Игра остановлена.\n"
                + role_reveal_text(players),
                parse_mode="HTML",
            )

    except Exception:
        pass

    await callback_query.answer(
        "Игра остановлена."
    )


class BanCheckMiddleware(BaseMiddleware):
    async def on_process_message(
        self,
        message: types.Message,
        data: dict,
    ):
        user_id = message.from_user.id

        # Администраторов нельзя заблокировать этим middleware.
        # Даже если админ случайно сделал /ban самому себе,
        # он всё равно сможет выполнить /unban.
        if user_id in ADMIN_IDS:
            return

        if await is_user_banned(user_id):
            raise CancelHandler()

    async def on_process_callback_query(
        self,
        callback_query: types.CallbackQuery,
        data: dict,
    ):
        user_id = callback_query.from_user.id

        # Администраторов нельзя заблокировать этим middleware.
        if user_id in ADMIN_IDS:
            return

        if await is_user_banned(user_id):
            raise CancelHandler()


class BusinessDiagnosticMiddleware(BaseMiddleware):
    async def on_pre_process_update(
        self,
        update: types.Update,
        data: dict,
    ):
        inspect_update_for_business_fields(update)


async def _resolve_target_telegram_id(message: types.Message):
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
    ):
        return message.reply_to_message.from_user.id

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        return None

    arg = parts[1].strip()

    if arg.startswith("@"):
        return await find_telegram_id_by_username(arg)

    if arg.lstrip("-").isdigit():
        return int(arg)

    return None


async def cmd_ban(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    target_id = await _resolve_target_telegram_id(message)

    if not target_id:
        await message.answer(
            "Использование: <code>/ban telegram_id</code> или "
            "<code>/ban @username</code>, либо отправь /ban "
            "ответом на сообщение нужного пользователя.",
            parse_mode="HTML",
        )
        return

    await set_user_banned(
        target_id,
        True,
    )

    await message.answer(
        f"🚫 Пользователь <code>{target_id}</code> забанен.",
        parse_mode="HTML",
    )


async def cmd_unban(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    target_id = await _resolve_target_telegram_id(message)

    if not target_id:
        await message.answer(
            "Использование: <code>/unban telegram_id</code> или "
            "<code>/unban @username</code>, либо отправь /unban "
            "ответом на сообщение нужного пользователя.",
            parse_mode="HTML",
        )
        return

    await set_user_banned(
        target_id,
        False,
    )

    await message.answer(
        f"✅ Пользователь <code>{target_id}</code> разбанен.",
        parse_mode="HTML",
    )


async def cmd_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    total_users = await get_total_users_count()
    active_24h = await get_active_users_count(hours=24)
    messages_total = await get_total_messages_count()
    messages_24h = await get_total_messages_count(hours=24)
    provider_stats = await get_provider_stats(hours=24)

    lines = [
        "📊 <b>Статистика Kasper AI</b>",
        "",
        f"👥 Пользователей всего: {total_users}",
        f"🟢 Активных за 24ч: {active_24h}",
        f"💬 Сообщений всего: {messages_total}",
        f"💬 Сообщений за 24ч: {messages_24h}",
        "",
        "🧠 <b>Провайдеры за 24ч:</b>",
    ]

    if provider_stats:
        for p in provider_stats:
            fail_rate = (
                f" ({p['failed']}/{p['total']} упало)"
                if p["failed"]
                else ""
            )

            lines.append(
                f"• {p['provider']}: "
                f"{p['total']} запросов{fail_rate}"
            )
    else:
        lines.append(
            "• пока нет данных за этот период"
        )

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
    )


async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    photo_file_id = None
    text = None
    entities = None

    if message.reply_to_message:
        src = message.reply_to_message

        if src.photo:
            photo_file_id = src.photo[-1].file_id
            text = src.caption or ""
            entities = src.caption_entities or None
        else:
            text = src.text or src.caption or ""
            entities = (
                src.entities
                or src.caption_entities
                or None
            )

    else:
        parts = message.text.split(maxsplit=1)

        if len(parts) < 2:
            await message.answer(
                "Использование: <code>/broadcast текст</code>, "
                "либо отправь /broadcast ответом на сообщение "
                "(текст или фото с подписью), которое нужно разослать.",
                parse_mode="HTML",
            )
            return

        text = parts[1]

        if message.entities:
            prefix_len = len(message.text) - len(text)
            adjusted = []

            for ent in message.entities:
                if ent.offset + ent.length <= prefix_len:
                    continue

                new_offset = ent.offset - prefix_len

                if new_offset < 0:
                    continue

                new_ent = types.MessageEntity(
                    type=ent.type,
                    offset=new_offset,
                    length=ent.length,
                    url=ent.url,
                    user=ent.user,
                    language=ent.language,
                    custom_emoji_id=getattr(
                        ent,
                        "custom_emoji_id",
                        None,
                    ),
                )

                adjusted.append(new_ent)

            entities = adjusted or None

    telegram_ids = await get_all_telegram_ids()

    status_message = await message.answer(
        f"📤 Рассылка начата, получателей: "
        f"{len(telegram_ids)}..."
    )

    sent = 0
    failed = 0

    for telegram_id in telegram_ids:
        try:
            if photo_file_id:
                await message.bot.send_photo(
                    telegram_id,
                    photo_file_id,
                    caption=text or None,
                    caption_entities=entities,
                )
            else:
                await message.bot.send_message(
                    telegram_id,
                    text,
                    entities=entities,
                )

            sent += 1

        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    try:
        await status_message.edit_text(
            f"✅ Рассылка завершена. "
            f"Отправлено: {sent}, не доставлено: {failed}."
        )
    except Exception:
        pass


async def handle_voice(message: types.Message):
    is_group = message.chat.type in (
        "group",
        "supergroup",
    )

    if is_group:
        is_reply_to_bot = False

        if message.reply_to_message:
            bot_info = await message.bot.get_me()

            if (
                message.reply_to_message.from_user
                and message.reply_to_message.from_user.id == bot_info.id
            ):
                is_reply_to_bot = True

        if not is_reply_to_bot:
            return

    user_id = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    allowed, remaining = await check_and_increment_limit(
        user_id,
        daily_limit=20,
        telegram_id=message.from_user.id,
    )

    if not allowed:
        await message.answer(
            "⛔ Лимит исчерпан, ждите сброса."
        )
        return

    chat_id = (
        message.chat.id
        if is_group
        else None
    )

    await handle_voice_message(
        bot=message.bot,
        message=message,
        ai_ask_fn=ask_provider,
        get_history_fn=get_history,
        save_message_fn=save_message,
        user_id=user_id,
        chat_id=chat_id,
    )


def register_handlers(dp: Dispatcher):
    dp.middleware.setup(
        BanCheckMiddleware()
    )

    dp.middleware.setup(
        BusinessDiagnosticMiddleware()
    )

    dp.register_message_handler(
        cmd_start,
        commands=["start"],
    )

    dp.register_message_handler(
        cmd_help,
        commands=["help"],
    )

    dp.register_message_handler(
        cmd_limit,
        commands=["limit"],
    )

    dp.register_message_handler(
        cmd_status,
        commands=["status"],
    )

    dp.register_message_handler(
        cmd_stats,
        commands=["stats"],
    )

    dp.register_message_handler(
        cmd_gamestats,
        commands=["gamestats"],
    )

    dp.register_message_handler(
        cmd_game,
        commands=["shadowcity"],
    )

    dp.register_message_handler(
        cmd_stopgame,
        commands=["stopshadowcity"],
    )

    dp.register_message_handler(
        cmd_ban,
        commands=["ban"],
    )

    dp.register_message_handler(
        cmd_unban,
        commands=["unban"],
    )

    dp.register_message_handler(
        cmd_broadcast,
        commands=["broadcast"],
    )

    dp.register_message_handler(
        cmd_agent,
        commands=["agent"],
    )

    dp.register_callback_query_handler(
        handle_agent_confirm,
        lambda c: c.data == "agent_confirm",
    )

    dp.register_callback_query_handler(
        handle_agent_cancel,
        lambda c: c.data == "agent_cancel",
    )

    dp.register_callback_query_handler(
        handle_agent_edit,
        lambda c: c.data == "agent_edit",
    )

    dp.register_callback_query_handler(
        handle_agent_stop,
        lambda c: c.data == "agent_stop",
    )

    dp.register_callback_query_handler(
        handle_game_join,
        lambda c: c.data == "game_join",
    )

    dp.register_callback_query_handler(
        handle_game_stop,
        lambda c: c.data == "game_stop",
    )

    dp.register_callback_query_handler(
        handle_game_start,
        lambda c: c.data == "game_start",
    )

    dp.register_callback_query_handler(
        handle_game_night_action,
        lambda c: c.data
        and c.data.startswith("game_night:"),
    )

    dp.register_callback_query_handler(
        handle_game_vote_action,
        lambda c: c.data
        and c.data.startswith("game_vote:"),
    )

    dp.register_message_handler(
        handle_new_chat_members,
        content_types=types.ContentTypes.NEW_CHAT_MEMBERS,
    )

    dp.register_message_handler(
        handle_voice,
        content_types=types.ContentTypes.VOICE,
    )

    dp.register_message_handler(
        handle_message,
        content_types=types.ContentTypes.TEXT,
    )
