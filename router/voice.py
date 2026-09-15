"""
Голосовые сообщения Kasper AI.

Поток:
1. Telegram voice → скачать .ogg
2. Groq Whisper → текст
3. AI → ответ
4. Silero TTS → голос
5. Telegram → voice

Silero работает локально и бесплатно.
"""

import os
import io
import wave
import asyncio
import logging
import subprocess
import threading

import aiohttp
import numpy as np
import imageio_ffmpeg

from router.ai_router import get_provider_order

log = logging.getLogger(__name__)

GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3-turbo"

SPEAKER = "aidar"
SAMPLE_RATE = 24000
TTS_TIMEOUT = 60

_model = None
_model_error = None
_model_lock = threading.Lock()
_tts_lock = asyncio.Lock()


def _load_model():
    """Загружает Silero один раз."""
    global _model
    global _model_error

    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        if _model_error is not None:
            raise RuntimeError(_model_error)

        try:
            import torch

            cpu_count = os.cpu_count() or 1

            try:
                torch.set_num_threads(min(4, cpu_count))
            except Exception:
                pass

            try:
                torch.set_num_interop_threads(1)
            except Exception:
                pass

            log.info("[voice] Silero: начинаю загрузку модели...")

            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language="ru",
                speaker="v4_ru",
                trust_repo=True,
            )

            model.to(torch.device("cpu"))

            # НЕ вызываем model.eval().
            # TTSModelMultiAcc_v3 не является обычным nn.Module
            # и у него нет метода eval().

            _model = model

            log.info("[voice] Silero: модель успешно загружена")

            return _model

        except Exception as exc:
            _model_error = f"Silero model error: {exc}"

            log.exception(
                "[voice] %s",
                _model_error,
            )

            raise


def _warmup_model():
    """Загружает модель сразу после старта бота."""
    try:
        _load_model()

    except Exception:
        log.exception(
            "[voice] Silero warmup failed"
        )


# Загружаем Silero сразу после импорта модуля.
threading.Thread(
    target=_warmup_model,
    name="silero-warmup",
    daemon=True,
).start()


async def download_voice(
    bot,
    file_id: str,
) -> bytes:
    """Скачивает голосовое сообщение из Telegram."""

    file = await bot.get_file(file_id)
    file_path = file.file_path

    token = bot._token

    url = (
        f"https://api.telegram.org/"
        f"file/bot{token}/{file_path}"
    )

    timeout = aiohttp.ClientTimeout(
        total=30
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


async def transcribe_voice(
    ogg_bytes: bytes,
) -> str:
    """Распознаёт голос через Groq Whisper."""

    api_key = os.getenv(
        "GROQ_API_KEY",
        "",
    )

    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY не задан — STT невозможен"
        )

    headers = {
        "Authorization": f"Bearer {api_key}"
    }

    data = aiohttp.FormData()

    data.add_field(
        "file",
        ogg_bytes,
        filename="voice.ogg",
        content_type="audio/ogg",
    )

    data.add_field(
        "model",
        WHISPER_MODEL,
    )

    data.add_field(
        "language",
        "ru",
    )

    data.add_field(
        "response_format",
        "json",
    )

    timeout = aiohttp.ClientTimeout(
        total=30
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.post(
            GROQ_WHISPER_URL,
            headers=headers,
            data=data,
        ) as resp:

            if resp.status != 200:
                body = await resp.text()

                raise RuntimeError(
                    f"Groq Whisper error "
                    f"{resp.status}: {body}"
                )

            result = await resp.json()

            return result.get(
                "text",
                "",
            ).strip()


def _wav_to_mp3(
    wav_bytes: bytes,
) -> bytes:
    """Конвертирует WAV в MP3."""

    ffmpeg_path = (
        imageio_ffmpeg.get_ffmpeg_exe()
    )

    proc = subprocess.run(
        [
            ffmpeg_path,
            "-loglevel",
            "error",
            "-y",
            "-i",
            "pipe:0",
            "-f",
            "mp3",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "pipe:1",
        ],
        input=wav_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg error: "
            + proc.stderr.decode(
                errors="ignore"
            )
        )

    return proc.stdout


def _synthesize_sync(
    text: str,
    speaker: str,
) -> bytes:
    """Синтезирует речь."""

    model = _load_model()

    audio = model.apply_tts(
        text=text,
        speaker=speaker,
        sample_rate=SAMPLE_RATE,
    )

    pcm16 = (
        audio.detach()
        .cpu()
        .numpy()
        * 32767
    ).clip(
        -32768,
        32767,
    ).astype(np.int16)

    buffer = io.BytesIO()

    with wave.open(
        buffer,
        "wb",
    ) as wf:

        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)

        wf.writeframes(
            pcm16.tobytes()
        )

    return _wav_to_mp3(
        buffer.getvalue()
    )


