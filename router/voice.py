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

DOWNLOAD_TIMEOUT = 30
STT_TIMEOUT = 30
AI_TIMEOUT = 60
TTS_TIMEOUT = 60
FFMPEG_TIMEOUT = 30

_model = None
_model_error = None
_model_lock = threading.Lock()
_tts_lock = asyncio.Lock()


# =========================================================
# SILERO
# =========================================================

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
                torch.set_num_threads(
                    min(4, cpu_count)
                )
            except Exception:
                pass

            try:
                torch.set_num_interop_threads(1)
            except Exception:
                pass

            log.info(
                "[voice] Silero: начинаю загрузку модели..."
            )

            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language="ru",
                speaker="v4_ru",
                trust_repo=True,
            )

            model.to(
                torch.device("cpu")
            )

            # ВАЖНО:
            # model.eval() здесь НЕ вызываем.
            # TTSModelMultiAcc_v3 не имеет eval().

            _model = model

            log.info(
                "[voice] Silero: модель успешно загружена"
            )

            return _model

        except Exception as exc:

            _model_error = (
                f"Silero model error: {exc}"
            )

            log.exception(
                "[voice] %s",
                _model_error,
            )

            raise


def _warmup_model():
    """Загружает Silero сразу после старта."""

    try:

        _load_model()

    except Exception:

        log.exception(
            "[voice] Silero warmup failed"
        )


threading.Thread(
    target=_warmup_model,
    name="silero-warmup",
    daemon=True,
).start()


# =========================================================
# TELEGRAM DOWNLOAD
# =========================================================

async def download_voice(
    bot,
    file_id: str,
) -> bytes:
    """Скачивает голосовое сообщение из Telegram."""

    log.info(
        "[voice] downloading Telegram voice..."
    )

    file = await asyncio.wait_for(
        bot.get_file(file_id),
        timeout=DOWNLOAD_TIMEOUT,
    )

    file_path = file.file_path

    token = bot._token

    url = (
        "https://api.telegram.org/"
        f"file/bot{token}/{file_path}"
    )

    timeout = aiohttp.ClientTimeout(
        total=DOWNLOAD_TIMEOUT
    )

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.get(url) as resp:

            resp.raise_for_status()

            data = await resp.read()

    log.info(
        "[voice] downloaded voice: %s bytes",
        len(data),
    )

    return data


# =========================================================
# GROQ WHISPER
# =========================================================

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
        total=STT_TIMEOUT
    )

    log.info(
        "[voice] sending audio to Groq Whisper..."
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

    text = result.get(
        "text",
        "",
    ).strip()

    return text


# =========================================================
# WAV → MP3
# =========================================================

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
        timeout=FFMPEG_TIMEOUT,
    )

    if proc.returncode != 0:

        error_text = proc.stderr.decode(
            errors="ignore"
        )

        raise RuntimeError(
            f"ffmpeg error: {error_text}"
        )

    return proc.stdout


# =========================================================
# SILERO SYNTHESIS
# =========================================================

def _synthesize_sync(
    text: str,
    speaker: str,
) -> bytes:
    """Синтезирует речь в отдельном executor."""

    model = _load_model()

    log.info(
        "[voice] TTS: начинаю синтез, %s chars",
        len(text),
    )

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

    mp3 = _wav_to_mp3(
        buffer.getvalue()
    )

    log.info(
        "[voice] TTS: готово, %s bytes",
        len(mp3),
    )

    return mp3


async def synthesize_speech(
    text: str,
    speaker: str = SPEAKER,
) -> bytes:
    """Асинхронный TTS."""

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

            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    _synthesize_sync,
                    text,
                    speaker,
                ),
                timeout=TTS_TIMEOUT,
            )

            return result

        except asyncio.TimeoutError:

            log.error(
                "[voice] Silero TTS timeout "
                "after %s seconds",
                TTS_TIMEOUT,
            )

            raise RuntimeError(
                "Silero TTS timeout"
            )


# =========================================================
# HISTORY HELPERS
# =========================================================

def _get_history_row(
    row,
):
    """
    Унифицированно получает role/content
    как из tuple, так и из dict.
    """

    if isinstance(row, dict):

        return (
            row.get("role"),
            row.get("content"),
        )

    if isinstance(row, (tuple, list)):

        if len(row) >= 2:

            return (
                row[0],
                row[1],
            )

    return None, None


