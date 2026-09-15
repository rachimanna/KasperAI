import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "")

CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "")

AI_PROVIDERS = [
    p.strip().lower()
    for p in os.getenv("AI_PROVIDERS", "groq,cerebras,gemini").split(",")
    if p.strip()
]

# Единый источник истины для SQLite. DATABASE_PATH имеет приоритет,
# DB_PATH оставлен для обратной совместимости со старым Render env.
DATABASE_PATH = (
    os.getenv("DATABASE_PATH")
    or os.getenv("DB_PATH")
    or "data/kasper.db"
)

# Telegram ID администраторов бота. Поддерживаются оба варианта:
# ADMIN_ID=123456789 (один айди, уже мог быть задан на Render)
# ADMIN_IDS=123456789,987654321 (несколько через запятую)
_admin_ids_raw = os.getenv("ADMIN_IDS", "") + "," + os.getenv("ADMIN_ID", "")
ADMIN_IDS = [
    int(x.strip())
    for x in _admin_ids_raw.split(",")
    if x.strip().isdigit()
]
