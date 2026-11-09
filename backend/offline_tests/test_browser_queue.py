"""Browser queue tests against a temporary file database and fake upload forms."""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_posting import BrowserPosting, POST_INTERVAL, PostingBlocked, _public_views
from browser_upload import DISCLOSURE_UNAVAILABLE
from filedb import Database


class FakeBrowser:
    def __init__(self):
        self.accounts = {"youtube": "channel-bound", "tiktok": "@profile-bound"}
        self.attempts = []
        self.uploads = []
        self.publishes = []
        self.disclosures = []
        self.before_upload = None
        self.interrupt_at = None
        self.missing_disclosure = False

    async def account(self, platform, config):
        return self.accounts[platform]

    async def publish(self, platform, config, path, clip, account, checkpoint):
        self.attempts.append((platform, account, path.name, clip["caption"]))
        if "ad_disclosure" in clip:
            self.disclosures.append((platform, clip["ad_disclosure"]))
        if self.accounts[platform] != account:
            raise PostingBlocked("В браузере открыт другой аккаунт.")
        if self.before_upload:
            await self.before_upload(platform)
        await checkpoint("uploading")
        self.uploads.append(platform)
        if self.interrupt_at == "uploading":
            raise asyncio.CancelledError
        if self.missing_disclosure:
            raise PostingBlocked(DISCLOSURE_UNAVAILABLE)
        await checkpoint("publishing")
        self.publishes.append(platform)
        if self.interrupt_at == "publishing":
            raise asyncio.CancelledError
        return "https://example.invalid/published"

    async def close(self):
        pass


class BrowserQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        work = Path(os.environ.get("KUDCLIP_TEST_WORKDIR", str(Path.cwd() / "work"))).resolve()
        work.mkdir(parents=True, exist_ok=True)
        self.work = work
        self.temp = tempfile.TemporaryDirectory(prefix="browser-queue-test-", dir=work)
        self.root = Path(self.temp.name).resolve()
        self.clips = self.root / "clips"
        self.clips.mkdir()
        self.clock = datetime(2026, 11, 8, 7, tzinfo=timezone.utc).timestamp()
        self.day = "2026-11-08"
        self.reopen()

    async def asyncTearDown(self):
        await self.posting.stop()
        if self.root.parent != self.work or not self.root.name.startswith("browser-queue-test-"):
            raise AssertionError("Temporary cleanup escaped the browser queue test workspace")
        self.temp.cleanup()

    def reopen(self):
        self.db = Database(self.root / "data")
        self.browser = FakeBrowser()
        self.posting = BrowserPosting(self.db, self.clips, self.browser, now=lambda: self.clock)

    async def enable(self, *platforms):
        for platform in platforms:
            await self.posting.bind(platform)
            await self.posting.enable(platform, True)

    async def clip(self, clip_id, *, ready=True, review=False, **extra):
        name = f"{clip_id}.mp4"
        (self.clips / name).write_bytes(b"offline test upload fixture")
        await self.db.clips.update_one({"id": clip_id}, {"$set": {
            "id": clip_id, "video_filename": name, "title": f"Release {clip_id}",
            "caption": "Fallback caption", "publication_caption": "Final publication caption",
            "ready_for_publish": ready, "needs_review": review, **extra,
        }}, upsert=True)

    async def release(self, clip_id, *, day=None, completed_at=None, status="done"):
        day = day or self.day
        await self.db.autopilot_runs.insert_one({
            "id": f"{day}:{clip_id}", "output_day": day, "status": status,
            "output_clip_id": clip_id, "completed_at": self.clock if completed_at is None else completed_at,
        })

    async def releases(self, entries, *, status="selecting", **legacy):
        await self.db.autopilot_runs.insert_one({"id": "multi-release-run", "status": status,
            "releases": [{"clip_id": clip_id, "day": self.day, "ready_at": ready_at} for clip_id, ready_at in entries],
            **legacy})

    async def post(self, platform, day=None, clip_id=None):
        query = {"platform": platform, "day": day or self.day}
        if clip_id is not None:
            query["clip_id"] = clip_id
        return await self.db.browser_posts.find_one(query)

    async def test_only_daily_releases_completed_after_enabling_are_uploaded(self):
        await self.clip("manual")
        await self.clip("old-today")
        await self.clip("yesterday")
        await self.release("old-today", completed_at=self.clock - 1)
        await self.release("yesterday", day="2026-11-07", completed_at=self.clock - 86400)
        await self.enable("youtube")
        self.clock += 10
        await self.clip("unfinished")
        await self.release("unfinished", status="running")
        await self.posting.tick()
        self.assertEqual(self.browser.attempts, [])

        await self.clip("new")
        await self.release("new")
        await self.posting.tick()
        self.assertEqual(self.browser.attempts, [
            ("youtube", "channel-bound", "new.mp4", "Final publication caption"),
        ])
        self.assertEqual((await self.post("youtube"))["status"], "published")
        self.assertIsNone(await self.post("tiktok"))

    async def test_requires_ready_release_without_manual_review(self):
        await self.enable("youtube")
        self.clock += 1
        await self.clip("daily", ready=False)
        await self.release("daily")
        await self.posting.tick()
        await self.clip("daily", review=True)
        await self.posting.tick()
        self.assertEqual(self.browser.attempts, [])
        self.assertIsNone(await self.post("youtube"))
        await self.clip("daily")
        await self.posting.tick()
        self.assertEqual(self.browser.uploads, ["youtube"])

    async def test_wrong_account_blocks_only_its_platform_and_retry_uploads_once(self):
        await self.enable("youtube", "tiktok")
        self.browser.accounts["youtube"] = "different-channel"
        self.clock += 1
        await self.clip("daily")
        await self.release("daily")
        await self.posting.tick()
        self.assertEqual(self.browser.uploads, ["tiktok"])
        self.assertEqual((await self.post("youtube"))["status"], "blocked")
        self.assertEqual((await self.post("tiktok"))["status"], "published")

        self.reopen()
        await self.posting.tick()
        self.assertEqual(self.browser.attempts, [])
        await self.posting.retry("youtube")
        await self.posting.tick()
        await self.posting.tick()
        self.assertEqual(self.browser.uploads, ["youtube"])
        self.assertEqual((await self.post("youtube"))["status"], "published")
        with self.assertRaises(PostingBlocked):
            await self.posting.retry("youtube")

    async def test_interrupted_upload_blocks_duplicate_and_other_platform_can_publish_after_restart(self):
        await self.enable("youtube", "tiktok")
        self.clock += 1
        await self.clip("daily")
        await self.release("daily")
        self.browser.interrupt_at = "uploading"
        with self.assertRaises(asyncio.CancelledError):
            await self.posting.tick()
        self.assertEqual(self.browser.uploads, ["youtube"])
        self.assertEqual((await self.post("youtube"))["status"], "uploading")

        self.reopen()
        await self.posting.tick()
        await self.posting.tick()
        self.assertEqual(self.browser.uploads, ["tiktok"])
        self.assertEqual((await self.post("youtube"))["status"], "needs_review")
        self.assertEqual((await self.post("tiktok"))["status"], "published")
        with self.assertRaises(PostingBlocked):
            await self.posting.retry("youtube")

    async def test_interrupted_publish_is_recovered_after_midnight_even_if_disabled(self):
        await self.enable("youtube")
        self.clock += 1
        await self.clip("daily")
        await self.release("daily")
        self.browser.interrupt_at = "publishing"
        with self.assertRaises(asyncio.CancelledError):
            await self.posting.tick()
        self.assertEqual(self.browser.publishes, ["youtube"])
        self.assertEqual((await self.post("youtube"))["status"], "publishing")
        await self.posting.enable("youtube", False)

        self.clock = datetime(2026, 11, 8, 19, tzinfo=timezone.utc).timestamp()
        self.reopen()
        await self.posting.tick()
        self.assertEqual((await self.posting.status())["day"], "2026-11-09")
        self.assertEqual((await self.post("youtube"))["status"], "needs_review")
        self.assertEqual(self.browser.attempts, [])

    async def test_restart_and_next_day_cannot_repeat_the_same_clip_per_platform(self):
        await self.enable("youtube", "tiktok")
        self.clock += 1
        await self.clip("daily")
        await self.release("daily")
        await self.posting.tick()
        self.assertEqual(self.browser.uploads, ["youtube", "tiktok"])
        self.reopen()
        await self.posting.tick()
        self.assertEqual(self.browser.attempts, [])

        self.clock += 86400
        await self.release("daily", day="2026-11-09")
        await self.posting.tick()
        self.assertEqual(self.browser.attempts, [])
        self.clock += 1
        await self.clip("next")
        await self.release("next", day="2026-11-09")
        await self.posting.tick()
        self.assertEqual(self.browser.uploads, ["youtube", "tiktok"])
        self.assertEqual((await self.post("youtube", "2026-11-09"))["clip_id"], "next")

    async def test_disabling_in_preflight_prevents_file_attachment(self):
        await self.enable("youtube", "tiktok")
        self.clock += 1
        await self.clip("daily")
        await self.release("daily")

        async def pause_current(platform):
            if platform == "youtube":
                await self.posting.enable(platform, False)

        self.browser.before_upload = pause_current
        await self.posting.tick()
        self.assertEqual(self.browser.uploads, ["tiktok"])
        self.assertEqual((await self.post("youtube"))["status"], "blocked")
        self.assertEqual((await self.post("tiktok"))["status"], "published")

    async def test_disabling_second_platform_during_first_skips_its_browser_entirely(self):
        await self.enable("youtube", "tiktok")
        self.clock += 1
        await self.clip("daily")
        await self.release("daily")

        async def pause_other(platform):
            if platform == "youtube":
                await self.posting.enable("tiktok", False)

        self.browser.before_upload = pause_other
        await self.posting.tick()
        self.assertEqual([attempt[0] for attempt in self.browser.attempts], ["youtube"])
        self.assertEqual(self.browser.uploads, ["youtube"])
        self.assertIsNone(await self.post("tiktok"))

    async def test_retry_rejects_release_older_than_current_enable_or_day(self):
        await self.enable("youtube")
        self.browser.accounts["youtube"] = "different-channel"
        self.clock += 1
        await self.clip("daily")
        await self.release("daily")
        await self.posting.tick()
        self.assertEqual((await self.post("youtube"))["status"], "blocked")
        self.clock += 1
        await self.posting.enable("youtube", False)
        await self.posting.enable("youtube", True)
        with self.assertRaises(PostingBlocked):
            await self.posting.retry("youtube")
        self.clock += 86400
        with self.assertRaises(PostingBlocked):
            await self.posting.retry("youtube")
        self.assertEqual((await self.post("youtube"))["status"], "blocked")
        self.assertEqual(self.browser.uploads, [])

    async def test_new_releases_are_published_fifo_with_independent_90_minute_spacing(self):
        await self.enable("youtube", "tiktok")
        self.clock += 10
        for clip_id in ("first", "second", "third"):
            await self.clip(clip_id)
        await self.releases([("third", self.clock), ("first", self.clock - 5), ("second", self.clock - 2)],
                            status="waiting_quota")
        await self.posting.tick()
        self.assertEqual([attempt[2] for attempt in self.browser.attempts], ["first.mp4", "first.mp4"])
        first = await self.post("youtube", clip_id="first")
        self.assertEqual(first["id"], f"youtube:{self.day}:first")
        self.assertEqual(first["published_at"], self.clock)
        self.clock += POST_INTERVAL - 1
        await self.posting.tick()
        self.assertEqual(len(self.browser.attempts), 2)
        self.clock += 1
        await self.posting.tick()
        await self.posting.tick()
        self.assertEqual([attempt[2] for attempt in self.browser.attempts],
                         ["first.mp4", "first.mp4", "second.mp4", "second.mp4"])
        self.clock += POST_INTERVAL
        await self.posting.tick()
        self.assertEqual([attempt[2] for attempt in self.browser.attempts][-2:], ["third.mp4", "third.mp4"])

    async def test_legacy_record_is_preserved_and_only_suppresses_its_clip(self):
        await self.enable("youtube")
        self.clock += 10
        for clip_id in ("legacy", "new"):
            await self.clip(clip_id)
        await self.releases([("legacy", self.clock - 1), ("new", self.clock)])
        key = f"youtube:{self.day}"
        old = {"id": key, "platform": "youtube", "day": self.day, "clip_id": "legacy", "status": "published",
               "account": "channel-bound", "updated_at": self.clock - POST_INTERVAL}
        await self.db.browser_posts.insert_one(old)
        await self.posting.tick()
        self.assertEqual([attempt[2] for attempt in self.browser.attempts], ["new.mp4"])
        self.assertEqual(await self.db.browser_posts.find_one({"id": key}), old)
        self.assertEqual((await self.post("youtube", clip_id="new"))["id"], f"youtube:{self.day}:new")

    async def test_legacy_completion_does_not_make_a_pre_enable_release_new(self):
        await self.clip("early")
        prepared_at = self.clock - 1
        await self.enable("youtube")
        self.clock += 2
        await self.releases([("early", prepared_at)], status="done", output_day=self.day,
                            output_clip_id="early", completed_at=self.clock)
        await self.posting.tick()
        self.assertEqual(self.browser.attempts, [])

    async def test_uncertain_clip_is_not_repeated_and_original_checkpoint_controls_spacing(self):
        await self.enable("youtube")
        self.clock += 10
        await self.clip("first")
        await self.clip("next")
        await self.releases([("first", self.clock - 1), ("next", self.clock)])
        self.browser.interrupt_at = "uploading"
        with self.assertRaises(asyncio.CancelledError):
            await self.posting.tick()
        started_at = self.clock
        self.clock += POST_INTERVAL - 1
        self.reopen()
        await self.posting.tick()
        old = await self.post("youtube", clip_id="first")
        self.assertEqual(old["status"], "needs_review")
        self.assertEqual(old["last_attempt_at"], started_at)
        self.assertEqual(self.browser.attempts, [])
        self.clock += 1
        await self.posting.tick()
        self.assertEqual([attempt[2] for attempt in self.browser.attempts], ["next.mp4"])
        with self.assertRaises(PostingBlocked):
            await self.posting.retry("youtube")

    async def test_ads_stop_before_browser_and_retry_selects_oldest_eligible_blocked(self):
        await self.enable("youtube")
        self.clock += 10
        await self.clip("ad", ad_kind="banner")
        await self.clip("retry-first")
        await self.clip("retry-next")
        await self.releases([("ad", self.clock - 2), ("retry-first", self.clock - 1), ("retry-next", self.clock)])
        await self.posting.tick()
        ad = await self.post("youtube", clip_id="ad")
        self.assertEqual(ad["status"], "blocked")
        self.assertIs(ad["retry_allowed"], False)
        self.assertIn("вручную с отметкой о рекламе", ad["message"])
        self.assertEqual(self.browser.attempts, [])
        self.browser.accounts["youtube"] = "wrong-account"
        await self.posting.tick()
        await self.posting.tick()
        self.assertEqual((await self.post("youtube", clip_id="retry-first"))["status"], "blocked")
        self.assertEqual((await self.post("youtube", clip_id="retry-next"))["status"], "blocked")
        self.browser.accounts["youtube"] = "channel-bound"
        await self.posting.retry("youtube")
        await self.posting.tick()
        self.assertEqual([attempt[2] for attempt in self.browser.attempts],
                         ["retry-first.mp4", "retry-next.mp4", "retry-first.mp4"])
        self.assertEqual(self.browser.uploads, ["youtube"])

    async def test_own_brand_banner_uses_tiktok_disclosure_and_stays_manual_on_youtube(self):
        await self.enable("youtube", "tiktok")
        self.clock += 1
        await self.clip("banner", ad_kind="banner", ad_disclosure="own")
        await self.releases([("banner", self.clock)])
        await self.posting.tick()
        self.assertEqual(self.browser.disclosures, [("tiktok", "own")])
        self.assertEqual(self.browser.uploads, ["tiktok"])
        self.assertEqual((await self.post("youtube"))["status"], "blocked")
        self.assertEqual((await self.post("tiktok"))["ad_disclosure"], "own")
        self.assertEqual((await self.post("tiktok"))["status"], "published")

    async def test_sponsored_banner_reaches_both_forms_and_missing_disclosure_prevents_retry(self):
        await self.enable("youtube", "tiktok")
        self.clock += 1
        await self.clip("banner", ad_kind="banner", ad_disclosure="sponsored")
        await self.releases([("banner", self.clock)])
        self.browser.missing_disclosure = True
        await self.posting.tick()
        await self.posting.tick()
        self.assertEqual(self.browser.disclosures, [("youtube", "sponsored"), ("tiktok", "sponsored")])
        self.assertEqual(self.browser.publishes, [])
        for platform in ("youtube", "tiktok"):
            post = await self.post(platform)
            self.assertEqual(post["status"], "needs_review")
            self.assertEqual(post["message"], DISCLOSURE_UNAVAILABLE)
            with self.assertRaises(PostingBlocked):
                await self.posting.retry(platform)

    async def test_legacy_video_ad_stays_manual_even_if_disclosure_type_is_present(self):
        await self.enable("youtube", "tiktok")
        self.clock += 1
        await self.clip("legacy-ad", has_ad=True, ad_kind="video", ad_disclosure="sponsored")
        await self.releases([("legacy-ad", self.clock)])
        await self.posting.tick()
        self.assertEqual(self.browser.attempts, [])
        self.assertEqual((await self.post("youtube"))["status"], "blocked")
        self.assertEqual((await self.post("tiktok"))["status"], "blocked")

    async def record_public_post(self, record_id, platform, url, age):
        published_at = self.clock - age
        await self.db.browser_posts.insert_one({"id": record_id, "platform": platform, "clip_id": record_id,
                                               "status": "published", "url": url, "published_at": published_at,
                                               "updated_at": published_at})
        return published_at

    async def test_view_measurements_store_actual_zero_and_do_not_backfill_missed_window(self):
        youtube = "https://youtu.be/aB12cDE_3fG"
        tiktok = "https://www.tiktok.com/@artist/video/7281234567890123456"
        since = await self.record_public_post("yt", "youtube", youtube, 24 * 3600 + 60)
        await self.record_public_post("tt", "tiktok", tiktok, 48 * 3600 + 60)
        await self.record_public_post("unknown", "youtube", "https://example.invalid/video", 24 * 3600)
        reads = []

        async def view_reader(platform, url):
            reads.append((platform, url))
            return 0 if platform == "youtube" else 183

        self.posting.view_reader = view_reader
        await self.posting.measure_views()
        await self.posting.measure_views()
        self.assertEqual(reads, [("youtube", youtube), ("tiktok", tiktok)])
        yt = await self.db.browser_posts.find_one({"id": "yt"})
        tt = await self.db.browser_posts.find_one({"id": "tt"})
        self.assertEqual(yt["view_samples"], {"24h": {"views": 0, "checked_at": self.clock, "due_at": since + 24 * 3600}})
        self.assertEqual(yt["updated_at"], since)
        self.assertEqual(set(tt["view_samples"]), {"48h"})
        self.assertEqual(tt["view_samples"]["48h"]["views"], 183)

    async def test_unavailable_views_are_not_repeated_or_recorded_as_zero(self):
        await self.record_public_post("old", "youtube", "https://youtu.be/aB12cDE_3fG", 24 * 3600)
        calls = 0

        async def unavailable(platform, url):
            nonlocal calls
            calls += 1
            raise RuntimeError("No public metadata")

        self.posting.view_reader = unavailable
        await self.posting.measure_views()
        await self.posting.measure_views()
        record = await self.db.browser_posts.find_one({"id": "old"})
        self.assertEqual(calls, 1)
        self.assertNotIn("view_samples", record)
        self.assertEqual(record["view_checks"], {"24h": self.clock})

    async def test_slow_view_measurement_does_not_block_ready_release(self):
        await self.record_public_post("old", "youtube", "https://youtu.be/aB12cDE_3fG", 24 * 3600)
        await self.enable("youtube")
        self.clock += 2
        await self.clip("new")
        await self.releases([("new", self.clock)])
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_reader(platform, url):
            started.set()
            await release.wait()
            return 42

        self.posting.view_reader = slow_reader
        measurement = asyncio.create_task(self.posting.measure_views())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.wait_for(self.posting.tick(), timeout=2)
            self.assertFalse(measurement.done())
            self.assertEqual((await self.post("youtube", clip_id="new"))["status"], "published")
        finally:
            release.set()
            await measurement

    async def test_public_view_reader_matches_video_and_disables_cookies_and_download(self):
        target = "https://www.youtube.com/shorts/aB12cDE_3fG?feature=share"
        metadata = {"id": "aB12cDE_3fG", "view_count": 17, "availability": "public"}
        commands = []

        async def run(command, *, timeout):
            commands.append((command, timeout))
            return json.dumps(metadata)

        extractor = SimpleNamespace(YTDLP_CMD=["yt-dlp"], YTDLP_ARGS=[], _run=run)
        with patch.dict(sys.modules, {"pipeline": extractor}):
            self.assertEqual(await _public_views("youtube", target), 17)
            metadata["id"] = "different01"
            self.assertIsNone(await _public_views("youtube", target))
            metadata.update(id="aB12cDE_3fG", availability="unlisted")
            self.assertIsNone(await _public_views("youtube", target))
            self.assertIsNone(await _public_views("youtube", "https://youtube.com.example.invalid/watch?v=aB12cDE_3fG"))
        self.assertEqual(len(commands), 3)
        command, timeout = commands[0]
        self.assertTrue({"--no-cookies", "--no-cookies-from-browser", "--skip-download", "--ignore-config"}.issubset(command))
        self.assertEqual(command[-1], "https://www.youtube.com/watch?v=aB12cDE_3fG")
        self.assertEqual(timeout, 30)


if __name__ == "__main__":
    unittest.main()
