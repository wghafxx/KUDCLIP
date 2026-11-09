"""Scheduler coverage with fixed clocks and public metadata fixtures."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autopilot import Autopilot
from filedb import Database


class TrendSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.folder.cleanup)
        self.db = Database(Path(self.folder.name))
        self.now = datetime(2026, 11, 9, 6, tzinfo=timezone.utc).timestamp()
        self.scan = AsyncMock()
        self.scheduler = Autopilot(self.db, self.enqueue, scan=self.scan, now=lambda: self.now)
        await self.db.profiles.insert_one({"id": "profile", "channels": ["https://www.youtube.com/@user"]})

    def candidate(self, video_id, age=3600, speed=320, status="eligible", score=20):
        return {"profile_id": "profile", "video": {
            "video_id": video_id, "url": "https://youtu.be/" + video_id, "provider": "youtube",
            "published_at": self.now - age, "trend_status": status, "views_per_hour": speed,
            "trend_score": score, "trend_ready": status == "eligible",
        }}

    async def enqueue(self, profile, video):
        job = {"id": video["video_id"], "status": "queued", "profile_id": profile["id"],
               "video_url": video["url"], "autopilot_attempt_id": video["autopilot_attempt_id"],
               "requested": video["requested_clips"]}
        await self.db.jobs.insert_one(job)
        return job

    async def complete(self, video_id, scores):
        ids = []
        for index, score in enumerate(scores):
            clip_id = f"{video_id}_{index}"
            ids.append(clip_id)
            await self.db.clips.insert_one({"id": clip_id, "job_id": video_id, "ready_for_publish": True,
                                            "quality_score": score})
        await self.db.jobs.update_one({"id": video_id}, {"$set": {"status": "done", "clip_ids": ids}})
        await self.scheduler._tick()
        return ids

    async def test_measured_only_and_release_up_to_three_across_sources(self):
        self.scan.return_value = [
            self.candidate("bbbbbbbbbbb"),
            self.candidate("aaaaaaaaaaa", score=70),
            self.candidate("ccccccccccc", age=48 * 3600, score=100),
            self.candidate("ddddddddddd", speed=None, score=None, status="collecting"),
        ]
        await self.scheduler._tick()
        first = await self.db.jobs.find_one({"id": "aaaaaaaaaaa"})
        self.assertEqual(first["requested"], 3)
        ids = await self.complete("aaaaaaaaaaa", [94, 80, 88])
        await self.scheduler._tick()
        self.assertEqual((await self.db.jobs.find_one({"id": "bbbbbbbbbbb"}))["requested"], 1)
        extra = await self.complete("bbbbbbbbbbb", [91])
        await self.scheduler._tick()
        status = await self.scheduler.status()
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["counters"]["outputs"], 3)
        self.assertEqual(status["counters"]["sources_started"], 2)
        self.assertEqual([r["clip_id"] for r in status["last_run"]["releases"]], [ids[0], ids[2], extra[0]])
        self.assertEqual(self.scan.await_count, 1)

    async def test_waits_for_second_measurement_and_rescans(self):
        self.scan.return_value = [self.candidate("aaaaaaaaaaa", speed=None, score=None, status="collecting")]
        await self.scheduler._tick()
        self.assertEqual((await self.scheduler.status())["status"], "waiting_trend")
        self.assertEqual(await self.db.jobs.find({}).to_list(None), [])
        self.now += 901
        self.scan.return_value = [self.candidate("aaaaaaaaaaa")]
        await self.scheduler._tick()
        self.assertTrue(await self.db.jobs.find_one({"id": "aaaaaaaaaaa"}))

    async def test_v1_target_migrates_but_manual_one_is_kept(self):
        await self.db.app_settings.insert_one({"id": "main", "autopilot": {"daily_target": 1}})
        self.assertEqual((await self.scheduler.status())["daily_target"], 3)
        await self.scheduler.update({"daily_target": 1})
        self.assertEqual((await self.scheduler.status())["daily_target"], 1)
