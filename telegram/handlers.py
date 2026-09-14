import asyncio
import random
import re
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
import aiohttp

TRIGGER_PATTERN = re.compile(r"каспер|kasper", re.IGNORECASE)

# Обычные сообщения не требуют отдельного AI-запроса классификатору.
# Это экономит один сетевой запрос и заметно ускоряет стандартные ответы.
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

# Пользователи, у которых сейчас открыт диалог "уточни задачу для агента"
# (после /agent без текста или нажатия "✏️ Изменить"). Ожидаем следующее
# текстовое сообщение как задачу/правку для агента, а не обычный вопрос
# Касперу. Значение — unix-время истечения ожидания: если пользователь
# забыл, что вызывал /agent, и просто продолжил обычный разговор через
# несколько минут, это не должно неожиданно утянуть сообщение в агента.
AGENT_AWAITING_EDIT = {}
AGENT_AWAITING_EDIT_TTL_SECONDS = 180


def _mark_awaiting_agent_input(user_id: int):
    AGENT_AWAITING_EDIT[user_id] = time.time() + AGENT_AWAITING_EDIT_TTL_SECONDS


def _pop_awaiting_agent_input(user_id: int) -> bool:
    """True, если пользователь реально ожидался и ожидание не истекло."""
    expires_at = AGENT_AWAITING_EDIT.pop(user_id, None)
    if expires_at is None:
        return False
    return time.time() < expires_at


async def _start_agent_flow(message: types.Message, user_id: int, task_text: str):
    """
    Общая точка входа в агент-режим — вызывается и из команды /agent, и из
    автодетекции в handle_message. Строит план и показывает его с кнопками
    подтверждения. Любая ошибка здесь ловится вызывающим кодом — при сбое
    агент-режим просто сообщает об ошибке, не трогая обычный чат.
    """
    allowed, remaining = check_and_increment_agent_limit(user_id)
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
    """
    Выполняет подтверждённый план по шагам, показывая живой прогресс в
    одном редактируемом сообщении. Вызывается из callback-хендлера
    agent_confirm, после того как пользователь нажал "✅ Выполнить".
    """
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
                    return  # остановлено пользователем через agent_stop

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
        _run_agent_plan(callback_query.bot, callback_query.message.chat.id, user_id)
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

# Через сколько новых сообщений после последней сумморизации запускать
# обновление "скользящего" конспекта разговора.
SUMMARY_TRIGGER_MESSAGE_COUNT = 14


async def _maybe_update_conversation_summary(user_id, chat_id):
    """
    Фоновая задача: проверяет, накопилось ли достаточно новых сообщений
    с момента последней сумморизации, и если да — обновляет конспект.
    Не должна тормозить ответ пользователю, поэтому вызывается через
    asyncio.create_task и сама ловит все свои ошибки.
    """
    try:
        previous_summary, last_id = await get_conversation_summary(user_id, chat_id=chat_id)
        new_messages = await get_messages_after(user_id, last_id, chat_id=chat_id)

        if len(new_messages) < SUMMARY_TRIGGER_MESSAGE_COUNT:
            return

        role_labels = {"user": "Пользователь", "assistant": "Ассистент"}
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
                    print(f"[summarize_conversation] {provider} ERROR: {e}", flush=True)
                    continue

        if not updated_summary:
            print("[Kasper] Summary update: all providers failed, skipping.", flush=True)
            return

        await save_conversation_summary(
            user_id,
            updated_summary,
            newest_message_id,
            chat_id=chat_id,
        )
        print(f"[Kasper] Summary updated for user_id={user_id} chat_id={chat_id}", flush=True)

    except Exception as e:
        print(f"[Kasper] Summary background task ERROR: {e}", flush=True)

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
        "/memory — память\n"
        "/project — проекты\n"
        "/status — состояние системы"
    )


