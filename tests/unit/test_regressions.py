import asyncio
import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from router import ai_router as ai
from router.locks import conversation_lock, _entries
from database import db
from router.context import get_context
from memory.service import remember, get_user_memory
from projects.service import create, open_project, remove_project
from telegram import handlers
from router import game_logic, reminders
from router.business import extract_trigger_question


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_payload_and_thought_filter(self):
        body = json.dumps({"candidates": [{"content": {"parts": [
            {"text": "private", "thought": True}, {"text": "Ответ"}]}}]})
        post = AsyncMock(return_value=(200, body))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test", "GEMINI_MODEL": "", "GEMINI_THINKING_LEVEL": "medium"}), patch.object(ai, "_post", post):
            answer = await ai.ask_gemini(None, [
                {"role": "system", "content": "rules"}, {"role": "user", "content": "A"},
                {"role": "user", "content": "B"}])
        self.assertEqual(answer, "Ответ")
        _, url, headers, payload = post.call_args.args
        self.assertIn("gemini-3.8-flash", url)
        self.assertEqual(headers["x-goog-api-key"], "test")
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "rules")
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "A\n\nB")
        self.assertEqual(payload["generationConfig"]["thinkingConfig"]["thinkingLevel"], "medium")

    async def test_gemini_model_fallback_on_503(self):
        body = json.dumps({"candidates": [{"content": {"parts": [{"text": "backup answer"}]}}]})
        post = AsyncMock(side_effect=[(503, "busy"), (200, body)])
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test", "GEMINI_MODEL": "gemini-3.8-flash", "GEMINI_FALLBACK_MODELS": "gemini-3.7-flash"}), patch.object(ai, "_post", post):
            self.assertEqual(await ai.ask_gemini(None, [{"role": "user", "content": "hi"}]), "backup answer")
        self.assertIn("gemini-3.8-flash", post.call_args_list[0].args[1])
        self.assertIn("gemini-3.7-flash", post.call_args_list[1].args[1])

    async def test_gemini_auth_does_not_switch_models(self):
        post = AsyncMock(return_value=(403, "no access"))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}), patch.object(ai, "_post", post):
            with self.assertRaises(ai.GeminiHTTPError):
                await ai.ask_gemini(None, [{"role": "user", "content": "hi"}])
        post.assert_awaited_once()

    async def test_gemini_fallback_can_be_disabled(self):
        post = AsyncMock(return_value=(503, "busy"))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test", "GEMINI_FALLBACK_MODELS": ""}), patch.object(ai, "_post", post):
            with self.assertRaises(RuntimeError):
                await ai.ask_gemini(None, [{"role": "user", "content": "hi"}])
        post.assert_awaited_once()

    async def test_invalid_thinking_rejected(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test", "GEMINI_MODEL": "gemini-3.8-flash", "GEMINI_THINKING_LEVEL": "minimal"}):
            with self.assertRaises(ValueError):
                await ai.ask_gemini(None, [{"role": "user", "content": "hi"}])

    async def test_model_prefill_rejected(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):
            with self.assertRaises(ValueError):
                await ai.ask_gemini(None, [{"role": "assistant", "content": "prefill"}])

    async def test_blocked_or_empty_response_is_error(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}), patch.object(ai, "_post", AsyncMock(return_value=(200, '{"promptFeedback":{"blockReason":"SAFETY"}}'))):
            with self.assertRaises(RuntimeError):
                await ai.ask_gemini(None, [{"role": "user", "content": "hi"}])

    async def test_fallback(self):
        async def provider(session, name, messages):
            if name == "gemini":
                raise RuntimeError("temporary error")
            return "OK"
        with patch.object(ai, "init_http_session", AsyncMock()), patch.object(ai, "get_provider_order", return_value=["gemini", "groq"]), patch.object(ai, "ask_provider", side_effect=provider), patch.object(ai, "_safe_log_provider_attempt", AsyncMock()):
            result = await ai.ask("hello")
        self.assertEqual(result["provider"], "groq")
        self.assertEqual(result["errors"][0]["provider"], "gemini")

    async def test_no_keys_fails_cleanly(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(ai, "init_http_session", AsyncMock()):
            self.assertEqual(ai.get_provider_order(), [])
            with self.assertRaises(RuntimeError):
                await ai.ask("hi")

    async def test_classification_false_strings(self):
        raw = '{"is_music_request":"false", "is_website_request":false, "needs_web_search":"true", "search_query":"weather"}'
        with patch.object(ai, "ask_provider", AsyncMock(return_value=raw)):
            result = await ai.classify_request(None, "gemini", "hello")
        self.assertFalse(result["is_music_request"])
        self.assertTrue(result["needs_web_search"])

    async def test_retry_transient_not_auth(self):
        class Response:
            headers = {}
            def __init__(self, status): self.status = status
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def text(self): return "body"
        class Session:
            def __init__(self, codes): self.codes = iter(codes); self.calls = 0
            def post(self, *args, **kwargs):
                self.calls += 1
                return Response(next(self.codes))
        with patch.object(ai.asyncio, "sleep", AsyncMock()):
            session = Session([503, 200])
            self.assertEqual((await ai._post(session, "url", {}, {}))[0], 200)
            self.assertEqual(session.calls, 2)
            session = Session([401])
            self.assertEqual((await ai._post(session, "url", {}, {}))[0], 401)
            self.assertEqual(session.calls, 1)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        await db.close_db()
        db.DB_PATH = self.tmp.name + "/bot.db"
        db._connection_lock = asyncio.Lock()
        db._db_lock = asyncio.Lock()
        await db.init_db()
        self.uid = await db.get_or_create_user(1001, "first")
        self.other = await db.get_or_create_user(1002, "second")

    async def asyncTearDown(self):
        await db.close_db()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    async def test_single_connection_under_concurrency(self):
        await db.close_db()
        conns = await asyncio.gather(*(db.get_db() for _ in range(25)))
        self.assertTrue(all(conn is conns[0] for conn in conns))

    async def test_history_and_group_isolation(self):
        await db.save_message(self.uid, "user", "private")
        await db.save_message(self.uid, "user", "group", chat_id=-1)
        await db.save_message(self.other, "user", "second-private")
        self.assertEqual(await get_context(self.uid), [{"role": "user", "content": "private"}])
        self.assertEqual(len(await db.get_history(self.uid, chat_id=-1)), 1)
        self.assertEqual((await db.get_history(self.other))[0]["content"], "second-private")

    async def test_daily_limit_concurrent(self):
        results = await asyncio.gather(*(db.check_and_increment_limit(self.uid, daily_limit=5) for _ in range(20)))
        self.assertEqual(sum(allowed for allowed, _ in results), 5)
        self.assertEqual(await db.get_usage_count(self.uid), 5)

    async def test_admin_unlimited(self):
        with patch.object(db, "ADMIN_IDS", [1001]):
            self.assertTrue((await db.check_and_increment_limit(self.uid, daily_limit=0, telegram_id=1001))[0])

    async def test_memory_isolation(self):
        await remember(self.uid, "likes Python")
        self.assertEqual((await get_user_memory(self.uid))[0][1], "likes Python")
        self.assertEqual(await get_user_memory(self.other), [])

    async def test_project_ownership(self):
        await create(self.uid, "site", "description")
        self.assertIsNotNone(await open_project(self.uid, "site"))
        self.assertIsNone(await open_project(self.other, "site"))
        self.assertFalse(await remove_project(self.other, "site"))
        self.assertTrue(await remove_project(self.uid, "site"))

    async def test_summary_scope(self):
        await db.save_conversation_summary(self.uid, "private", 1)
        await db.save_conversation_summary(self.uid, "group", 2, chat_id=-1)
        self.assertEqual((await db.get_conversation_summary(self.uid))[0], "private")
        self.assertEqual((await db.get_conversation_summary(self.other, chat_id=-1))[0], "group")

    async def test_reminder_owner(self):
        rid = await db.add_reminder(self.uid, 1001, "test", datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertFalse(await db.cancel_reminder(self.other, rid))
        self.assertTrue(await db.cancel_reminder(self.uid, rid))


class BehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_order_and_cleanup(self):
        values = []
        async def work(n):
            async with conversation_lock("example"):
                values.append((n, "start"))
                await asyncio.sleep(0)
                values.append((n, "end"))
        await asyncio.gather(work(1), work(2))
        self.assertEqual(values, [(1, "start"), (1, "end"), (2, "start"), (2, "end")])
        self.assertNotIn("example", _entries)

    async def test_network_send_not_duplicated(self):
        message = SimpleNamespace(answer=AsyncMock(side_effect=ConnectionError("network")))
        with self.assertRaises(ConnectionError):
            await handlers._send_answer(message, "hello", False)
        self.assertEqual(message.answer.await_count, 1)

    async def test_markdown_parse_fallback(self):
        from aiogram.utils.exceptions import BadRequest
        message = SimpleNamespace(answer=AsyncMock(side_effect=[BadRequest("can't parse entities"), None]))
        await handlers._send_answer(message, "*bad", False)
        self.assertEqual(message.answer.await_count, 2)
        self.assertIsNone(message.answer.call_args.kwargs["parse_mode"])

    async def test_stale_night_action_rejected(self):
        with patch.object(game_logic, "get_game_by_id", AsyncMock(return_value=(1, -1, "voting", None, None, 3))), patch.object(game_logic, "save_game_action", AsyncMock()) as save:
            self.assertFalse(await game_logic.handle_night_action(1, 2, "kill", 5, 6))
            save.assert_not_awaited()

    async def test_wrong_role_rejected(self):
        players = [(1, 5, 1001, "a", "civilian", 1), (2, 6, 1002, "b", "doctor", 1)]
        with patch.object(game_logic, "get_game_by_id", AsyncMock(return_value=(1, -1, "night", None, None, 3))), patch.object(game_logic, "get_game_players", AsyncMock(return_value=players)), patch.object(game_logic, "save_game_action", AsyncMock()) as save:
            self.assertFalse(await game_logic.handle_night_action(1, 3, "kill", 5, 6))
            save.assert_not_awaited()

    async def test_valid_night_action(self):
        players = [(1, 5, 1001, "a", "shadow", 1), (2, 6, 1002, "b", "doctor", 1)]
        with patch.object(game_logic, "get_game_by_id", AsyncMock(return_value=(1, -1, "night", None, None, 3))), patch.object(game_logic, "get_game_players", AsyncMock(return_value=players)), patch.object(game_logic, "save_game_action", AsyncMock()) as save:
            self.assertTrue(await game_logic.handle_night_action(1, 3, "kill", 5, 6))
            save.assert_awaited_once()

    async def test_reminder_network_failure_not_lost(self):
        row = (1, 2, 3, "test", "2026-01-01 10:00:00")
        async def stop(*args): raise asyncio.CancelledError
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=ConnectionError("temporary")))
        with patch.object(reminders, "get_due_reminders", AsyncMock(return_value=[row])), patch.object(reminders, "mark_reminder_sent", AsyncMock()) as mark, patch.object(reminders.asyncio, "sleep", side_effect=stop):
            with self.assertRaises(asyncio.CancelledError): await reminders.reminder_loop(bot)
            mark.assert_not_awaited()

    async def test_startup_import_no_silero(self):
        import router.voice as voice
        self.assertIsNone(voice._model)
        self.assertIsNone(voice._model_error)
        importlib.import_module("main")


