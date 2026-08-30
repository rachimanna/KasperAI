import asyncio
import re

from aiogram import Dispatcher, types

from database.db import get_or_create_user, save_message, get_history, check_and_increment_limit, get_limit_status
from router.ai_router import ask, ask_provider, get_provider_order, classify_request, generate_website_html
from router.web_search import tavily_search, format_search_results
from router.music import download_music
import aiohttp

TRIGGER_PATTERN = re.compile(r"каспер|kasper", re.IGNORECASE)

WELCOME_PROMPT = (
    "Ты — Kasper AI, ИИ-помощник в Telegram. Тебя только что добавили "
    "в групповой чат. Поздоровайся и коротко (2-3 предложения) представься: "
    "кто ты, и что участники группы могут к тебе обращаться по имени "
    "'Каспер' или 'Kasper', чтобы получить ответ. Пиши дружелюбно, "
    "без лишнего формализма."
)


async def cmd_start(message: types.Message):
    await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    await message.answer(
        "Здравствуйте, я ИИ-помощник. Я был создан разработчиками Kasper AI. "
        "Задавайте вопрос, буду рад помочь! 🙂"
    )


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


async def _animate_thinking(thinking_message: types.Message):
    dots_cycle = [".", "..", "..."]
    i = 0
    try:
        while True:
            dots = dots_cycle[i % len(dots_cycle)]
            try:
                await thinking_message.edit_text(f"`thinking{dots}`", parse_mode="Markdown")
            except Exception:
                pass
            i += 1
            await asyncio.sleep(0.6)
    except asyncio.CancelledError:
        pass


async def handle_message(message: types.Message):
    is_group = message.chat.type in ("group", "supergroup")
    text = (message.text or "").strip()
    if not text:
        return

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
        "Ты — Kasper AI, дружелюбный ИИ-помощник в Telegram, созданный "
        "разработчиками Kasper AI. Если спросят, кто тебя создал — "
        "отвечай, что тебя создали разработчики Kasper AI, без других "
        "подробностей. Если спросят, на какой модели или технологии ты "
        "работаешь, какая ты версия GPT/Gemini/другой ИИ — не называй "
        "конкретных технологий или компаний-провайдеров, вежливо уклонись "
        "и скажи, что это внутренняя технология Kasper AI. "
        "У тебя дружелюбный характер, как у приятного собеседника в переписке. "
        "Общайся живо и с юмором, но без мата и оскорблений, даже если "
        "пользователь груб или провоцирует — сохраняй спокойствие и вежливость, "
        "можно с лёгкой иронией, но без ответной агрессии. "
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
        "копирования. Обычный текст ответа пиши без разметки."
    )

    messages = [
        {
            "role": "system",
            "content": KASPER_SYSTEM_PROMPT,
        }
    ]
    for role, content in history:
        messages.append(
            {
                "role": role,
                "content": content,
            }
        )

    search_context = ""
    classification = None
    try:
        async with aiohttp.ClientSession() as _classify_session:
            for _provider_try in get_provider_order():
                try:
                    classification = await classify_request(_classify_session, _provider_try, text)
                    break
                except Exception as _e_classify:
                    print(f"[classify_request] {_provider_try} ERROR: {_e_classify}", flush=True)
                    continue
    except Exception as e:
        print(f"[Kasper] Classify ERROR: {e}", flush=True)

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

    if not messages or messages[-1]["content"] != text:
        user_content = text
        if search_context:
            user_content = f"{search_context}\n\nВопрос пользователя: {text}"

        messages.append(
            {
                "role": "user",
                "content": user_content,
            }
        )

    thinking_message = None
    animation_task = None

    if not is_group:
        thinking_message = await message.answer("`thinking.`", parse_mode="Markdown")
        animation_task = asyncio.create_task(_animate_thinking(thinking_message))

    try:
        print(
            f"[Kasper] Sending {len(messages)} messages to router...",
            flush=True,
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

        if is_group:
            try:
                await message.reply(answer, parse_mode="Markdown")
            except Exception:
                await message.reply(answer)
        else:
            animation_task.cancel()
            try:
                await animation_task
            except asyncio.CancelledError:
                pass

            try:
                await thinking_message.edit_text(answer, parse_mode="Markdown")
            except Exception:
                try:
                    await thinking_message.edit_text(answer)
                except Exception:
                    await message.answer(answer)

        if _send_as_file:
            doc = types.InputFile(_send_as_file)
            await message.answer_document(doc)

    except Exception as e:
        if animation_task:
            animation_task.cancel()
            try:
                await animation_task
            except asyncio.CancelledError:
                pass

        print(
            f"[Kasper] AI ERROR: {e}",
            flush=True,
        )

        error_text = f"⚠️ Ошибка AI: {e}"

        if is_group:
            await message.reply(error_text)
        elif thinking_message:
            try:
                await thinking_message.edit_text(error_text)
            except Exception:
                await message.answer(error_text)
        else:
            await message.answer(error_text)


def register_handlers(dp: Dispatcher):
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
        handle_new_chat_members,
        content_types=types.ContentTypes.NEW_CHAT_MEMBERS,
    )
    dp.register_message_handler(
        handle_message,
        content_types=types.ContentTypes.TEXT,
    )
