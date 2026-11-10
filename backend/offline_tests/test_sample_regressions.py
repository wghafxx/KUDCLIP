"""Sample-only regressions: fake Groq calls and one-second synthetic media."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

BACKEND = Path(__file__).resolve().parents[1]
WORK = Path(r"C:\Users\lolar\Documents\Codex\2026-11-06\c-users-lolar-desktop-v2-0\work\sample-regressions")
WORK.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BACKEND))
with patch.dict(os.environ, {"MEDIA_DIR": str(WORK / "import-media")}):
    import pipeline
import groq_pool
import ready_pack
import httpx
from filedb import Database


async def local_run(command, timeout=120):
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace")[-1600:])
    return stdout.decode(errors="replace")


class FreeModelFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_transient_gateway_failures_try_next_free_model(self):
        response = {"choices": [{"message": {"content": '{"ads":[]}'}}]}
        models = ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
        for status in (502, 503, 504):
            with self.subTest(status=status):
                fake = AsyncMock(side_effect=[groq_pool.GroqUnavailable(f"Groq HTTP {status}"), response])
                with patch.object(pipeline, "groq_request", fake), patch.object(
                        pipeline.httpx, "AsyncClient", side_effect=AssertionError("Live HTTP forbidden")):
                    result = await pipeline._groq_chat_json(object(), [{"role": "user", "content": "Offline"}], models=models)
                self.assertEqual(result, {"ads": []})
                self.assertEqual([call.kwargs["json_body"]["model"] for call in fake.await_args_list], models)
                self.assertTrue(all(model in groq_pool.FREE_LIMITS for model in models))

    async def test_quota_or_other_failure_is_not_rotated_to_another_model(self):
        errors = [groq_pool.GroqDeferred(9999999999, "HTTP 503 but quota must propagate"),
                  groq_pool.GroqUnavailable("Groq HTTP 500"), groq_pool.GroqUnavailable("Groq HTTP 403"),
                  groq_pool.GroqUnavailable("Ошибка сети: исход запроса неизвестен"),
                  groq_pool.GroqUnavailable("Billing blocked")]
        for error in errors:
            with self.subTest(error=type(error).__name__, message=str(error)):
                fake = AsyncMock(side_effect=error)
                with patch.object(pipeline, "groq_request", fake), patch.object(
                        pipeline.httpx, "AsyncClient", side_effect=AssertionError("Live HTTP forbidden")):
                    with self.assertRaises(type(error)) as caught:
                        await pipeline._groq_chat_json(object(), [{"role": "user", "content": "Offline"}])
                self.assertIs(caught.exception, error)
                self.assertEqual(fake.await_count, 1)

    async def test_rate_limit_upgrade_link_does_not_block_billing_or_free_rotation(self):
        with tempfile.TemporaryDirectory(prefix="sample_quota_", dir=WORK) as scratch:
            root = Path(scratch).resolve()
            self.assertTrue(root.is_relative_to(WORK.resolve()))
            db = Database(root / "data")
            for identity in ("a", "b"):
                await db.groq_keys.insert_one({"id": identity, "key": "gsk_OFFLINE_FAKE_" + identity,
                                                "free_tier_confirmed": True})
            calls = []

            async def handler(request):
                calls.append(request)
                if len(calls) == 1:
                    return httpx.Response(429, headers={"retry-after": "61"}, json={"error": {
                        "code": "rate_limit_exceeded", "type": "tokens", "message":
                        "Rate limit reached. Please try again in 61s. "
                        "Visit https://console.groq.com/settings/organization/billing to upgrade."}})
                return httpx.Response(200, json={"usage": {"total_tokens": 5}, "choices": []})

            real_client = httpx.AsyncClient
            transport = httpx.MockTransport(handler)
            with patch.object(groq_pool, "_LOCK", asyncio.Lock()), patch.object(
                    httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=transport, **kw)):
                response = await groq_pool.request(db, "/chat/completions", json_body={
                    "model": "qwen/qwen3.8-27b", "messages": [{"role": "user", "content": "Offline"}], "max_tokens": 20})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(calls), 2)
            keys = await db.groq_keys.find({}).to_list(None)
            self.assertTrue(all(not key.get("billing_blocked") for key in keys))
            states = await db.groq_usage.find({}).to_list(None)
            self.assertTrue(any(state.get("blocks") for state in states), "429 must remain a quota cooldown")


class SourceContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.line = "Прятки в CS2: люди стали предметами"
        self.evidence = "играем в прятки в CS2: люди превращаются в предметы"
        self.segments = [
            {"start": 0, "end": 8, "text": "Промокод ПЛАТИ1000 даёт рекламный приз."},
            {"start": 12, "end": 28, "text": "Сейчас играем в прятки в CS2: люди превращаются в предметы."},
            {"start": 55, "end": 65, "text": "Поздний секрет НЕ_ВО_ВВЕДЕНИИ меняет весь исход."},
        ]
        self.stages, self.saved = {}, []
        self.chat = AsyncMock(return_value={"context_line": self.line, "evidence": self.evidence})
        self.network_guard = patch.object(pipeline.httpx, "AsyncClient", side_effect=AssertionError("Live HTTP forbidden"))
        self.network_guard.start()

    async def asyncTearDown(self):
        self.network_guard.stop()

    async def save(self, key, value):
        self.saved.append((key, value))

    async def context(self, **kwargs):
        return await pipeline.source_context(self.segments, self.stages, self.save, self.chat,
                                             video_title="Прятки в CS2", banned=[(0, 8)], **kwargs)

    async def test_grounded_intro_omits_ads_and_late_text_and_reuses_cache(self):
        self.assertEqual(await self.context(), self.line)
        prompt = json.dumps(self.chat.await_args.args[0], ensure_ascii=False)
        self.assertIn(self.evidence, prompt)
        self.assertNotIn("ПЛАТИ1000", prompt)
        self.assertNotIn("НЕ_ВО_ВВЕДЕНИИ", prompt)
        self.assertEqual(await self.context(), self.line)
        self.chat.assert_awaited_once()
        self.assertEqual(len(self.saved), 1)

    async def test_unsupported_quote_short_quote_new_numbers_and_long_line_cache_empty(self):
        for response in [
            {"context_line": self.line, "evidence": "Этой цитаты нет во введении"},
            {"context_line": self.line, "evidence": "CS2"},
            {"context_line": "Прятки: победитель получает 1000", "evidence": self.evidence},
            {"context_line": "А" * 49, "evidence": self.evidence},
            {"context_line": "", "evidence": ""},
        ]:
            with self.subTest(response=response):
                self.stages, self.saved = {}, []
                self.chat = AsyncMock(return_value=response)
                self.assertEqual(await self.context(), "")
                self.assertEqual(await self.context(), "")
                self.chat.assert_awaited_once()
                self.assertEqual(len(self.saved), 1, "An invalid answer is cached empty, not repeatedly requested")

    async def test_intro_change_invalidates_cache_but_late_change_does_not(self):
        await self.context()
        self.segments[-1]["text"] = "Другой поздний итог; это не введение."
        await self.context()
        self.assertEqual(self.chat.await_count, 1)
        self.segments[1]["text"] += " Правила выбираются до начала раунда."
        await self.context()
        self.assertEqual(self.chat.await_count, 2)

    async def test_cached_premise_cannot_gain_unsupported_numbers(self):
        await self.context()
        next(iter(self.stages.values()))["context_line"] = "Прятки: приз 1000"
        with self.assertRaises(RuntimeError):
            await self.context()
        self.chat.assert_awaited_once()

    async def test_cached_unmatched_quote_is_rejected_and_not_hidden_by_retry(self):
        await self.context()
        next(iter(self.stages.values()))["evidence"] = "Отсутствующая цитата из другого видео"
        with self.assertRaises(RuntimeError):
            await self.context()
        self.chat.assert_awaited_once()

    async def test_quota_failure_propagates_without_saving_a_false_premise(self):
        deferred = groq_pool.GroqDeferred(9999999999, "offline quota")
        self.chat.side_effect = deferred
        with self.assertRaises(groq_pool.GroqDeferred) as caught:
            await self.context()
        self.assertIs(caught.exception, deferred)
        self.assertEqual(self.stages, {})
        self.assertEqual(self.saved, [])

    async def test_empty_or_fully_banned_intro_never_calls_model(self):
        for segments, banned in [([], []), (self.segments[:1], [(0, 8)])]:
            with self.subTest(segments=segments):
                value = await pipeline.source_context(segments, {}, self.save, self.chat, banned=banned)
                self.assertEqual(value, "")
        self.chat.assert_not_awaited()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Local FFmpeg unavailable")
class SampleMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sample_", dir=WORK)
        self.folder = Path(self.temporary.name).resolve()
        self.network_guard = patch.object(pipeline.httpx, "AsyncClient", side_effect=AssertionError("Live HTTP forbidden"))
        self.network_guard.start()

    def tearDown(self):
        self.network_guard.stop()
        if not self.folder.is_relative_to(WORK.resolve()) or not self.folder.name.startswith("sample_"):
            raise AssertionError("Refusing scratch cleanup outside sample workspace")
        self.temporary.cleanup()

    async def test_focus_keeps_large_square_gameplay_from_y440_to_y1520(self):
        # Horizontal colour bands and white top/bottom edges make the actual
        # foreground geometry measurable after the real FFmpeg composition.
        fixture = self.folder / "source.png"
        image = Image.new("RGB", (480, 270), "green")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 479, 89), fill=(240, 5, 5))
        draw.rectangle((0, 90, 479, 179), fill=(5, 240, 5))
        draw.rectangle((0, 180, 479, 269), fill=(5, 5, 240))
        draw.rectangle((0, 0, 479, 3), fill="white")
        draw.rectangle((0, 266, 479, 269), fill="white")
        image.save(fixture)
        source, output = self.folder / "source.mp4", self.folder / "focus.mp4"
        await local_run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(fixture), "-t", "1",
                         "-r", "10", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)])
        await pipeline.render_clip(source, self.folder / "absent.ass", output, 1, background="focus", crop_x=.5)
        metadata = await pipeline._probe(output)
        video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (1080, 1920))
        self.assertAlmostEqual(float(metadata["format"]["duration"]), 1, delta=.15)
        frame = self.folder / "focus.png"
        await local_run(["ffmpeg", "-v", "error", "-y", "-ss", "0.4", "-i", str(output), "-frames:v", "1", str(frame)])
        with Image.open(frame) as actual:
            for y in (444, 1515):
                self.assertGreater(min(actual.getpixel((540, y))[:3]), 220, f"Foreground edge missing at y={y}")
            red = actual.getpixel((540, 700))
            blue = actual.getpixel((540, 1200))
            self.assertGreater(red[0], red[1] + 100)
            self.assertGreater(blue[2], blue[1] + 100)

    async def test_cover_at_selects_requested_frame_and_invalidates_only_changed_cache(self):
        source = self.folder / "two-phases.mp4"
        await local_run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                         "color=c=red:s=270x480:r=10:d=0.5", "-f", "lavfi", "-i",
                         "color=c=blue:s=270x480:r=10:d=0.5", "-filter_complex",
                         "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264",
                         "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)])
        calls = []

        async def recorded(command, **kwargs):
            calls.append(command)
            return await local_run(command, **kwargs)

        kwargs = {"title": "Поворот в конце боя", "banner_text": "Поворот в конце боя", "caption": "Фактический исход. #игры #shorts",
                  "profile_name": "Момент дня", "out_dir": self.folder, "clip_id": "cover_sample", "run": recorded,
                  "fonts_dir": BACKEND / "fonts"}
        first = await ready_pack.build_package(source, cover_at=.1, **kwargs)
        with Image.open(self.folder / first["cover_filename"]) as image:
            early = image.getpixel((540, 1000))
        first_manifest = json.loads((self.folder / "cover_sample_ready.json").read_text(encoding="utf-8"))
        calls.clear()
        second = await ready_pack.build_package(source, cover_at=.8, **kwargs)
        with Image.open(self.folder / second["cover_filename"]) as image:
            late = image.getpixel((540, 1000))
        second_manifest = json.loads((self.folder / "cover_sample_ready.json").read_text(encoding="utf-8"))
        self.assertGreater(early[0], early[2] + 100)
        self.assertGreater(late[2], late[0] + 100)
        self.assertNotEqual(first_manifest["signature"], second_manifest["signature"])
        self.assertTrue(any("-filter_complex" in command for command in calls), "Changed cover_at must rebuild package")
        self.assertTrue(second["ready_for_publish"])
        calls.clear()
        third = await ready_pack.build_package(source, cover_at=.8, **kwargs)
        self.assertEqual(second, third)
        self.assertTrue(calls)
        self.assertTrue(all(Path(command[0]).stem == "ffprobe" for command in calls), "Same cover_at must not re-encode")


if __name__ == "__main__":
    unittest.main()
