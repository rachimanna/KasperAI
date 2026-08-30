import json
import os

import aiohttp
from dotenv import load_dotenv

load_dotenv(override=True)


TIMEOUT = aiohttp.ClientTimeout(
    total=45,
    connect=10,
    sock_connect=10,
    sock_read=35,
)


async def _post(session, url, headers, payload):
    async with session.post(
        url,
        headers=headers,
        json=payload,
    ) as response:
        body = await response.text()
        return response.status, body


async def ask_gemini(session, messages):
    key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL")

    if not key:
        raise RuntimeError("Gemini API key is missing")

    if not model:
        raise RuntimeError("Gemini model is missing")

    contents = []

    for message in messages:
        role = message.get("role", "user")

        if role == "assistant":
            role = "model"
        else:
            role = "user"

        content = str(message.get("content", ""))

        if not content:
            continue

        contents.append(
            {
                "role": role,
                "parts": [
                    {
                        "text": content,
                    }
                ],
            }
        )

    if not contents:
        raise RuntimeError("Gemini received empty messages")

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    payload = {
        "contents": contents,
    }

    status, body = await _post(
        session,
        url,
        {
            "x-goog-api-key": key,
            "Content-Type": "application/json",
        },
        payload,
    )

    if status >= 400:
        raise RuntimeError(
            f"Gemini HTTP {status}: {body[:500]}"
        )

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Gemini returned invalid JSON: {body[:500]}"
        )

    try:
        parts = data["candidates"][0]["content"]["parts"]

        text_parts = []

        for part in parts:
            if isinstance(part, dict) and "text" in part:
                text_parts.append(part["text"])

        answer = "".join(text_parts).strip()

        if not answer:
            raise RuntimeError(
                f"Gemini returned empty answer: {body[:500]}"
            )

        return answer

    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Unexpected Gemini response: {body[:500]}"
        )


