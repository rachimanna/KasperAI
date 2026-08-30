import os
import httpx
from dotenv import load_dotenv

load_dotenv()

model = os.getenv("GEMINI_MODEL")
key = os.getenv("GEMINI_API_KEY")

url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

response = httpx.post(
    url,
    params={"key": key},
    json={
        "contents": [
            {
                "parts": [
                    {"text": "Ответь одним словом: ПРИВЕТ"}
                ]
            }
        ]
    },
    timeout=30,
    trust_env=False,
)

print("HTTP:", response.status_code)
print(response.text[:1000])