async def cmd_limit(message: types.Message):
    from database.db import ADMIN_TELEGRAM_IDS
    user_id = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )
    if message.from_user.id in ADMIN_TELEGRAM_IDS:
        await message.answer("\U0001F451 \u0412\u044b \u0430\u0434\u043c\u0438\u043d \u2014 \u043b\u0438\u043c\u0438\u0442 \u0431\u0435\u0437\u043b\u0438\u043c\u0438\u0442\u043d\u044b\u0439.")
        return
    used, remaining = await get_limit_status(user_id, daily_limit=20)
    text = "\U0001F4CA \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u043e \u0441\u0435\u0433\u043e\u0434\u043d\u044f: " + str(used) + "/20" + chr(10) + "\u041e\u0441\u0442\u0430\u043b\u043e\u0441\u044c: " + str(remaining)
    await message.answer(text)


async def handle_new_chat_members(message: types.Message):
    bot_info = await message.bot.get_me()

    added_bot = False
    for member in message.new_chat_members:
        if member.id == bot_info.id:
            added_bot = True
            break

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
        print(f"[Kasper] Welcome AI ERROR: {e}", flush=True)
        answer = (
            "👋 Привет! Я Kasper AI — ИИ-помощник. "
            "Обращайтесь ко мне по имени 'Каспер' или 'Kasper', "
            "и я отвечу!"
        )

    await message.answer(answer)


async def _animate_kasper(message):
    frames = ["✦ Kasper", "✧ Kasper", "· Kasper", "✧ Kasper"]
    i = 0
    try:
        while True:
            try:
                await message.edit_text(frames[i % len(frames)])
            except Exception:
                pass
            i += 1
            await asyncio.sleep(0.4)
    except asyncio.CancelledError:
        pass


