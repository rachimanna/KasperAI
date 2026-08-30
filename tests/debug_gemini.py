import os
import json
import subprocess
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("GEMINI_API_KEY")
model = os.getenv("GEMINI_MODEL")

url = (
    "https://generativelanguage.googleapis.com/"
    f"v1beta/models/{model}:generateContent"
)

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
        "--connect-timeout", "10",
        "--max-time", "30",
        "-w", "\n__STATUS__:%{http_code}",
        url,
        "-X", "POST",
        "-H", "Content-Type: application/json",
        "-H", f"x-goog-api-key: {key}",
        "-d", payload,
    ],
    capture_output=True,
    text=True,
    timeout=35,
)

output = result.stdout

if "__STATUS__:" in output:
    body, status = output.rsplit("__STATUS__:", 1)
    print("HTTP:", status.strip())
    print("RESPONSE:")
    print(body[:1000]); print("CURL STDERR:"); print(result.stderr[:1000])
else:
    print("CURL ERROR:")
    print(result.stderr[:1000])
