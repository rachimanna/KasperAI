"""
Голосовые сообщения для Kasper AI.

Поток:
  1. Telegram voice → скачать .ogg
  2. Groq Whisper API → распознать текст (STT)
  3. router/ai_router.ask() → ответ Каспера
  4. Silero TTS → синтез речи .mp3 (TTS). Модель работает ЛОКАЛЬНО,
     внутри процесса бота — никуда наружу не стучится (в отличие от
     edge-tts/gTTS), поэтому её не может заблокировать сторонний сервис.
     Бесплатно, без API-ключа.
  5. Отправить voice note в чат

Важно: при первом запуске Silero скачивает саму модель (~50 МБ) с
GitHub — это происходит один раз при первом голосовом сообщении после
деплоя/рестарта и может занять до минуты. Дальше модель уже в памяти
процесса и ответы быстрые.
"""

import os
import io
import wave
import asyncio
import logging
import subprocess

import aiohttp
import numpy as np
import imageio_ffmpeg

from router.ai_router import get_provider_order

log = logging.getLogger(__name__)

GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3-turbo"


async def download_voice(bot, file_id: str) -> bytes:
    """Скачивает голосовое сообщение из Telegram и возвращает байты."""
    file = await bot.get_file(file_id)
    file_path = file.file_path

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


# Голос по умолчанию — мужской, уверенный, под дерзкий характер Каспера.
# Другие варианты русских голосов Silero v4 (модель "v4_ru"):
#   "aidar"   — мужской, энергичный (используется сейчас)
#   "eugene"  — мужской, более низкий и спокойный
#   "baya"    — женский
#   "kseniya" — женский, мягкий
#   "xenia"   — женский
SPEAKER = "aidar"
SAMPLE_RATE = 48000

_model = None  # модель Silero, грузится один раз лениво при первом сообщении


def _load_model():
    global _model
    if _model is None:
        import torch
        torch.set_num_threads(4)
        log.info("[voice] загружаю модель Silero TTS (один раз)...")
        model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language="ru",
            speaker="v4_ru",
            trust_repo=True,
        )
        model.to(torch.device("cpu"))
        _model = model
        log.info("[voice] модель Silero TTS загружена")
    return _model


def _wav_to_mp3(wav_bytes: bytes) -> bytes:
    """Конвертирует WAV в MP3 через ffmpeg (уже есть в зависимостях как imageio-ffmpeg)."""
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg_path, "-y", "-i", "pipe:0", "-f", "mp3", "pipe:1"],
        input=wav_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {proc.stderr.decode(errors='ignore')}")
    return proc.stdout


def _synthesize_sync(text: str, speaker: str) -> bytes:
    model = _load_model()
    audio = model.apply_tts(text=text, speaker=speaker, sample_rate=SAMPLE_RATE)
    pcm16 = (audio.numpy() * 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())

    return _wav_to_mp3(buf.getvalue())


async def synthesize_speech(text: str, speaker: str = SPEAKER) -> bytes:
    """
    Синтезирует речь через локальную модель Silero TTS.
    Бесплатно, без API-ключа, ничего не отправляет наружу — работает,
    даже если внешние TTS-сервисы блокируют запросы с сервера.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _synthesize_sync, text, speaker)


async def handle_voice_message(bot, message, ai_ask_fn, get_history_fn, save_message_fn, user_id, chat_id=None):
    """
    Полный цикл обработки голосового сообщения.
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

    await message.answer(f"🎙 *Распознал:* {recognized_text}", parse_mode="Markdown")

    # 3. Сохраняем в историю как обычное сообщение
    await save_message_fn(user_id, "user", recognized_text, chat_id=chat_id)

    history = await get_history_fn(user_id, limit=5, chat_id=chat_id)

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

    # ВАЖНО: get_history() возвращает кортежи (role, content) из sqlite,
    # а не словари. Раньше здесь стояло h["role"] — это падало с TypeError
    # на каждом голосовом сообщении. Поддерживаем оба варианта на случай,
    # если row_factory когда-нибудь поменяют на dict/sqlite3.Row.
    for row in history:
        if isinstance(row, dict):
            role, content = row.get("role"), row.get("content")
        else:
            role, content = row[0], row[1]
        if role and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": recognized_text})

    # 4. Получаем ответ AI
    ai_response = None
    async with aiohttp.ClientSession() as session:
        # Тот же порядок провайдеров, что и в текстовом чате
        # (router.ai_router.get_provider_order), иначе голос и текст
        # ходят к разным моделям.
        for provider in get_provider_order():
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
        mp3_bytes = await synthesize_speech(ai_response)
    except Exception as e:
        log.error(f"[voice] TTS error: {e}")
        await message.answer(ai_response)
        return

    # 6. Отправляем голосовое сообщение
    try:
        from aiogram.types import InputFile
        voice_file = InputFile(io.BytesIO(mp3_bytes), filename="kasper_response.mp3")
        await message.answer_voice(voice_file)
    except Exception as e:
        log.error(f"[voice] send voice error: {e}")
        await message.answer(ai_response)


# ---------------------------------------------------------------------------
# КОНЕЦ ФАЙЛА router/voice.py
# Ниже кода нет. Эти строки — комментарии-подушка: если при копировании
# в GitHub с телефона обрежется самый хвост файла, пострадают только они,
# а рабочий код выше останется целым.
# ---------------------------------------------------------------------------
