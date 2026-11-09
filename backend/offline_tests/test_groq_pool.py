"""Offline scheduler tests. No real API, keys, media, or project data are used.

Run: python -m unittest discover -s backend/offline_tests -p test_groq_pool.py
"""

import asyncio
import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import groq_pool as pool


class _Cursor:
    def __init__(self, values):
        self.values = values

    async def to_list(self, length=None):
        return copy.deepcopy(self.values if length is None else self.values[:length])


class _Collection:
    def __init__(self):
        self.docs = []

    def find(self, query=None):
        return _Cursor([d for d in self.docs if all(d.get(k) == v for k, v in (query or {}).items())])

    async def find_one(self, query):
        docs = await self.find(query).to_list(1)
        return docs[0] if docs else None

    async def insert_one(self, doc):
        self.docs.append(copy.deepcopy(doc))

    async def update_one(self, query, update, upsert=False):
        found = next((d for d in self.docs if all(d.get(k) == v for k, v in query.items())), None)
        if found is None and upsert:
            found = copy.deepcopy(query)
            self.docs.append(found)
        if found is not None:
            found.update(copy.deepcopy(update.get("$set", {})))
            for name, amount in update.get("$inc", {}).items():
                found[name] = found.get(name, 0) + amount
        return SimpleNamespace(matched_count=int(found is not None))


class _Database:
    def __init__(self):
        self.groq_keys = _Collection()
        self.groq_usage = _Collection()


class GroqPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = _Database()
        self.at = datetime(2026, 11, 6, 23, 59, tzinfo=timezone.utc).timestamp()
        self.clock = patch.object(pool.time, "time", return_value=self.at).start()
        self.addCleanup(patch.stopall)
        pool._LOCK = asyncio.Lock()
        self.calls = []
        self.responses = []
        real_client = httpx.AsyncClient

        async def handler(request):
            self.calls.append(request)
            if not self.responses:
                raise AssertionError("Unexpected HTTP request: every call must be explicitly mocked")
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            if callable(response):
                return await response(request)
            return response

        self.transport = httpx.MockTransport(handler)
        self.client_factory = patch.object(pool.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=self.transport, **kw)).start()

    async def key(self, key_id="a", **fields):
        value = {
            "id": key_id, "key": "gsk_OFFLINE_SECRET_" + key_id,
            "free_tier_confirmed": True, "day": pool._day(self.at),
            "used_requests": 0, "used_seconds": 0, "used_tokens": 0,
            **fields,
        }
        await self.db.groq_keys.insert_one(value)
        return value

    @staticmethod
    def chat_body(**fields):
        return {"model": "openai/gpt-oss-20b", "messages": [{"role": "user", "content": "Привет"}], "max_tokens": 32, **fields}

    async def chat(self, **fields):
        return await pool.request(self.db, "/chat/completions", json_body=self.chat_body(**fields))

    async def audio(self, seconds=60):
        return await pool.request(self.db, "/audio/transcriptions", data={"model": "whisper-large-v3"},
                                  files={"file": ("offline.mp3", b"not-real-audio", "audio/mpeg")}, audio_seconds=seconds)

    async def seed(self, key, model, *, events=None, blocks=None):
        scope = pool._scope(key)
        state = {"id": pool._state_id(scope, model), "scope": scope, "model": model,
                 "events": events or [], "blocks": blocks or {}, "provider": {}, "provider_limits": {}}
        await self.db.groq_usage.insert_one(state)
        return state

    def event(self, *, at=None, requests=0, tokens=0, audio_seconds=0, key_id="a"):
        return {"id": "earlier", "key_id": key_id, "at": self.at if at is None else at,
                "requests": requests, "tokens": tokens, "audio_seconds": audio_seconds, "status": "completed"}

    def success(self, tokens=17):
        return httpx.Response(200, json={"choices": [], "usage": {"total_tokens": tokens}})

    def test_duration_parser_and_http_date(self):
        for value, expected in [("2m59.56s", 179.56), ("1d2h3m4s", 93784), ("1500ms", 1.5), ("2.5", 2.5)]:
            self.assertAlmostEqual(pool.parse_duration(value), expected)
        self.assertIsNone(pool.parse_duration("NaN"))
        self.assertIsNone(pool.parse_duration("-1"))
        self.assertIsNone(pool.parse_duration("unknown"))
        self.assertEqual(pool._deadline("Sat, 07 Nov 2026 00:00:00 GMT", self.at), self.at + 60)

    def test_body_retry_duration_and_maximum_exhausted_reset(self):
        response = httpx.Response(429, text="Rate limit. Please try again in 1h2m3.45s.")
        self.assertAlmostEqual(pool._rate_blocks(response, self.at)["rate_limit"], self.at + 3723.45)
        response = httpx.Response(429, headers={
            "retry-after": "5", "x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "1d",
            "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "7s",
        })
        blocks = pool._rate_blocks(response, self.at)
        self.assertEqual(max(blocks.values()), self.at + 86400)
        self.assertEqual(blocks["tpm"], self.at + 7)

    async def test_unconfirmed_free_account_never_creates_http_client(self):
        await self.key(free_tier_confirmed=False)
        await self.key("b", free_tier_confirmed=None)
        with self.assertRaisesRegex(pool.NoGroqKeys, "Подтвердите бесплатный тариф"):
            await self.chat()
        self.client_factory.assert_not_called()
        self.assertEqual(self.db.groq_usage.docs, [])

    async def test_legacy_missing_confirmation_is_blocked(self):
        key = await self.key()
        self.db.groq_keys.docs[0].pop("free_tier_confirmed")
        with self.assertRaises(pool.NoGroqKeys):
            await self.audio()
        self.assertFalse(self.calls)
        self.assertEqual((await pool.get_key_status(self.db))[0]["status"], "unconfirmed")
        self.assertNotIn(key["key"], json.dumps(await pool.get_key_status(self.db)))

    async def test_manual_paused_key_not_used(self):
        await self.key(paused=True)
        with self.assertRaises(pool.NoGroqKeys):
            await self.chat()
        status = (await pool.get_key_status(self.db))[0]
        self.assertEqual(status["status"], "cooldown")
        self.assertIsNone(status["next_reset_at"])
        self.assertFalse(self.calls)

    async def test_utc_counter_reset_does_not_clear_provider_cooldown(self):
        until = self.at + 100000
        await self.key(day="2026-11-05", used_seconds=200, used_requests=5, used_tokens=300, cooldown_until=until)
        status = (await pool.get_key_status(self.db))[0]
        self.assertEqual(status["day"], "2026-11-06")
        self.assertEqual(status["used_requests"], 0)
        self.assertEqual(status["next_reset_at"], until)
        with self.assertRaises(pool.GroqDeferred) as caught:
            await self.chat()
        self.assertEqual(caught.exception.retry_at, until)
        self.assertEqual(self.db.groq_keys.docs[0]["cooldown_until"], until)
        self.assertFalse(self.calls)

    async def test_rolling_audio_budget_survives_midnight(self):
        key = await self.key()
        await self.seed(key, "whisper-large-v3", events=[self.event(at=self.at - 100, requests=1, audio_seconds=5700)])
        self.clock.return_value = self.at + 120  # UTC next day; quota must not reset.
        with self.assertRaises(pool.GroqDeferred) as caught:
            await self.audio(seconds=100)
        self.assertEqual(caught.exception.retry_at, self.at - 100 + 3600)
        self.assertEqual(self.db.groq_keys.docs[0]["day"], "2026-11-07")
        self.assertFalse(self.calls)

    async def test_rolling_request_day_is_24_hours_not_calendar_day(self):
        key = await self.key()
        model = "openai/gpt-oss-20b"
        with patch.dict(pool.FREE_LIMITS, {model: {"rpm": 100, "rpd": 10, "tpm": 8000, "tpd": 200000}}):
            await self.seed(key, model, events=[self.event(at=self.at - 3600, requests=8)])
            self.clock.return_value = self.at + 120
            with self.assertRaises(pool.GroqDeferred) as caught:
                await self.chat()
        self.assertEqual(caught.exception.retry_at, self.at - 3600 + 86400)
        self.assertFalse(self.calls)

    async def test_keys_in_same_scope_share_budget(self):
        key = await self.key(scope_id="organization-offline")
        await self.key("b", organization_id="organization-offline")
        await self.seed(key, "whisper-large-v3", events=[self.event(audio_seconds=5760)])
        with self.assertRaises(pool.GroqDeferred) as caught:
            await self.audio()
        self.assertEqual(caught.exception.retry_at, self.at + 3600)
        self.assertFalse(self.calls)

    async def test_independent_accounts_do_not_share_budget(self):
        key = await self.key()
        await self.key("b")
        await self.seed(key, "whisper-large-v3", events=[self.event(audio_seconds=5760)])
        self.responses.append(httpx.Response(200, json={"text": "offline", "duration": 60}))
        await self.audio()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0].headers["authorization"].endswith("_b"))

    async def test_429_day_header_not_capped_to_six_hours(self):
        await self.key()
        self.responses.append(httpx.Response(429, json={"error": {"message": "rate limit"}}, headers={
            "retry-after": "2", "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "1d2h",
        }))
        with self.assertRaises(pool.GroqDeferred) as caught:
            await self.chat()
        exact = self.at + 26 * 3600
        self.assertEqual(caught.exception.retry_at, exact)
        self.clock.return_value = self.at + 120
        with self.assertRaises(pool.GroqDeferred) as later:
            await self.chat()
        self.assertEqual(later.exception.retry_at, exact)
        self.assertEqual(len(self.calls), 1)

    async def test_429_minute_tokens_ignores_nonexhausted_day_reset(self):
        await self.key()
        self.responses.append(httpx.Response(429, json={"error": {"message": "minute token limit"}}, headers={
            "retry-after": "2", "x-ratelimit-limit-requests": "1000", "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-requests": "500", "x-ratelimit-reset-requests": "12h",
            "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "7.66s",
        }))
        with self.assertRaises(pool.GroqDeferred) as caught:
            await self.chat()
        self.assertAlmostEqual(caught.exception.retry_at, self.at + 7.66)
        self.assertEqual(len(self.calls), 1)

    async def test_shared_scope_429_does_not_retry_other_org_key(self):
        await self.key(scope_id="same-org")
        await self.key("b", scope_id="same-org")
        self.responses.append(httpx.Response(429, headers={"retry-after": "61"}, json={"error": {"message": "RPM"}}))
        with self.assertRaises(pool.GroqDeferred) as caught:
            await self.chat()
        self.assertEqual(caught.exception.retry_at, self.at + 61)
        self.assertEqual(len(self.calls), 1)

    async def test_cooldown_is_per_model(self):
        await self.key()
        self.responses.append(httpx.Response(429, headers={"retry-after": "86401"}, json={"error": {"message": "audio quota"}}))
        with self.assertRaises(pool.GroqDeferred):
            await self.audio()
        self.responses.append(self.success())
        result = await self.chat()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.calls), 2)

    async def test_actual_tokens_replace_estimate(self):
        await self.key()
        self.responses.append(self.success(tokens=17))
        self.assertGreater(pool.estimate_tokens(self.chat_body()), 17)
        response = await self.chat()
        self.assertIsInstance(response, httpx.Response)
        self.assertEqual(self.db.groq_usage.docs[0]["events"][0]["tokens"], 17)
        status = (await pool.get_key_status(self.db))[0]
        self.assertEqual(status["used_tokens"], 17)
        self.assertEqual(status["used_requests"], 1)
        self.assertNotIn("gsk_OFFLINE_SECRET_a", json.dumps(status))

    async def test_missing_usage_keeps_reservation(self):
        await self.key()
        self.responses.append(httpx.Response(200, json={"choices": []}))
        await self.chat()
        self.assertEqual(self.db.groq_usage.docs[0]["events"][0]["tokens"], pool.estimate_tokens(self.chat_body()))

    async def test_success_headers_preserve_twenty_percent_reserve(self):
        await self.key()
        self.responses.append(httpx.Response(200, json={"usage": {"total_tokens": 17}}, headers={
            "x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "1200",
            "x-ratelimit-reset-tokens": "45s",
        }))
        await self.chat()
        with self.assertRaises(pool.GroqDeferred) as caught:
            await self.chat()
        self.assertEqual(caught.exception.retry_at, self.at + 45)
        self.assertEqual(len(self.calls), 1)

    async def test_timeout_preserves_reservation_and_does_not_retry(self):
        await self.key()
        await self.key("b")
        self.responses.append(httpx.ReadTimeout("gsk_OFFLINE_SECRET_a"))
        with self.assertRaises(pool.GroqUnavailable) as caught:
            await self.chat()
        self.assertNotIn("gsk_OFFLINE_SECRET_a", str(caught.exception))
        self.assertEqual(len(self.calls), 1)
        event = self.db.groq_usage.docs[0]["events"][0]
        self.assertEqual(event["status"], "reserved")
        self.assertEqual(event["tokens"], pool.estimate_tokens(self.chat_body()))

    async def test_auth_401_disables_only_invalid_key(self):
        await self.key()
        await self.key("b")
        self.responses.extend([httpx.Response(401, json={"error": {"message": "invalid key"}}), self.success()])
        await self.chat()
        self.assertTrue(self.db.groq_keys.docs[0]["disabled"])
        self.assertFalse(self.db.groq_keys.docs[1].get("disabled", False))
        self.assertEqual(len(self.calls), 2)

    async def test_permission_403_does_not_disable_or_rotate(self):
        await self.key()
        await self.key("b")
        self.responses.append(httpx.Response(403, json={"error": {"message": "Access denied by organization policy"}}))
        with self.assertRaises(pool.GroqUnavailable):
            await self.chat()
        self.assertFalse(self.db.groq_keys.docs[0].get("disabled", False))
        self.assertEqual(len(self.calls), 1)

    async def test_billing_error_blocks_scope_and_stops_rotation(self):
        await self.key(scope_id="same-org")
        await self.key("b", scope_id="same-org")
        await self.key("c")
        self.responses.append(httpx.Response(402, json={"error": {"message": "Insufficient credits"}}))
        with self.assertRaisesRegex(pool.GroqUnavailable, "оплате/кредитах"):
            await self.chat()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.db.groq_keys.docs[0]["billing_blocked"])
        self.assertTrue(self.db.groq_keys.docs[1]["billing_blocked"])
        self.assertFalse(self.db.groq_keys.docs[2].get("billing_blocked", False))

    async def test_model_404_is_not_invalid_key(self):
        await self.key()
        self.responses.append(httpx.Response(404, json={"error": {"message": "model not found"}}))
        with self.assertRaises(pool.GroqModelUnavailable):
            await self.chat()
        self.assertFalse(self.db.groq_keys.docs[0].get("disabled", False))

    async def test_concurrent_reservations_cannot_overspend_audio_hour(self):
        await self.key()
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_response(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json={"text": "offline", "duration": 4000})

        self.responses.append(slow_response)
        first = asyncio.create_task(self.audio(seconds=4000))
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            with self.assertRaises(pool.GroqDeferred) as caught:
                await self.audio(seconds=4000)
            self.assertEqual(caught.exception.retry_at, self.at + 3600)
            self.assertEqual(len(self.calls), 1)
        finally:
            release.set()
            await first

    async def test_cancelled_inflight_request_keeps_durable_reservation(self):
        await self.key()
        started = asyncio.Event()

        async def interrupted(request):
            started.set()
            await asyncio.Event().wait()

        self.responses.append(interrupted)
        task = asyncio.create_task(self.chat())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.db.groq_usage.docs[0]["events"][0]["status"], "reserved")
        self.assertEqual(self.db.groq_keys.docs[0]["used_requests"], 1)

    async def test_provider_headers_keep_other_inflight_reservations(self):
        await self.key()
        first_started, second_started = asyncio.Event(), asyncio.Event()
        release_first, release_second = asyncio.Event(), asyncio.Event()

        async def first_response(request):
            first_started.set()
            await release_first.wait()
            return httpx.Response(200, json={"usage": {"total_tokens": 1}}, headers={
                "x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "6000",
                "x-ratelimit-reset-tokens": "60s",
            })

        async def second_response(request):
            second_started.set()
            await release_second.wait()
            return self.success(tokens=2000)

        self.responses.extend([first_response, second_response])
        first = asyncio.create_task(self.chat(max_tokens=2000))
        await asyncio.wait_for(first_started.wait(), timeout=2)
        second = asyncio.create_task(self.chat(max_tokens=2000))
        await asyncio.wait_for(second_started.wait(), timeout=2)
        try:
            release_first.set()
            await first
            with self.assertRaises(pool.GroqDeferred) as caught:
                await self.chat(max_tokens=3000)
            self.assertEqual(caught.exception.retry_at, self.at + 60)
            self.assertEqual(len(self.calls), 2)
        finally:
            release_first.set()
            release_second.set()
            await asyncio.gather(first, second)

    def test_image_estimate_excludes_base64_payload(self):
        body = self.chat_body(messages=[{"role": "user", "content": [
            {"type": "text", "text": "Describe"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}},
        ]}])
        small = pool.estimate_tokens(body)
        body["messages"][0]["content"][1]["image_url"]["url"] += "A" * 2000000
        self.assertEqual(pool.estimate_tokens(body), small)
        self.assertLess(small, 3000)

    async def test_unknown_model_endpoint_and_paid_tier_never_call_http(self):
        await self.key()
        with self.assertRaises(pool.GroqModelUnavailable):
            await self.chat(model="unknown/possibly-paid")
        with self.assertRaises(pool.GroqUnavailable):
            await self.chat(service_tier="flex")
        with self.assertRaises(pool.GroqUnavailable):
            await pool.request(self.db, "https://elsewhere.invalid", json_body=self.chat_body())
        self.assertFalse(self.calls)

    async def test_single_oversized_request_is_permanent_error_not_deferred(self):
        await self.key()
        with self.assertRaises(pool.GroqUnavailable) as caught:
            await self.audio(seconds=6000)
        self.assertNotIsInstance(caught.exception, pool.GroqDeferred)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
