import os
import re
import glob
import asyncio

MUSIC_DIR = "data/music_cache"

MAX_DURATION = 600  # 10 минут, защита от скачивания огромных файлов


def _clean_old_files():
    files = glob.glob(os.path.join(MUSIC_DIR, "*.mp3"))
    if len(files) > 20:
        files.sort(key=os.path.getmtime)
        for f in files[:-20]:
            try:
                os.remove(f)
            except Exception:
                pass


async def download_music(query):
    """
    Ищет и скачивает трек по названию через yt-dlp.
    Возвращает путь к mp3-файлу или None при ошибке.
    """
    os.makedirs(MUSIC_DIR, exist_ok=True)
    _clean_old_files()

    safe_name = re.sub(r'[^\w\s-]', '', query).strip()[:60]
    output_template = os.path.join(MUSIC_DIR, f"{safe_name}.%(ext)s")

    cmd = [
        "yt-dlp",
        f"ytsearch1:{query} official audio",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "5",
        "--max-filesize", "30m",
        "--match-filter", f"duration < {MAX_DURATION}",
        "-o", output_template,
        "--no-playlist",
        "--playlist-items", "1",
        "--embed-metadata",
        "--parse-metadata", "%(title)s:%(meta_title)s",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)

        if process.returncode != 0:
            print(f"[music] yt-dlp ERROR: {stderr.decode()[:300]}", flush=True)
            return None

        mp3_path = os.path.join(MUSIC_DIR, f"{safe_name}.mp3")
        if os.path.exists(mp3_path):
            return mp3_path

        candidates = glob.glob(os.path.join(MUSIC_DIR, f"{safe_name}*.mp3"))
        return candidates[0] if candidates else None

    except asyncio.TimeoutError:
        print("[music] yt-dlp TIMEOUT", flush=True)
        return None
    except Exception as e:
        print(f"[music] ERROR: {e}", flush=True)
        return None
