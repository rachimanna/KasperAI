"""
Скачивание музыки через yt-dlp.

ПОЧЕМУ БЕЗ КОНВЕРТАЦИИ В MP3:
Раньше здесь стояло "-x --audio-format mp3". Извлечение и перекодирование
аудио yt-dlp делает НЕ сам — он вызывает ffmpeg. На Render (нативный
Python-рантайм) ffmpeg не установлен и поставить его без root нельзя,
поэтому шаг постпроцессинга падал, функция возвращала None и пользователь
всегда получал "⚠️ Не удалось скачать музыку" — при том, что сам файл
скачивался нормально.

Теперь качаем уже готовую аудиодорожку как есть (обычно m4a/AAC или webm/opus)
и отправляем в Telegram без конвертации: Telegram проигрывает m4a и opus
штатно. ffmpeg не нужен вообще. Если на площадке ffmpeg всё-таки есть,
качество и размер от этого не меняются — просто пропускаем лишний шаг.
"""

import os
import re
import glob
import asyncio

MUSIC_DIR = "data/music_cache"

MAX_DURATION = 600  # 10 минут, защита от скачивания огромных файлов
MAX_CACHE_FILES = 20
AUDIO_EXTS = ("m4a", "mp3", "webm", "opus", "ogg", "mp4a")


def _clean_old_files():
    """Оставляет в кэше не больше MAX_CACHE_FILES самых свежих файлов."""
    files = []
    for ext in AUDIO_EXTS:
        files.extend(glob.glob(os.path.join(MUSIC_DIR, f"*.{ext}")))

    if len(files) > MAX_CACHE_FILES:
        files.sort(key=os.path.getmtime)
        for f in files[:-MAX_CACHE_FILES]:
            try:
                os.remove(f)
            except Exception:
                pass


def _find_downloaded(safe_name):
    """Ищет скачанный файл по имени без расширения."""
    for ext in AUDIO_EXTS:
        path = os.path.join(MUSIC_DIR, f"{safe_name}.{ext}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path

    candidates = []
    for ext in AUDIO_EXTS:
        candidates.extend(glob.glob(os.path.join(MUSIC_DIR, f"{safe_name}*.{ext}")))

    candidates = [c for c in candidates if os.path.getsize(c) > 0]
    if not candidates:
        return None

    candidates.sort(key=os.path.getmtime)
    return candidates[-1]


async def _run_ytdlp(cmd, timeout=90):
    """Запускает yt-dlp. Возвращает True при успешном завершении."""
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )

        if process.returncode != 0:
            print(f"[music] yt-dlp exit {process.returncode}: "
                  f"{stderr.decode(errors='ignore')[:300]}", flush=True)
            return False

        return True

    except asyncio.TimeoutError:
        print("[music] yt-dlp TIMEOUT", flush=True)
        return False
    except FileNotFoundError:
        print("[music] yt-dlp не установлен в окружении", flush=True)
        return False
    except Exception as e:
        print(f"[music] ERROR: {e}", flush=True)
        return False


async def download_music(query):
    """
    Ищет и скачивает трек по названию через yt-dlp.
    Возвращает путь к аудиофайлу (m4a/webm/mp3) или None при ошибке.
    """
    os.makedirs(MUSIC_DIR, exist_ok=True)
    _clean_old_files()

    safe_name = re.sub(r'[^\w\s-]', '', query).strip()[:60] or "track"
    output_template = os.path.join(MUSIC_DIR, f"{safe_name}.%(ext)s")

    cmd = [
        "yt-dlp",
        f"ytsearch1:{query} official audio",
        # Готовая аудиодорожка без постпроцессинга: сначала пробуем m4a
        # (лучше всего понимается Telegram), иначе любое лучшее аудио.
        "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "--max-filesize", "30m",
        "--match-filter", f"duration < {MAX_DURATION}",
        "-o", output_template,
        "--no-playlist",
        "--playlist-items", "1",
        "--no-warnings",
        "--quiet",
    ]

    if await _run_ytdlp(cmd):
        path = _find_downloaded(safe_name)
        if path:
            return path

    print("[music] прямое скачивание не дало файла", flush=True)
    return None