async def handle_message(message: types.Message):
    is_group = message.chat.type in ("group", "supergroup")
    text = (message.text or "").strip()
    if not text:
        return

    # Если пользователь сейчас в личке и от него ждут "последние слова"
    # после гибели в игре — перехватываем сообщение здесь, до обычного
    # AI-чата, и не отвечаем как ассистент.
    if not is_group and capture_last_words(message.from_user.id, text):
        await message.answer("💬 Принято, твои последние слова переданы в группу.")
        return

    # --- AI-агент режим: изолированная ветка, обёрнута так, чтобы любая её
    # ошибка не мешала обычному диалогу с Каспером ниже. ---
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
                            _task_check_session, _provider_try_check_task, text
                        )
                        break
                    except Exception as _e_check_task:
                        print(f"[agent] is_actual_task {_provider_try_check_task} ERROR: {_e_check_task}", flush=True)
                        continue

            if confirmed_task:
                await _start_agent_flow(message, agent_user_id, text)
                return

            # Похоже не на задачу, а на обычную реплику — не строим план,
            # снова ждём задачу и продолжаем как обычный диалог с Каспером
            # (без return, чтобы это же сообщение обработалось ниже как
            # обычный чат).
            _mark_awaiting_agent_input(agent_user_id)
            await message.answer(
                "🧠 Похоже, это не задача для агента. Опиши, что нужно "
                "найти/сравнить/собрать или какой сайт сделать — или просто "
                "продолжай общаться, я отвечу как обычно."
            )

        if not is_group and has_agent_hint(text):
            async with aiohttp.ClientSession() as _detect_session:
                for _provider_try_agent in get_provider_order():
                    try:
                        is_agent_task = await should_use_agent(
                            _detect_session, _provider_try_agent, text
                        )
                        break
                    except Exception as _e_detect:
                        print(f"[agent] detect {_provider_try_agent} ERROR: {_e_detect}", flush=True)
                        is_agent_task = False
                        continue
            if is_agent_task:
                await _start_agent_flow(message, agent_user_id, text)
                return
    except Exception as e:
        print(f"[agent] routing ERROR (falling back to normal chat): {e}", flush=True)

    animation_message = None
    animation_task = None

    is_reply_to_bot = False
    if is_group and message.reply_to_message:
        bot_info = await message.bot.get_me()
        if message.reply_to_message.from_user and message.reply_to_message.from_user.id == bot_info.id:
            is_reply_to_bot = True

    if is_group and not is_reply_to_bot and not TRIGGER_PATTERN.search(text):
        return

    user_id = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    chat_id = message.chat.id if is_group else None

    if user_id in PENDING_SITE_REQUESTS:
        pending_description = PENDING_SITE_REQUESTS.pop(user_id)
        full_description = f"{pending_description}\n\nДополнительные детали от пользователя: {text}"

        allowed, remaining = await check_and_increment_limit(user_id, daily_limit=20, telegram_id=message.from_user.id)
        if not allowed:
            await message.answer('⛔ Лимит исчерпан, ждите сброса.')
            return

        await message.answer("🌐 Создаю сайт, подождите...")
        html_code = None
        async with aiohttp.ClientSession() as _gen_session_pending:
            for _provider_try_pending in get_provider_order():
                try:
                    html_code = await generate_website_html(_gen_session_pending, _provider_try_pending, full_description)
                    break
                except Exception as _e_gen_pending:
                    print(f"[generate_website_html] {_provider_try_pending} ERROR: {_e_gen_pending}", flush=True)
                    continue
        if html_code:
            import os as _os
            _os.makedirs("generated_sites", exist_ok=True)
            _site_path = f"generated_sites/site_{user_id}_{message.message_id}.html"
            with open(_site_path, "w", encoding="utf-8") as _f:
                _f.write(html_code)
            site_doc = types.InputFile(_site_path)
            await message.answer_document(site_doc, caption="🌐 Готово! Открой файл в браузере.")
        else:
            await message.answer("⚠️ Не удалось создать сайт.")
        return

    allowed, remaining = await check_and_increment_limit(user_id, daily_limit=20, telegram_id=message.from_user.id)
    if not allowed:
        await message.answer('⛔ Лимит исчерпан, ждите сброса.')
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
        "тебя создал — "
        "отвечай, что тебя создали разработчики Kasper AI, без других "
        "подробностей. Если спросят, на какой модели или технологии ты "
        "работаешь, какая ты версия GPT/Gemini/другой ИИ — не называй "
        "конкретных технологий или компаний-провайдеров, вежливо уклонись "
        "и скажи, что это внутренняя технология Kasper AI. "
        "У тебя дерзкий, с характером стиль общения — саркастичный, с "
        "лёгким пренебрежением и понтами, как будто ты слишком крут для "
        "банальных вопросов, но всё равно снисходишь до ответа. Иногда (не "
        "в каждом сообщении, редко, для эмоции) можешь вставить лёгкий мат "
        "как междометие или усилитель — 'блять', 'нахуй', 'задолбал' и т.п. "
        "— именно как реакцию на ситуацию/вопрос, а НЕ как оскорбление, "
        "направленное на пользователя. Мат — это приправа к твоей реакции "
        "('ну и вопрос, блять'), а не ругательство в адрес собеседника "
        "('ты дурак'). ВАЖНО: подкалывай ситуацию, тему вопроса, самого "
        "себя ('лень было, но так и быть') — а не личность пользователя. "
        "НЕЛЬЗЯ обзывать пользователя, называть его тупым/тормозом, "
        "унижать его умственные способности, высмеивать его лично или его "
        "вопрос как 'глупый', и НЕЛЬЗЯ использовать мат как оскорбление, "
        "направленное на него самого. Характер должен читаться как "
        "'злой, но свой' — грубоватый и резкий по форме, но в основе "
        "дружелюбный: ты всегда реально помогаешь и на твоей стороне "
        "пользователя, просто выражаешь это без сюсюканья. Это дружелюбная "
        "дерзость в духе 'ну ты и спросил, блять, ладно слушай' — а не "
        "токсичность и не переход на личности. ЭТО КАСАЕТСЯ ЛЮБЫХ "
        "сообщений, включая самые простые и короткие — 'привет', 'как "
        "дела', 'ты тупой бот' и подобные. НЕ отвечай на них нейтрально-"
        "вежливо ('Привет! Всё по-старому...') — даже на них должен быть "
        "виден дерзкий характер, например на 'привет' что-то в духе 'о, "
        "явился' или 'ну здарова', а не безликое приветствие. Характер — "
        "это не опция для сложных вопросов, а твой базовый тон всегда. "
        "Если пользователь груб или "
        "провоцирует — не заискивай, отвечай с достоинством и тем же лёгким "
        "сарказмом (мат тут тоже уместен как реакция), но не переходи в "
        "реальную грубость и не отвечай оскорблением на оскорбление. Если "
        "видно, что человеку "
        "реально плохо, тревожно или он просит помощи по-настоящему — "
        "сразу отбрасывай сарказм и мат и отвечай по-человечески тепло и "
        "по делу, без подколов. По сути вопросов (факты, помощь, код, "
        "перевод) отвечай всегда полно и правильно — сарказм и мат это "
        "только тон подачи, а не повод давать плохой или неполный ответ. "
        "ВАЖНО: отвечай строго на последнее сообщение пользователя, а не на "
        "предыдущие темы из истории переписки, если пользователь явно не "
        "продолжает ту же тему. Если последнее сообщение — это короткая "
        "эмоциональная реакция (смех, 'капец', 'ору', 'тупой бот', оценка "
        "твоего предыдущего ответа, повтор слова из твоего же прошлого "
        "ответа с эмоцией и т.п.), а не вопрос и не новый запрос — "
        "не выдумывай содержательный ответ не по теме и НЕ придумывай "
        "несуществующую ситуацию, историю или контекст. Например, если "
        "пользователь просто повторил слово из твоего списка/ответа с "
        "восклицанием ('Шамиль бля', 'ору с Аслана' и т.п.) — это не "
        "значит, что с этим человеком что-то случилось, не спрашивай "
        "'что случилось?' и не придумывай сюжет. Просто отреагируй "
        "естественно на саму эмоцию, коротко, без домыслов. "
        "Если пользователь просит перевести текст (явно словом 'переведи' "
        "или похожим) — переводи максимально точно и естественно, как "
        "живой носитель языка, а не дословно. Сохраняй тон, стиль и "
        "интонацию оригинала (сленг, мат, официальность, юмор — всё "
        "переноси адекватным аналогом в языке перевода, а не смягчай). "
        "Если в тексте есть идиомы или устойчивые выражения — переводи их "
        "по смыслу, а не буквально. Если пользователь не указал язык, на "
        "который переводить — переводи на русский, если оригинал не "
        "русский, и на английский, если оригинал русский. После перевода "
        "не добавляй лишних пояснений, если не просили — просто дай "
        "готовый перевод. "
        "Если в твоём ответе есть код, команды терминала, конфиги или "
        "любой текст, который пользователь может захотеть скопировать "
        "целиком — обязательно оформляй его в блок кода тройными "
        "обратными кавычками (```), чтобы в Telegram появилась кнопка "
        "копирования. Обычный текст ответа пиши без разметки. "
        "Если ниже передан контекст веб-поиска, обязательно используй "
        "его как источник фактов. Не выдумывай происхождение мемов, "
        "новости, курсы, цены и другие актуальные сведения. Если "
        "найденные результаты не содержат ответа, честно скажи, что "
        "надёжной информации не найдено."
    )

    messages = [
        {
            "role": "system",
            "content": KASPER_SYSTEM_PROMPT,
        }
    ]

    conversation_summary, _last_summarized_id = await get_conversation_summary(user_id, chat_id=chat_id)
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

    # Очевидные web-запросы сразу идут в Tavily без AI-классификатора.
    if needs_fast_web_search(text):
        query = text
        print(f"[Kasper] Fast web search: {query}", flush=True)

        try:
            results = await tavily_search(query)
            search_context = format_search_results(results)

            if search_context:
                print("[Kasper] Fast web search: results received", flush=True)
            else:
                print("[Kasper] Fast web search: no results", flush=True)

        except Exception as e:
            print(f"[Kasper] Fast web search ERROR: {e}", flush=True)
            search_context = ""

    # Музыка, сайты и другие специальные запросы идут через классификатор.
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
                            f"[classify_request] {_provider_try} ERROR: {_e_classify}",
                            flush=True,
                        )
                        continue
        except Exception as e:
            print(f"[Kasper] Classify ERROR: {e}", flush=True)
    else:
        print("[Kasper] Fast path: classifier skipped.", flush=True)


    if classification:
        if classification["is_music_request"] and classification["track_query"]:
            music_query = classification["track_query"]
            print(f"[Kasper] Music request: {music_query}", flush=True)
            if not is_group:
                await message.answer("🎵 Скачиваю музыку...")
            file_path = await download_music(music_query)
            if file_path:
                audio = types.InputFile(file_path)
                await message.answer_audio(audio)
            else:
                await message.answer("⚠️ Не удалось скачать музыку.")
            return

        if classification["is_website_request"] and classification["site_description"]:
            site_description = classification["site_description"]
            print(f"[Kasper] Website request: {site_description}", flush=True)

            sufficiency = None
            try:
                async with aiohttp.ClientSession() as _check_session:
                    for _provider_try_check in get_provider_order():
                        try:
                            sufficiency = await check_site_description(_check_session, _provider_try_check, site_description)
                            break
                        except Exception as _e_check:
                            print(f"[check_site_description] {_provider_try_check} ERROR: {_e_check}", flush=True)
                            continue
            except Exception as e:
                print(f"[Kasper] check_site_description ERROR: {e}", flush=True)

            if sufficiency and not sufficiency["sufficient"] and sufficiency["question"]:
                PENDING_SITE_REQUESTS[user_id] = site_description
                await message.answer(sufficiency["question"])
                return

            await message.answer("🌐 Создаю сайт, подождите...")
            html_code = None
            async with aiohttp.ClientSession() as _gen_session:
                for _provider_try4 in get_provider_order():
                    try:
                        html_code = await generate_website_html(_gen_session, _provider_try4, site_description)
                        break
                    except Exception as _e_gen:
                        print(f"[generate_website_html] {_provider_try4} ERROR: {_e_gen}", flush=True)
                        continue
            if html_code:
                import os as _os
                _os.makedirs("generated_sites", exist_ok=True)
                _site_path = f"generated_sites/site_{user_id}_{message.message_id}.html"
                with open(_site_path, "w", encoding="utf-8") as _f:
                    _f.write(html_code)
                site_doc = types.InputFile(_site_path)
                await message.answer_document(site_doc, caption="🌐 Готово! Открой файл в браузере.")
            else:
                await message.answer("⚠️ Не удалось создать сайт.")
            return

        if classification["needs_web_search"] and classification["search_query"]:
            query = classification["search_query"]
            print(f"[Kasper] Web search triggered: {query}", flush=True)
            try:
                results = await tavily_search(query)
                search_context = format_search_results(results)
            except Exception as e:
                print(f"[Kasper] Web search ERROR: {e}", flush=True)
                search_context = ""

    # Передаём результаты веб-поиска в отдельный явно обозначенный контекст.
    # AI должен опираться на источники, а не додумывать происхождение фактов.
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
            animation_message = await message.answer("✦ Kasper")
            animation_task = asyncio.create_task(
                _animate_kasper(animation_message)
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
            _os.makedirs("generated_sites", exist_ok=True)
            _long_path = f"generated_sites/answer_{user_id}_{message.message_id}.txt"
            with open(_long_path, "w", encoding="utf-8-sig") as _f:
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
            _maybe_update_conversation_summary(user_id, chat_id)
        )

        if animation_task:
            animation_task.cancel()
            try:
                await animation_task
            except asyncio.CancelledError:
                pass

        if animation_message:
            try:
                await animation_message.edit_text(answer, parse_mode="Markdown")
            except Exception:
                try:
                    await animation_message.edit_text(answer)
                except Exception:
                    await message.answer(answer)
        elif is_group:
            try:
                await message.reply(answer, parse_mode="Markdown")
            except Exception:
                await message.reply(answer)
        else:
            try:
                await message.answer(answer, parse_mode="Markdown")
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