async def synthesize_speech(
    text: str,
    speaker: str = SPEAKER,
) -> bytes:
    """Синтезирует голос."""

    text = (
        text or ""
    ).strip()

    if not text:
        raise ValueError(
            "TTS text is empty"
        )

    async with _tts_lock:

        loop = (
            asyncio.get_running_loop()
        )

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    _synthesize_sync,
                    text,
                    speaker,
                ),
                timeout=TTS_TIMEOUT,
            )

        except asyncio.TimeoutError:

            log.error(
                "[voice] Silero TTS timeout "
                "after %s seconds",
                TTS_TIMEOUT,
            )

            raise RuntimeError(
                "Silero TTS timeout"
            )


async def handle_voice_message(
    bot,
    message,
    ai_ask_fn,
    get_history_fn,
    save_message_fn,
    user_id,
    chat_id=None,
):
    """Полный цикл обработки голосового."""

    # ---------------------------------------------------------
    # 1. Скачать голосовое
    # ---------------------------------------------------------

    try:

        ogg_bytes = await download_voice(
            bot,
            message.voice.file_id,
        )

    except Exception as exc:

        log.error(
            "[voice] download error: %s",
            exc,
        )

        await message.answer(
            "⚠️ Не смог скачать голосовое. "
            "Попробуй ещё раз."
        )

        return

    # ---------------------------------------------------------
    # 2. Распознать
    # ---------------------------------------------------------

    try:

        recognized_text = (
            await transcribe_voice(
                ogg_bytes
            )
        )

    except Exception as exc:

        log.error(
            "[voice] STT error: %s",
            exc,
        )

        await message.answer(
            "🎙 Не смог распознать голосовое. "
            "Попробуй ещё раз."
        )

        return

    if not recognized_text:

        await message.answer(
            "🎙 Ничего не распознал. "
            "Говори чуть громче и чётче."
        )

        return

    log.info(
        "[voice] recognized: %r",
        recognized_text,
    )

    await message.answer(
        f"🎙 *Распознал:* {recognized_text}",
        parse_mode="Markdown",
    )

    # ---------------------------------------------------------
    # 3. История
    # ---------------------------------------------------------

    await save_message_fn(
        user_id,
        "user",
        recognized_text,
        chat_id=chat_id,
    )

    history = await get_history_fn(
        user_id,
        limit=5,
        chat_id=chat_id,
    )

    system_prompt = (
        "Ты — Kasper AI, ИИ-помощник в Telegram "
        "с дерзким, злым-но-своим характером, "
        "созданный разработчиками Kasper AI. "
        "Если спросят, кто тебя создал — отвечай, "
        "что тебя создали разработчики Kasper AI. "
        "Дерзкий, саркастичный стиль — но по делу "
        "и помогаешь. "
        "Отвечай коротко и чётко — ответ будет "
        "озвучен голосом, поэтому без markdown, "
        "без звёздочек, без списков с тире. "
        "Пиши как будто говоришь вслух."
    )

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        }
    ]

    for row in history:

        if isinstance(row, dict):

            role = row.get("role")
            content = row.get("content")

        else:

            role = row[0]
            content = row[1]

        if role and content:

            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    # Не дублируем последнее сообщение.
    if (
        not history
        or history[-1][0] != "user"
        or history[-1][1] != recognized_text
    ):

        messages.append(
            {
                "role": "user",
                "content": recognized_text,
            }
        )

    # ---------------------------------------------------------
    # 4. AI
    # ---------------------------------------------------------

    ai_response = None

    timeout = aiohttp.ClientTimeout(
        total=60
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        for provider in get_provider_order():

            try:

                ai_response = await ai_ask_fn(
                    session,
                    provider,
                    messages,
                )

                if ai_response:
                    break

            except Exception as exc:

                log.error(
                    "[voice] AI provider %s "
                    "error: %s",
                    provider,
                    exc,
                )

    if not ai_response:

        await message.answer(
            "⚠️ AI не ответил. "
            "Попробуй ещё раз."
        )

        return

    await save_message_fn(
        user_id,
        "assistant",
        ai_response,
        chat_id=chat_id,
    )

    # ---------------------------------------------------------
    # 5. Silero TTS
    # ---------------------------------------------------------

    try:

        mp3_bytes = (
            await synthesize_speech(
                ai_response
            )
        )

    except Exception as exc:

        log.error(
            "[voice] TTS error: %s",
            exc,
        )

        # Если голос не получился,
        # всё равно отдаём ответ текстом.
        await message.answer(
            ai_response
        )

        return

    # ---------------------------------------------------------
    # 6. Отправить голос
    # ---------------------------------------------------------

    try:

        from aiogram.types import InputFile

        voice_file = InputFile(
            io.BytesIO(mp3_bytes),
            filename="kasper_response.mp3",
        )

        await message.answer_voice(
            voice_file
        )

    except Exception as exc:

        log.error(
            "[voice] send voice error: %s",
            exc,
        )

        await message.answer(
            ai_response
        )