class TextTests(unittest.TestCase):
    def test_json_fences(self):
        self.assertEqual(ai.extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_json_not_object(self):
        with self.assertRaises(ValueError): ai.extract_json('[1,2]')

    def test_provider_order_default(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test", "GROQ_API_KEY": "test"}, clear=True):
            self.assertEqual(ai.get_provider_order(), ["gemini", "groq"])

    def test_tools_before_web(self):
        self.assertFalse(handlers.needs_fast_web_search("создай сайт погоды на сегодня"))
        self.assertTrue(handlers.needs_smart_classification("создай сайт погоды на сегодня"))

    def test_long_text_split(self):
        text = "hello " * 2000
        parts = handlers._split_for_telegram(text)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        self.assertEqual(" ".join(parts), text.strip())

    def test_emoji_text_split(self):
        parts = handlers._split_for_telegram("😀" * 6000)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts))
        self.assertEqual("".join(parts), "😀" * 6000)

    def test_code_fence_split(self):
        parts = handlers._split_for_telegram("```python\n" + "print(1)\n" * 1000 + "```")
        self.assertTrue(all(p.count("```") % 2 == 0 for p in parts))
        self.assertTrue(all(len(p) <= 4096 for p in parts))

    def test_business_word_boundary(self):
        self.assertEqual(extract_trigger_question("Kasper, сколько 2+2"), "сколько 2+2")
        self.assertIsNone(extract_trigger_question("касперский антивирус"))

    def test_reminder_question_not_scheduled(self):
        self.assertIsNone(reminders.detect_reminder("напомни как решать уравнения?", "Europe/Berlin"))


if __name__ == "__main__": unittest.main()
