"""
Голосовые сообщения для Kasper AI.

Поток:
  1. Telegram voice → скачать .ogg
  2. Groq Whisper API → распознать текст (STT)
  3. router/ai_router.ask() → ответ Каспера
  4. gTTS → синтез речи .mp3 (TTS)
  5. Отправить voice note в чат

Зависимости (добавить в requirements.txt):
  gTTS==2.5.3
  pydub==0.25.1   # конвертация ogg→mp3 для Whisper, если нужно
"""

import os
import io
import tempfile
import logging

import aiohttp
from gtts import gTTS

log = logging.getLogger(__name__)

GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3-turbo"


async def download_voice(bot, file_id: str) -> bytes:
    """Скачивает голосовое сообщение из Telegram и возвращает байты."""
    file = await bot.get_file(file_id)
    file_path = file.file_path

    # Строим URL для скачивания файла
    token = bot._token  # aiogram 2.x
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


async def transcribe_voice(ogg_bytes: bytes) -> str:
    """
    Отправляет аудио в Groq Whisper и возвращает распознанный текст.
    Groq принимает ogg/opus напрямую — конвертация не нужна.
    """
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY не задан — STT невозможен")

    headers = {"Authorization": f"Bearer {api_key}"}

    data = aiohttp.FormData()
    data.add_field(
        "file",
        ogg_bytes,
        filename="voice.ogg",
        content_type="audio/ogg",
    )
    data.add_field("model", WHISPER_MODEL)
    data.add_field("language", "ru")
    data.add_field("response_format", "json")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            GROQ_WHISPER_URL,
            headers=headers,
            data=data,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Groq Whisper error {resp.status}: {body}")
            result = await resp.json()
            text = result.get("text", "").strip()
            return text


def synthesize_speech(text: str, lang: str = "ru") -> bytes:
    """
    Синтезирует речь через gTTS и возвращает mp3-байты.
    gTTS — бесплатно, без ключей, хорошо говорит по-русски.
    """
    tts = gTTS(text=text, lang=lang, slow=False)
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    buf.seek(0)
    return buf.read()


async def handle_voice_message(bot, message, ai_ask_fn, get_history_fn, save_message_fn, user_id, chat_id=None):
    """
    Полный цикл обработки голосового сообщения.

    Параметры:
        bot           — объект aiogram Bot
        message       — объект aiogram Message
        ai_ask_fn     — функция ask() из ai_router (session, provider, messages)
        get_history_fn — функция get_history() из db
        save_message_fn — функция save_message() из db
        user_id       — ID пользователя в БД
        chat_id       — ID чата (для групп) или None (для личек)
    """
    # 1. Скачиваем голосовое
    voice = message.voice
    try:
        ogg_bytes = await download_voice(bot, voice.file_id)
    except Exception as e:
        log.error(f"[voice] download error: {e}")
        await message.answer("⚠️ Не смог скачать голосовое, попробуй ещё раз.")
        return

    # 2. STT — распознаём текст
    try:
        recognized_text = await transcribe_voice(ogg_bytes)
    except Exception as e:
        log.error(f"[voice] STT error: {e}")
        await message.answer("🎙 Не смог распознать голосовое. Попробуй говорить чётче или напиши текстом.")
        return

    if not recognized_text:
        await message.answer("🎙 Ничего не распознал. Говори чуть громче и чётче.")
        return

    log.info(f"[voice] recognized: {recognized_text!r}")

    # Показываем что распознали (удобно для дебага и пользователю приятно)
    await message.answer(f"🎙 *Распознал:* {recognized_text}", parse_mode="Markdown")

    # 3. Сохраняем в историю как обычное сообщение
    await save_message_fn(user_id, "user", recognized_text, chat_id=chat_id)

    history = await get_history_fn(user_id, limit=5, chat_id=chat_id)

    from config.settings import AI_PROVIDERS

    KASPER_SYSTEM_PROMPT = (
        "Ты — Kasper AI, ИИ-помощник в Telegram с дерзким, злым-но-своим "
        "характером, созданный разработчиками Kasper AI. Если спросят, кто "
        "тебя создал — отвечай, что тебя создали разработчики Kasper AI. "
        "Дерзкий, саркастичный стиль — но по делу и помогаешь. "
        "Отвечай коротко и чётко — ответ будет озвучен голосом, "
        "поэтому без markdown, без звёздочек, без списков с тире. "
        "Пиши как будто говоришь вслух."
    )

    messages = [{"role": "system", "content": KASPER_SYSTEM_PROMPT}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": recognized_text})

    # 4. Получаем ответ AI
    ai_response = None
    async with aiohttp.ClientSession() as session:
        for provider in AI_PROVIDERS:
            try:
                ai_response = await ai_ask_fn(session, provider, messages)
                break
            except Exception as e:
                log.error(f"[voice] AI provider {provider} error: {e}")
                continue

    if not ai_response:
        await message.answer("⚠️ AI не ответил. Попробуй ещё раз.")
        return

    # Сохраняем ответ в историю
    await save_message_fn(user_id, "assistant", ai_response, chat_id=chat_id)

    # 5. TTS — синтезируем речь
    try:
        mp3_bytes = synthesize_speech(ai_response)
    except Exception as e:
        log.error(f"[voice] TTS error: {e}")
        # Если TTS упал — отвечаем текстом
        await message.answer(ai_response)
        return

    # 6. Отправляем голосовое сообщение
    try:
        from aiogram.types import InputFile
        voice_file = InputFile(io.BytesIO(mp3_bytes), filename="kasper_response.mp3")
        await message.answer_voice(voice_file)
    except Exception as e:
        log.error(f"[voice] send voice error: {e}")
        # Fallback — текстом
        await message.answer(ai_response)