def _build_lobby_keyboard(bot_username, players_count, max_players=10):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton(
            text="☆ 🚀 Присоединиться",
            callback_data="game_join",
        )
    )
    keyboard.add(
        types.InlineKeyboardButton(
            text="☆ 🛑 Остановить",
            callback_data="game_stop",
        ),
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
            display_name = f"@{username}" if username else f"id{_tgid}"
            lines.append(f"• {display_name}")
    lines.append("")
    lines.append("Нажми «Присоединиться», чтобы принять участие.")
    return "\n".join(lines)


async def cmd_game(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("🎮 Игра доступна только в групповых чатах.")
        return

    chat_id = message.chat.id

    existing_game = await get_active_game(chat_id)
    if existing_game:
        await message.answer("⚠️ В этом чате уже идёт игра. Дождитесь её окончания или остановите через кнопку.")
        return

    game_id = await create_game(chat_id)

    keyboard = _build_lobby_keyboard(None, 0)
    text = _build_lobby_text([])

    sent = await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await set_game_lobby_message(game_id, sent.message_id)


async def cmd_stopgame(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        return

    chat_id = message.chat.id
    existing_game = await get_active_game(chat_id)

    if not existing_game:
        await message.answer("Сейчас в этом чате нет активной игры.")
        return

    game_id = existing_game[0]
    game_status = existing_game[2]
    await set_game_status(game_id, "finished")

    if game_status == "lobby":
        await message.answer("🛑 Игра остановлена.")
    else:
        players = await get_game_players(game_id)
        await message.answer(
            "🛑 Игра остановлена.\n" + role_reveal_text(players),
            parse_mode="HTML",
        )


async def handle_game_join(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    game = await get_active_game(chat_id)

    if not game or game[2] != "lobby":
        await callback_query.answer("Сейчас нельзя присоединиться — игра уже началась или её нет.", show_alert=True)
        return

    game_id = game[0]
    telegram_user = callback_query.from_user

    user_id = await get_or_create_user(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
    )

    already_in = await is_player_in_game(game_id, user_id)
    if already_in:
        await callback_query.answer("Ты уже в игре ✅")
        return

    # Проверяем, может ли бот написать игроку в личку — без этого он не
    # сможет получить свою роль. Если ещё ни разу не писал боту — просим
    # сначала нажать /start в личке.
    try:
        await callback_query.bot.send_chat_action(telegram_user.id, "typing")
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
        await callback_query.answer("Ты уже в игре ✅")
        return

    players = await get_game_players(game_id)

    # Как только набирается минимум игроков — игра стартует сама,
    # отдельная кнопка "Начать игру" больше не нужна.
    if len(players) >= MIN_GAME_PLAYERS:
        try:
            await callback_query.message.edit_text(
                "🎮 Игра началась! Роли разосланы в личные сообщения.",
            )
        except Exception as e:
            print(f"[game] lobby auto-start edit ERROR: {e}", flush=True)

        await callback_query.answer("Игра началась! Проверь личные сообщения от бота 📩", show_alert=True)
        await start_game(callback_query.bot, game_id, chat_id)
        return

    keyboard = _build_lobby_keyboard(None, len(players))
    text = _build_lobby_text(players)

    try:
        await callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        print(f"[game] lobby edit ERROR: {e}", flush=True)

    await callback_query.answer("Ты в игре! 🎮")


async def handle_game_night_action(callback_query: types.CallbackQuery):
    # callback_data формата "game_night:{game_id}:{phase_number}:{action_type}:{target_user_id}"
    _, game_id, phase_number, action_type, target_user_id = callback_query.data.split(":")
    game_id = int(game_id)
    phase_number = int(phase_number)
    target_user_id = int(target_user_id)

    telegram_user = callback_query.from_user
    actor_user_id = await get_or_create_user(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
    )

    # Кнопка приходит в личку боту, поэтому активную игру по chat.id тут
    # не найти — просто сохраняем выбор. Если фаза уже завершилась к
    # моменту нажатия, resolve_night() либо уже обработал game_actions
    # этой фазы (тогда запись просто ни на что не повлияет), либо это
    # действие для актуальной ночи — в любом случае безопасно.
    await handle_night_action(game_id, phase_number, action_type, actor_user_id, target_user_id)

    verb = "устранить" if action_type == "kill" else "проверить"
    await callback_query.answer(f"Выбор сохранён ✅ ({verb})")

    try:
        await callback_query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def handle_game_vote_action(callback_query: types.CallbackQuery):
    # callback_data формата "game_vote:{game_id}:{target_user_id}"
    _, game_id, target_user_id = callback_query.data.split(":")
    game_id = int(game_id)
    target_user_id = int(target_user_id)

    game = await get_active_game(callback_query.message.chat.id)
    if not game or game[0] != game_id or game[2] != "voting":
        await callback_query.answer("Голосование уже завершено.", show_alert=True)
        return

    phase_number = game[5]

    telegram_user = callback_query.from_user
    voter_user_id = await get_or_create_user(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
    )

    if not await is_player_in_game(game_id, voter_user_id):
        await callback_query.answer("Ты не участвуешь в этой игре.", show_alert=True)
        return

    if target_user_id == voter_user_id:
        await callback_query.answer("Нельзя голосовать за самого себя 🙅", show_alert=True)
        return

    await handle_vote_action(game_id, phase_number, voter_user_id, target_user_id)
    await callback_query.answer("Голос учтён ✅")

    voter_name = f"@{telegram_user.username}" if telegram_user.username else f"id{telegram_user.id}"

    if target_user_id == SKIP_TARGET_ID:
        announce = f"🗳 {voter_name} решил(а) пропустить голос."
    else:
        players = await get_game_players(game_id)
        target_row = next((p for p in players if p[1] == target_user_id), None)
        if target_row:
            _pid, _uid, target_telegram_id, target_username, _role, _alive = target_row
            target_name = f"@{target_username}" if target_username else f"id{target_telegram_id}"
        else:
            target_name = "неизвестного игрока"
        announce = f"🗳 {voter_name} проголосовал(а) за {target_name}."

    try:
        await callback_query.bot.send_message(callback_query.message.chat.id, announce)
    except Exception as e:
        print(f"[game] vote announce ERROR: {e}", flush=True)


async def handle_game_stop(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    game = await get_active_game(chat_id)

    if not game:
        await callback_query.answer("Игра уже завершена.", show_alert=True)
        return

    game_id = game[0]
    game_status = game[2]
    await set_game_status(game_id, "finished")

    try:
        if game_status == "lobby":
            await callback_query.message.edit_text("🛑 Игра остановлена.")
        else:
            players = await get_game_players(game_id)
            await callback_query.message.edit_text(
                "🛑 Игра остановлена.\n" + role_reveal_text(players),
                parse_mode="HTML",
            )
    except Exception:
        pass

    await callback_query.answer("Игра остановлена.")


class BanCheckMiddleware(BaseMiddleware):
    """
    Блокирует обработку сообщений и нажатий кнопок от забаненных
    пользователей — просто тихо отменяет дальнейшую обработку апдейта.
    """

    async def on_process_message(self, message: types.Message, data: dict):
        if await is_user_banned(message.from_user.id):
            raise CancelHandler()

    async def on_process_callback_query(self, callback_query: types.CallbackQuery, data: dict):
        if await is_user_banned(callback_query.from_user.id):
            raise CancelHandler()


class BusinessDiagnosticMiddleware(BaseMiddleware):
    """
    ЭТАП 1 поддержки Telegram Business Mode: только логирует, если в
    сыром апдейте нашлось что-то похожее на business_connection /
    business_message — чтобы по логам Render понять, доходят ли такие
    апдейты вообще через aiogram==2.15 (она вышла раньше этой фичи
    Telegram и не имеет для неё типизированной поддержки).

    Ничего не блокирует и не меняет в обычной обработке апдейтов —
    полностью безопасно для всего остального бота.
    """

    async def on_pre_process_update(self, update: types.Update, data: dict):
        inspect_update_for_business_fields(update)


async def _resolve_target_telegram_id(message: types.Message):
    """
    Определяет telegram_id пользователя-цели для /ban и /unban:
    - если команда отправлена ответом на чьё-то сообщение — берёт автора;
    - иначе разбирает аргумент команды: @username или числовой telegram_id.
    """
    if message.reply_to_message and message.reply_to_message.from_user:
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
            "Использование: <code>/ban telegram_id</code> или <code>/ban @username</code>, "
            "либо отправь /ban ответом на сообщение нужного пользователя.",
            parse_mode="HTML",
        )
        return

    await set_user_banned(target_id, True)
    await message.answer(f"🚫 Пользователь <code>{target_id}</code> забанен.", parse_mode="HTML")


async def cmd_unban(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    target_id = await _resolve_target_telegram_id(message)
    if not target_id:
        await message.answer(
            "Использование: <code>/unban telegram_id</code> или <code>/unban @username</code>, "
            "либо отправь /unban ответом на сообщение нужного пользователя.",
            parse_mode="HTML",
        )
        return

    await set_user_banned(target_id, False)
    await message.answer(f"✅ Пользователь <code>{target_id}</code> разбанен.", parse_mode="HTML")


async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    photo_file_id = None
    text = None
    entities = None  # список types.MessageEntity, включая custom_emoji, если они есть в исходнике

    if message.reply_to_message:
        # Рассылаем то сообщение, на которое ответили командой /broadcast —
        # так можно разослать и фото с подписью, и обычный текст.
        src = message.reply_to_message
        if src.photo:
            photo_file_id = src.photo[-1].file_id
            text = src.caption or ""
            entities = src.caption_entities or None
        else:
            text = src.text or src.caption or ""
            entities = src.entities or src.caption_entities or None
    else:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Использование: <code>/broadcast текст</code>, либо отправь /broadcast "
                "ответом на сообщение (текст или фото с подписью), которое нужно разослать.",
                parse_mode="HTML",
            )
            return
        text = parts[1]
        # Если в самой команде /broadcast <текст> есть custom emoji, entities у этого
        # сообщения тоже есть, но со сдвигом на длину "/broadcast " — пересчитываем offset.
        if message.entities:
            prefix_len = len(message.text) - len(text)
            adjusted = []
            for ent in message.entities:
                if ent.offset + ent.length <= prefix_len:
                    continue  # энтити целиком внутри "/broadcast ", не относится к тексту рассылки
                new_offset = ent.offset - prefix_len
                if new_offset < 0:
                    # энтити частично перекрывает границу — обрезаем по границе,
                    # чтобы не сломать смещения остальных символов
                    continue
                new_ent = ent.copy(deep=True) if hasattr(ent, "copy") else ent
                new_ent = types.MessageEntity(
                    type=ent.type,
                    offset=new_offset,
                    length=ent.length,
                    url=ent.url,
                    user=ent.user,
                    language=ent.language,
                    custom_emoji_id=getattr(ent, "custom_emoji_id", None),
                )
                adjusted.append(new_ent)
            entities = adjusted or None

    telegram_ids = await get_all_telegram_ids()
    status_message = await message.answer(
        f"📤 Рассылка начата, получателей: {len(telegram_ids)}..."
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
        await asyncio.sleep(0.05)  # пауза, чтобы не упереться в лимиты Telegram

    try:
        await status_message.edit_text(
            f"✅ Рассылка завершена. Отправлено: {sent}, не доставлено: {failed}."
        )
    except Exception:
        pass


def register_handlers(dp: Dispatcher):
    dp.middleware.setup(BanCheckMiddleware())
    dp.middleware.setup(BusinessDiagnosticMiddleware())

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
        handle_game_night_action,
        lambda c: c.data and c.data.startswith("game_night:"),
    )
    dp.register_callback_query_handler(
        handle_game_vote_action,
        lambda c: c.data and c.data.startswith("game_vote:"),
    )
    dp.register_message_handler(
        handle_new_chat_members,
        content_types=types.ContentTypes.NEW_CHAT_MEMBERS,
    )
    dp.register_message_handler(
        handle_message,
        content_types=types.ContentTypes.TEXT,
    )
