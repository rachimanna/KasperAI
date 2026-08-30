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
    for p in os.getenv("AI_PROVIDERS", "gemini,groq,cerebras").split(",")
    if p.strip()
]

DATABASE_PATH = os.getenv("DATABASE_PATH", "data/kasper.db")
