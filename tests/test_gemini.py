from google import genai
from config.settings import GEMINI_API_KEY, GEMINI_MODEL

if not GEMINI_API_KEY:
    raise SystemExit("GEMINI_API_KEY не найден")

if not GEMINI_MODEL:
    raise SystemExit("GEMINI_MODEL не указан")

client = genai.Client(api_key=GEMINI_API_KEY)

try:
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents="Ответь одним словом: ПРИВЕТ",
    )
    print("[Gemini] OK")
    print("Ответ:", response.text)
except Exception as e:
    print("[Gemini] ERROR")
    print(type(e).__name__ + ":", str(e)[:1000])
