"""Manual live AI test. Requires keys; not part of the offline test suite."""
import asyncio
import aiohttp
from router.ai_router import ask_gemini, ask, close_http_session
from database.db import close_db

async def main(provider="gemini"):
    try:
        if provider == "gemini":
            async with aiohttp.ClientSession() as session:
                answer = await ask_gemini(session, [{"role": "user", "content": "Ответь одним словом: ПРИВЕТ"}])
            print("Gemini: OK", answer)
        else:
            result = await ask("Ответь одним словом: ПРИВЕТ")
            print("Router: OK", result["provider"], result["answer"])
    finally:
        await close_http_session()
        await close_db()

if __name__ == "__main__":
    asyncio.run(main())
