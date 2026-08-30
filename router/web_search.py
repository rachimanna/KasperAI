import json
import os

import aiohttp

TAVILY_URL = "https://api.tavily.com/search"

SEARCH_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def tavily_search(query, max_results=4):
    key = os.getenv("TAVILY_API_KEY")

    if not key:
        print("[web_search] TAVILY_API_KEY is missing", flush=True)
        return []

    payload = {
        "api_key": key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": False,
    }

    try:
        async with aiohttp.ClientSession(timeout=SEARCH_TIMEOUT) as session:
            async with session.post(TAVILY_URL, json=payload) as response:
                body = await response.text()

                if response.status >= 400:
                    print(f"[web_search] Tavily HTTP {response.status}: {body[:300]}", flush=True)
                    return []

                data = json.loads(body)

    except Exception as e:
        print(f"[web_search] Tavily ERROR: {e}", flush=True)
        return []

    results = []
    for item in data.get("results", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
            }
        )

    return results


def format_search_results(results):
    if not results:
        return ""

    lines = ["Результаты поиска в интернете:"]

    for i, item in enumerate(results, start=1):
        title = item["title"] or "Без названия"
        content = item["content"][:500]
        lines.append(f"{i}. {title}\n{content}\nИсточник: {item['url']}")

    return "\n\n".join(lines)