async def _call_groq_with_key(session, key, model, url, clean_messages):
    payload = {
        "model": model,
        "messages": clean_messages,
    }
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "medium"

    status, body = await _post(
        session,
        url,
        {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        payload,
    )

    if status >= 400:
        raise RuntimeError(f"groq HTTP {status}: {body[:500]}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"groq returned invalid JSON: {body[:500]}")

    try:
        answer = data["choices"][0]["message"]["content"]
        if not answer:
            raise RuntimeError("groq returned empty answer")
        return str(answer).strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Unexpected groq response: {body[:500]}")


async def ask_openai_compatible(session, provider, messages):
    if provider == "groq":
        model = os.getenv("GROQ_MODEL")
        url = "https://api.groq.com/openai/v1/chat/completions"

        if not model:
            raise RuntimeError("groq model is missing")

        clean_messages = []
        for message in messages:
            role = message.get("role", "user")
            content = str(message.get("content", ""))
            if not content:
                continue
            if role not in ("system", "user", "assistant"):
                role = "user"
            clean_messages.append({"role": role, "content": content})

        if not clean_messages:
            raise RuntimeError("groq received empty messages")

        keys = [
            k for k in [os.getenv("GROQ_API_KEY"), os.getenv("GROQ_API_KEY_2")]
            if k
        ]
        if not keys:
            raise RuntimeError("groq API key is missing")

        last_error = None
        for i, key in enumerate(keys):
            try:
                return await _call_groq_with_key(session, key, model, url, clean_messages)
            except Exception as e:
                last_error = e
                print(f"[groq] key #{i+1} failed: {e}", flush=True)
        raise last_error

    elif provider == "cerebras":
        key = os.getenv("CEREBRAS_API_KEY")
        model = os.getenv("CEREBRAS_MODEL")
        url = "https://api.cerebras.ai/v1/chat/completions"

    elif provider == "cerebras":
        key = os.getenv("CEREBRAS_API_KEY")
        model = os.getenv("CEREBRAS_MODEL")
        url = "https://api.cerebras.ai/v1/chat/completions"

    elif provider == "openai":
        key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL")
        url = "https://api.openai.com/v1/chat/completions"

    else:
        raise RuntimeError(
            f"Unknown provider: {provider}"
        )

    if not key:
        raise RuntimeError(
            f"{provider} API key is missing"
        )

    if not model:
        raise RuntimeError(
            f"{provider} model is missing"
        )

    clean_messages = []

    for message in messages:
        role = message.get("role", "user")
        content = str(message.get("content", ""))

        if not content:
            continue

        if role not in ("system", "user", "assistant"):
            role = "user"

        clean_messages.append(
            {
                "role": role,
                "content": content,
            }
        )

    if not clean_messages:
        raise RuntimeError(
            f"{provider} received empty messages"
        )

    payload = {
        "model": model,
        "messages": clean_messages,
    }
    if provider == "groq" and "gpt-oss" in model:
        payload["reasoning_effort"] = "medium"

    status, body = await _post(
        session,
        url,
        {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        payload,
    )

    if status >= 400:
        raise RuntimeError(
            f"{provider} HTTP {status}: {body[:500]}"
        )

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"{provider} returned invalid JSON: {body[:500]}"
        )

    try:
        answer = data["choices"][0]["message"]["content"]

        if not answer:
            raise RuntimeError(
                f"{provider} returned empty answer"
            )

        return str(answer).strip()

    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Unexpected {provider} response: {body[:500]}"
        )


def get_provider_order():
    value = os.getenv(
        "AI_PROVIDERS",
        "groq,cerebras,gemini",
    )

    return [
        provider.strip().lower()
        for provider in value.split(",")
        if provider.strip()
    ]


async def ask_provider(session, provider, messages):
    print(
        f"[{provider}] trying...",
        flush=True,
    )

    if provider == "gemini":
        answer = await ask_gemini(
            session,
            messages,
        )

    elif provider == "xkiro":
        answer = await ask_xkiro(
            session,
            messages,
        )

    elif provider in ("groq", "cerebras", "openai"):
        answer = await ask_openai_compatible(
            session,
            provider,
            messages,
        )

    else:
        raise RuntimeError(
            f"Unknown provider: {provider}"
        )

    print(
        f"[{provider}] OK",
        flush=True,
    )

    return answer


async def ask(messages):
    if isinstance(messages, str):
        messages = [
            {
                "role": "user",
                "content": messages,
            }
        ]

    errors = []

    async with aiohttp.ClientSession(
        timeout=TIMEOUT
    ) as session:

        for provider in get_provider_order():
            try:
                answer = await ask_provider(
                    session,
                    provider,
                    messages,
                )

                return {
                    "provider": provider,
                    "answer": answer,
                    "errors": errors,
                }

            except Exception as e:
                error = str(e)

                print(
                    f"[{provider}] ERROR: {error}",
                    flush=True,
                )

                errors.append(
                    {
                        "provider": provider,
                        "error": error,
                    }
                )

    raise RuntimeError(
        "All AI providers failed: "
        + json.dumps(
            errors,
            ensure_ascii=False,
        )
    )


async def should_search_web(session, provider, user_text):
    """
    Спрашивает у AI, нужен ли для ответа поиск в интернете.
    Возвращает (need_search: bool, query: str)
    """
    system_prompt = (
        "Ты определяешь, нужен ли для ответа на вопрос пользователя "
        "поиск актуальной информации в интернете (новости, курсы валют, "
        "погода, свежие события, факты после 2025 года и т.п.). "
        "Ответь СТРОГО в формате JSON без каких-либо пояснений: "
        '{"need_search": true/false, "query": "поисковый запрос на русском или английском"}. '
        "Если поиск не нужен (общие вопросы, разговор, творчество, код) — "
        '{"need_search": false, "query": ""}'
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]

    try:
        if provider == "gemini":
            raw = await ask_gemini(session, messages)
        elif provider in ("groq", "cerebras"):
            raw = await ask_openai_compatible(session, provider, messages)
        else:
            return False, ""

        raw = raw.strip()

        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        data = json.loads(raw)

        return bool(data.get("need_search")), str(data.get("query", "")).strip()

    except Exception as e:
        print(f"[should_search_web] ERROR: {e}", flush=True)
        return False, ""


async def should_play_music(session, provider, user_text):
    system_prompt = (
        'Определи, просит ли пользователь включить/найти/скачать музыку или песню. '
        'Ответь СТРОГО JSON без пояснений: '
        '{"is_music_request": true/false, "track_query": "название трека и исполнителя для поиска"}. '
        'Если это не запрос музыки — {"is_music_request": false, "track_query": ""}'
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
    try:
        if provider == "gemini":
            raw = await ask_gemini(session, messages)
        elif provider in ("groq", "cerebras"):
            raw = await ask_openai_compatible(session, provider, messages)
        else:
            return False, ""
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        return bool(data.get("is_music_request")), str(data.get("track_query", "")).strip()
    except Exception as e:
        print(f"[should_play_music] ERROR: {e}", flush=True)
        return False, ""


async def should_create_website(session, provider, user_text):
    system_prompt = (
        'Определи, просит ли пользователь СОЗДАТЬ веб-сайт, лендинг, '
        'страницу регистрации/логина, портфолио или любую HTML-страницу. '
        'Ответь СТРОГО JSON без пояснений: '
        '{"is_website_request": true/false, "site_description": "краткое описание того, что за сайт нужен"}. '
        'Если это не запрос на создание сайта — {"is_website_request": false, "site_description": ""}'
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
    try:
        if provider == "gemini":
            raw = await ask_gemini(session, messages)
        elif provider in ("groq", "cerebras", "openai"):
            raw = await ask_openai_compatible(session, provider, messages)
        else:
            return False, ""
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        return bool(data.get("is_website_request")), str(data.get("site_description", "")).strip()
    except Exception as e:
        print(f"[should_create_website] ERROR: {e}", flush=True)
        return False, ""


async def generate_website_html(session, provider, description):
    system_prompt = (
        "Ты — опытный веб-дизайнер. Создай ПОЛНЫЙ, готовый к использованию "
        "HTML-файл с современным, красивым дизайном (CSS внутри тега <style>, "
        "JS внутри тега <script>, всё в одном файле). Используй плавные "
        "анимации, градиенты, адаптивную вёрстку. Ответь СТРОГО кодом, "
        "начиная с <!DOCTYPE html>, без пояснений и без markdown-обёртки "
        "тройными кавычками."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": description},
    ]
    if provider == "gemini":
        raw = await ask_gemini(session, messages)
    elif provider in ("groq", "cerebras", "openai"):
        raw = await ask_openai_compatible(session, provider, messages)
    else:
        raise RuntimeError(f"Unknown provider: {provider}")

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("html"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


async def classify_request(session, provider, user_text):
    system_prompt = (
        'Проанализируй сообщение пользователя и определи ОДНОВРЕМЕННО три вещи. '
        'Ответь СТРОГО JSON без пояснений, в формате:\n'
        '{"is_music_request": true/false, "track_query": "название трека и исполнителя, если это музыка, иначе пусто", '
        '"is_website_request": true/false, "site_description": "описание сайта, если это запрос на создание сайта, иначе пусто", '
        '"needs_web_search": true/false, "search_query": "поисковый запрос, если нужен веб-поиск для ответа на вопрос, иначе пусто"}\n'
        'is_music_request=true только если явно просят включить/найти/скачать музыку или песню. '
        'is_website_request=true только если явно просят создать сайт, лендинг, страницу регистрации/логина, портфолио. '
        'needs_web_search=true только если нужна свежая/актуальная информация (новости, курсы, погода, текущие события), '
        'которую ты не можешь знать заранее. Если сообщение не подходит ни под одну категорию — все флаги false.'
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
    default = {
        "is_music_request": False,
        "track_query": "",
        "is_website_request": False,
        "site_description": "",
        "needs_web_search": False,
        "search_query": "",
    }
    try:
        if provider == "gemini":
            raw = await ask_gemini(session, messages)
        elif provider in ("groq", "cerebras", "openai"):
            raw = await ask_openai_compatible(session, provider, messages)
        else:
            return default
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        return {
            "is_music_request": bool(data.get("is_music_request")),
            "track_query": str(data.get("track_query", "")).strip(),
            "is_website_request": bool(data.get("is_website_request")),
            "site_description": str(data.get("site_description", "")).strip(),
            "needs_web_search": bool(data.get("needs_web_search")),
            "search_query": str(data.get("search_query", "")).strip(),
        }
    except Exception as e:
        print(f"[classify_request] ERROR: {e}", flush=True)
        return default


async def ask_xkiro(session, messages):
    key = os.getenv("XKIRO_API_KEY")
    model = os.getenv("XKIRO_MODEL", "qwen/qwen3.8-max:free")
    base_url = os.getenv("XKIRO_BASE_URL", "https://api.xkiro.com/v1").rstrip("/")

    if not key:
        raise RuntimeError("XKiro API key is missing")

    clean_messages = []

    for message in messages:
        role = message.get("role", "user")
        content = str(message.get("content", ""))

        if not content:
            continue

        if role not in ("system", "user", "assistant"):
            role = "user"

        clean_messages.append({
            "role": role,
            "content": content,
        })

    if not clean_messages:
        raise RuntimeError("XKiro received empty messages")

    payload = {
        "model": model,
        "messages": clean_messages,
    }

    status, body = await _post(
        session,
        f"{base_url}/chat/completions",
        {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        payload,
    )

    if status >= 400:
        raise RuntimeError(
            f"XKiro HTTP {status}: {body[:500]}"
        )

    try:
        data = json.loads(body)
        answer = data["choices"][0]["message"]["content"]

        if not answer:
            raise RuntimeError("XKiro returned empty answer")

        return str(answer).strip()

    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        raise RuntimeError(
            f"Unexpected XKiro response: {body[:500]}"
        )