# =========================================================
# MAIN VOICE HANDLER
# =========================================================

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

    # -----------------------------------------------------
    # 1. DOWNLOAD
    # -----------------------------------------------------

    log.info(
        "[voice] step 1/6: download"
    )

    try:

        ogg_bytes = await download_voice(
            bot,
            message.voice.file_id,
        )

    except Exception as exc:

        log.exception(
            "[voice] download error: %s",
            exc,
        )

        await message.answer(
            "⚠️ Не смог скачать голосовое. "
            "Попробуй ещё раз."
        )

        return

    # -----------------------------------------------------
    # 2. STT
    # -----------------------------------------------------

    log.info(
        "[voice] step 2/6: speech-to-text"
    )

    try:

        recognized_text = (
            await transcribe_voice(
                ogg_bytes
            )
        )

    except Exception as exc:

        log.exception(
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

    # -----------------------------------------------------
    # 3. HISTORY
    # -----------------------------------------------------

    log.info(
        "[voice] step 3/6: history"
    )

    try:

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

    except Exception as exc:

        log.exception(
            "[voice] history error: %s",
            exc,
        )

        await message.answer(
            "⚠️ Ошибка истории диалога."
        )

        return

    if history is None:
        history = []

    # -----------------------------------------------------
    # SYSTEM PROMPT
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # HISTORY → MESSAGES
    # -----------------------------------------------------

    last_role = None
    last_content = None

    for row in history:

        role, content = _get_history_row(row)

        if role and content:

            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

            last_role = role
            last_content = content

    # -----------------------------------------------------
    # НЕ ДУБЛИРУЕМ ПОСЛЕДНЕЕ USER MESSAGE
    # -----------------------------------------------------

    if not (
        last_role == "user"
        and last_content == recognized_text
    ):

        messages.append(
            {
                "role": "user",
                "content": recognized_text,
            }
        )

    log.info(
        "[voice] history ready: %s messages",
        len(messages),
    )

    # -----------------------------------------------------
    # 4. AI
    # -----------------------------------------------------

    log.info(
        "[voice] step 4/6: AI"
    )

    ai_response = None

    try:

        timeout = aiohttp.ClientTimeout(
            total=AI_TIMEOUT
        )

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            for provider in get_provider_order():

                log.info(
                    "[voice] trying AI provider: %s",
                    provider,
                )

                try:

                    ai_response = await asyncio.wait_for(
                        ai_ask_fn(
                            session,
                            provider,
                            messages,
                        ),
                        timeout=AI_TIMEOUT,
                    )

                    if ai_response:

                        log.info(
                            "[voice] AI response "
                            "received from %s",
                            provider,
                        )

                        break

                except Exception as exc:

                    log.exception(
                        "[voice] AI provider %s "
                        "error: %s",
                        provider,
                        exc,
                    )

    except Exception as exc:

        log.exception(
            "[voice] AI fatal error: %s",
            exc,
        )

    if not ai_response:

        await message.answer(
            "⚠️ AI не ответил. "
            "Попробуй ещё раз."
        )

        return

    ai_response = str(
        ai_response
    ).strip()

    # -----------------------------------------------------
    # SAVE AI RESPONSE
    # -----------------------------------------------------

    try:

        await save_message_fn(
            user_id,
            "assistant",
            ai_response,
            chat_id=chat_id,
        )

    except Exception as exc:

        log.exception(
            "[voice] save AI response error: %s",
            exc,
        )

    # -----------------------------------------------------
    # 5. TTS
    # -----------------------------------------------------

    log.info(
        "[voice] step 5/6: TTS"
    )

    try:

        mp3_bytes = (
            await synthesize_speech(
                ai_response
            )
        )

    except Exception as exc:

        log.exception(
            "[voice] TTS error: %s",
            exc,
        )

        # AI ответ уже есть —
        # отдаём его текстом.
        await message.answer(
            ai_response
        )

        return

    # -----------------------------------------------------
    # 6. SEND VOICE
    # -----------------------------------------------------

    log.info(
        "[voice] step 6/6: sending voice"
    )

    try:

        from aiogram.types import InputFile

        voice_file = InputFile(
            io.BytesIO(mp3_bytes),
            filename="kasper_response.mp3",
        )

        await message.answer_voice(
            voice_file
        )

        log.info(
            "[voice] voice response sent successfully"
        )

    except Exception as exc:

        log.exception(
            "[voice] send voice error: %s",
            exc,
        )

        await message.answer(
            ai_response
        )
