import json
import os
import urllib.request
import urllib.error
from dotenv import load_dotenv

load_dotenv()

def request(url, key, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            return e.code, body
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}

print("=== Kasper AI — API TEST ===")

# Gemini
gemini_key = os.getenv("GEMINI_API_KEY")
gemini_model = os.getenv("GEMINI_MODEL")

if gemini_key and gemini_model:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}"
    payload = {
        "contents": [
            {"parts": [{"text": "Ответь одним словом: ПРИВЕТ"}]}
        ]
    }

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            print("[Gemini] OK")
    except Exception as e:
        print("[Gemini] ERROR:", str(e)[:300])
else:
    print("[Gemini] NOT CONFIGURED")

# Groq
groq_key = os.getenv("GROQ_API_KEY")
groq_model = os.getenv("GROQ_MODEL")

if groq_key and groq_model:
    status, result = request(
        "https://api.groq.com/openai/v1/chat/completions",
        groq_key,
        {
            "model": groq_model,
            "messages": [{"role": "user", "content": "Ответь одним словом: ПРИВЕТ"}],
        },
    )

    if status == 200:
        print("[Groq] OK")
    else:
        print("[Groq] ERROR:", str(result)[:300])
else:
    print("[Groq] NOT CONFIGURED")

# Cerebras
cerebras_key = os.getenv("CEREBRAS_API_KEY")

if cerebras_key:
    status, result = request(
        "https://api.cerebras.ai/v1/chat/completions",
        cerebras_key,
        {
            "model": "gpt-oss-120b",
            "messages": [{"role": "user", "content": "Ответь одним словом: ПРИВЕТ"}],
        },
    )

    if status == 200:
        print("[Cerebras] OK")
    else:
        error = result.get("error", result)
        if isinstance(error, dict):
            print("[Cerebras] ERROR:", error.get("code", error.get("type", "unknown")))
        else:
            print("[Cerebras] ERROR:", str(error)[:300])
else:
    print("[Cerebras] NOT CONFIGURED")

print("=== TEST COMPLETE ===")
