"""
Скачивание музыки через yt-dlp.

ПРОБЛЕМА 1 — YouTube блокирует серверы.
С IP-адресов хостингов (Render, AWS и т.п.) YouTube отвечает
"Sign in to confirm you're not a bot" и ничего не отдаёт без cookies
реального браузера. Проверено: с сервера ytsearch падает всегда, с
домашнего интернета работает. Поэтому основной источник теперь
SoundCloud (scsearch) — он отдаёт треки серверам без авторизации.
YouTube остаётся вторым вариантом: если бот когда-нибудь будет
запущен не на хостинге, он тоже сработает.

ПРОБЛЕМА 2 — на Render нет ffmpeg.
yt-dlp сам не умеет перекодировать звук, он вызывает ffmpeg, а на
Render его нет и поставить через apt без root нельзя. Решение:
пакет imageio-ffmpeg (обычный pip-пакет из requirements.txt) содержит
готовый статический ffmpeg внутри себя. Берём путь к нему и передаём
yt-dlp через --ffmpeg-location.

Если ffmpeg по какой-то причине не найден — не сдаёмся, а качаем
готовую аудиодорожку без перекодирования (Telegram играет m4a и mp3
одинаково).
"""

import os
import re
import glob
import shutil
import asyncio

MUSIC_DIR = "data/music_cache"

MAX_DURATION = 600  # 10 минут, защита от скачивания огромных файлов
MAX_CACHE_FILES = 20
AUDIO_EXTS = ("mp3", "m4a", "webm", "opus", "ogg", "aac")

# Порядок форматов: сначала обычный прогрессивный mp3 (SoundCloud отдаёт
# его как http_mp3_*), потом mp3 через HLS, потом любое лучшее аудио.
FORMAT_SELECTOR = (
    "http_mp3_128/http_mp3_1_0/hls_mp3_0_1/"
    "bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio/best"
)


def _find_ffmpeg():
    """
    Путь к ffmpeg: сначала пробуем pip-пакет imageio-ffmpeg (работает на
    Render), потом системный ffmpeg (работает локально). None — значит
    перекодировать нечем, будем качать файл как есть.
    """
    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.exists(path):
            return path
    except Exception as e:
        print(f"[music] imageio-ffmpeg недоступен: {e}", flush=True)

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    return None


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


async def _run_ytdlp(cmd, timeout=120):
    """Запускает yt-dlp. Возвращает True, если процесс завершился успешно."""
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
            # Печатаем именно хвост stderr — там настоящая причина
            # ("Sign in to confirm you're not a bot", "Unable to extract" и т.п.)
            print(
                f"[music] yt-dlp exit {process.returncode}: "
                f"{stderr.decode(errors='ignore')[-400:]}",
                flush=True,
            )
            return False

        return True

    except asyncio.TimeoutError:
        print("[music] yt-dlp TIMEOUT", flush=True)
        try:
            process.kill()
        except Exception:
            pass
        return False
    except FileNotFoundError:
        print("[music] yt-dlp не установлен в окружении", flush=True)
        return False
    except Exception as e:
        print(f"[music] ERROR: {e}", flush=True)
        return False


def _build_cmd(search_target, output_template, ffmpeg_path):
    cmd = [
        "yt-dlp",
        search_target,
        "-f", FORMAT_SELECTOR,
        "--max-filesize", "30m",
        "--match-filter", f"duration < {MAX_DURATION}",
        "-o", output_template,
        "--no-playlist",
        "--playlist-items", "1",
        "--no-warnings",
        "--quiet",
        "--no-progress",
    ]

    if ffmpeg_path:
        # Есть ffmpeg — приводим к нормальному mp3 с названием трека в теге.
        cmd += [
            "--ffmpeg-location", ffmpeg_path,
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "5",
            "--embed-metadata",
        ]

    return cmd


async def download_music(query):
    """
    Ищет и скачивает трек по названию.
    Возвращает путь к аудиофайлу или None, если не получилось нигде.
    """
    os.makedirs(MUSIC_DIR, exist_ok=True)
    _clean_old_files()

    safe_name = re.sub(r"[^\w\s-]", "", query).strip()[:60] or "track"
    output_template = os.path.join(MUSIC_DIR, f"{safe_name}.%(ext)s")

    ffmpeg_path = _find_ffmpeg()
    if ffmpeg_path:
        print(f"[music] ffmpeg: {ffmpeg_path}", flush=True)
    else:
        print("[music] ffmpeg не найден — качаю без перекодирования", flush=True)

    # 1) SoundCloud — единственный источник, который стабильно работает с
    #    IP хостинга. 2) YouTube — на всякий случай, если SoundCloud не
    #    нашёл трек, а бот запущен не с серверного IP.
    search_targets = [
        f"scsearch1:{query}",
        f"ytsearch1:{query} official audio",
    ]

    for search_target in search_targets:
        source = "SoundCloud" if search_target.startswith("scsearch") else "YouTube"
        print(f"[music] пробую {source}: {query}", flush=True)

        cmd = _build_cmd(search_target, output_template, ffmpeg_path)

        if await _run_ytdlp(cmd):
            path = _find_downloaded(safe_name)
            if path:
                print(f"[music] готово ({source}): {path}", flush=True)
                return path
            print(f"[music] {source}: команда прошла, но файла нет", flush=True)
        else:
            print(f"[music] {source}: не получилось", flush=True)

    print(f"[music] трек не найден нигде: {query}", flush=True)
    return None


# ---------------------------------------------------------------------------
# КОНЕЦ ФАЙЛА router/music.py
# Ниже кода нет. Эти строки — комментарии-подушка: если при копировании
# в GitHub с телефона обрежется самый хвост файла, пострадают только они,
# а рабочий код выше останется целым.
# ---------------------------------------------------------------------------
