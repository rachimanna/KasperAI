import os
import subprocess
import json
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("GEMINI_API_KEY")
model = os.getenv("GEMINI_MODEL")

url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

payload = json.dumps({
    "contents": [
        {
            "parts": [
                {"text": "Ответь одним словом: ПРИВЕТ"}
            ]
        }
    ]
})

result = subprocess.run(
    [
        "curl",
        "-sS",
        url,
        "-H", "Content-Type: application/json",
        "-X", "POST",
        "-d", payload,
    ],
    capture_output=True,
    text=True,
    timeout=30,
)

print(result.stdout[:2000])
