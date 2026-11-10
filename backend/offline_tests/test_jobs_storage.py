"""Offline queue/storage/enqueue tests; never import the live server database.

Run from the work workspace, or set KUDCLIP_TEST_WORKDIR to its work directory.
All temporary data stays below that directory and is removed after each test.
"""

import ast
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import groq_pool
from filedb import Database, DataStoreError
from jobs import JobManager, yield_to_quota_waiter
from source_history import excluded_source_ids


def isolated_functions(filename, names, namespace):
    """Compile only requested functions: server/pipeline imports never execute."""
    tree = ast.parse((BACKEND / filename).read_text(encoding="utf-8"), filename)
    found = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names]
    if {node.name for node in found} != set(names):
        raise AssertionError("Requested functions missing from " + filename)
    for node in found:
        node.decorator_list = []
    exec(compile(ast.Module(body=found, type_ignores=[]), str(BACKEND / filename), "exec"), namespace)
    return namespace


class _TemporaryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.work_root = Path(os.environ.get("KUDCLIP_TEST_WORKDIR", str(Path.cwd() / "work"))).resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.scratch = Path(tempfile.mkdtemp(prefix="jobs-storage-test-", dir=self.work_root)).resolve()
        self.db = Database(self.scratch / "data")
        self.managers = []

    async def asyncTearDown(self):
        try:
            for manager in self.managers:
                await manager.stop()
        finally:
            # Explicit containment check before recursively removing test files.
            target = self.scratch.resolve()
            if target.parent != self.work_root or not target.name.startswith("jobs-storage-test-"):
                raise AssertionError("Refusing to remove a path outside the test work directory")
            shutil.rmtree(target)

    def manager(self, process):
        manager = JobManager(self.db, process)
        self.managers.append(manager)
        return manager

    async def job(self, job_id, status="queued", order="0", **fields):
        value = {"id": job_id, "profile_id": "profile", "status": status, "created_at": order,
                 "video_url": "https://youtu.be/abcdefghijk", "params": {}, **fields}
        await self.db.jobs.insert_one(value)
        return value

    async def wait_status(self, job_id, status):
        async def poll():
            while True:
                value = await self.db.jobs.find_one({"id": job_id})
                if value and value.get("status") == status:
                    return value
                await asyncio.sleep(.005)
        return await asyncio.wait_for(poll(), timeout=3)


class StorageTests(_TemporaryDatabaseTests):
    async def test_corrupt_json_blocks_read_and_write_without_overwriting(self):
        path = self.scratch / "data" / "profiles.json"
        broken = '[{"id": "original", "name": "not overwritten"}'
        path.write_text(broken, encoding="utf-8")
        with self.assertRaises(DataStoreError):
            await self.db.profiles.find_one({"id": "original"})
        with self.assertRaises(DataStoreError):
            await self.db.profiles.insert_one({"id": "new"})
        self.assertEqual(path.read_text(encoding="utf-8"), broken)
        self.assertFalse(path.with_suffix(".json.bak").exists())

    async def test_bad_collection_structure_is_rejected(self):
        path = self.scratch / "data" / "profiles.json"
        for content in ('{"id": "not-a-list"}', '[1, "not-an-object"]'):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(DataStoreError):
                await self.db.profiles.delete_many({})
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    async def test_backup_contains_previous_valid_version(self):
        await self.db.profiles.insert_one({"id": "original", "name": "первая версия"})
        await self.db.profiles.update_one({"id": "original"}, {"$set": {"name": "вторая версия"}})
        path = self.scratch / "data" / "profiles.json"
        backup = path.with_suffix(".json.bak")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))[0]["name"], "первая версия")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))[0]["name"], "вторая версия")
        path.write_text("broken JSON", encoding="utf-8")
        with self.assertRaises(DataStoreError):
            await self.db.profiles.insert_one({"id": "new"})
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))[0]["name"], "первая версия")

    async def test_read_os_error_does_not_become_empty_collection(self):
        await self.db.profiles.insert_one({"id": "original"})
        path = self.scratch / "data" / "profiles.json"
        before = path.read_bytes()
        with patch.object(Path, "read_text", side_effect=PermissionError("offline test")):
            with self.assertRaises(DataStoreError):
                await self.db.profiles.insert_one({"id": "new"})
        self.assertEqual(path.read_bytes(), before)

    async def test_unserializable_update_keeps_valid_main_file(self):
        await self.db.profiles.insert_one({"id": "original", "value": 1})
        path = self.scratch / "data" / "profiles.json"
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            await self.db.profiles.update_one({"id": "original"}, {"$set": {"value": float("nan")}})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((await self.db.profiles.find_one({"id": "original"}))["value"], 1)


class JobManagerTests(_TemporaryDatabaseTests):
    async def test_restart_recovers_inflight_only_and_keeps_quota_deadline(self):
        await self.job("interrupted", "rendering", "1")
        await self.job("reviewed", "review", "2")
        await self.job("completed", "done", "3")
        await self.job("failed", "error", "4")
        await self.job("cancelled", "cancelled", "5")
        future = 4102444800.0
        await self.job("quota", "waiting_quota", "6", next_attempt_at=future)
        called = []

        async def process(db, job_id):
            called.append(job_id)
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})

        manager = self.manager(process)
        await manager.start()
        await self.wait_status("interrupted", "done")
        self.assertEqual(called, ["interrupted"])
        quota = await self.db.jobs.find_one({"id": "quota"})
        self.assertEqual(quota["status"], "waiting_quota")
        self.assertEqual(quota["next_attempt_at"], future)

    async def test_quota_wait_does_not_occupy_worker(self):
        await self.job("quota", order="1")
        await self.job("ready", order="2")
        deadline = 4102444800.0
        called = []

        async def process(db, job_id):
            called.append(job_id)
            if job_id == "quota":
                raise groq_pool.GroqDeferred(deadline, "offline daily quota")
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})

        manager = self.manager(process)
        await manager.start()
        await self.wait_status("ready", "done")
        quota = await self.db.jobs.find_one({"id": "quota"})
        self.assertEqual(quota["status"], "waiting_quota")
        self.assertEqual(quota["next_attempt_at"], deadline)
        self.assertEqual(called, ["quota", "ready"])

    async def test_key_change_rechecks_persisted_quota_wait(self):
        await self.job("quota", "waiting_quota", next_attempt_at=4102444800.0, progress=18)

        async def process(db, job_id):
            job = await db.jobs.find_one({"id": job_id})
            self.assertEqual(job["progress"], 18)
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})

        manager = self.manager(process)
        await manager.start()
        await manager.recheck_quota()
        await self.wait_status("quota", "done")

    async def test_key_change_during_request_rechecks_the_same_job(self):
        await self.job("quota")
        waiting, allow_failure = asyncio.Event(), asyncio.Event()
        called = []

        async def process(db, job_id):
            called.append(job_id)
            if len(called) == 1:
                waiting.set()
                await allow_failure.wait()
                raise groq_pool.GroqDeferred(4102444800.0, "offline quota")
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})

        manager = self.manager(process)
        await manager.start()
        await asyncio.wait_for(waiting.wait(), timeout=3)
        await manager.recheck_quota()
        allow_failure.set()
        await self.wait_status("quota", "done")
        self.assertEqual(called, ["quota", "quota"])

    async def test_audio_checkpoint_yields_to_earlier_quota_job(self):
        await self.job("older", "waiting_quota", "1", next_attempt_at=4102444800.0)
        await self.job("audio", order="2")
        calls = []

        async def process(db, job_id):
            calls.append(job_id)
            job = await db.jobs.find_one({"id": job_id})
            if job_id == "audio" and not job.get("saved_part"):
                await db.jobs.update_one({"id": job_id}, {"$set": {"status": "transcribing", "saved_part": 1}})
                await manager.recheck_quota()
                await yield_to_quota_waiter(db, job)
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})

        manager = self.manager(process)
        await manager.start()
        job = await self.wait_status("audio", "done")
        self.assertEqual(calls, ["audio", "older", "audio"])
        self.assertEqual(job["saved_part"], 1)

    async def test_cancelling_active_job_stops_child_but_keeps_worker_alive(self):
        await self.job("active", order="1")
        await self.job("next", order="2")
        started, cancelled = asyncio.Event(), asyncio.Event()
        called = []

        async def process(db, job_id):
            called.append(job_id)
            if job_id == "active":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})

        manager = self.manager(process)
        await manager.start()
        await asyncio.wait_for(started.wait(), timeout=2)
        await manager.cancel("active")
        await self.wait_status("next", "done")
        self.assertTrue(cancelled.is_set())
        self.assertEqual((await self.db.jobs.find_one({"id": "active"}))["status"], "cancelled")
        self.assertEqual(called, ["active", "next"])
        self.assertFalse(manager.worker.done())

    async def test_shutdown_requeues_active_job_and_next_start_resumes_it(self):
        await self.job("resume")
        started = asyncio.Event()
        calls = 0

        async def process(db, job_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                await db.jobs.update_one({"id": job_id}, {"$set": {"status": "rendering"}})
                started.set()
                await asyncio.Event().wait()
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})

        manager = self.manager(process)
        await manager.start()
        await asyncio.wait_for(started.wait(), timeout=2)
        await manager.stop()
        self.assertEqual((await self.db.jobs.find_one({"id": "resume"}))["status"], "queued")
        await manager.start()
        await self.wait_status("resume", "done")
        self.assertEqual(calls, 2)

    async def test_queued_cancel_never_dispatches_and_retry_is_explicit(self):
        await self.job("cancel-first")
        called = []

        async def process(db, job_id):
            called.append(job_id)
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})

        manager = self.manager(process)
        await manager.cancel("cancel-first")
        await manager.start()
        await asyncio.sleep(.02)
        self.assertEqual(called, [])
        self.assertEqual((await manager.retry("cancel-first"))["status"], "queued")
        await self.wait_status("cancel-first", "done")
        self.assertEqual(called, ["cancel-first"])

    async def test_error_does_not_stop_following_jobs(self):
        await self.job("failure", order="1")
        await self.job("next", order="2")

        async def process(db, job_id):
            if job_id == "failure":
                raise RuntimeError("offline processing failure")
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})

        manager = self.manager(process)
        await manager.start()
        await self.wait_status("next", "done")
        failed = await self.db.jobs.find_one({"id": "failure"})
        self.assertEqual(failed["status"], "error")
        self.assertIn("offline processing failure", failed["last_error"])

    async def test_only_one_processing_job_at_a_time(self):
        for index in range(3):
            await self.job(str(index), order=str(index))
        active, maximum = 0, 0

        async def process(db, job_id):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(.01)
            await db.jobs.update_one({"id": job_id}, {"$set": {"status": "done"}})
            active -= 1

        manager = self.manager(process)
        await manager.start()
        await self.wait_status("2", "done")
        self.assertEqual(maximum, 1)


class EnqueueTests(_TemporaryDatabaseTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.notifications = 0

        def notify():
            self.notifications += 1

        extract = isolated_functions("pipeline.py", {"extract_video_id"}, {"re": re})["extract_video_id"]
        self.namespace = isolated_functions("server.py", {"_make_job", "_insert_job", "_autostart_profile", "normalized_settings", "delete_profile"}, {
            "asyncio": asyncio, "enqueue_lock": asyncio.Lock(), "db": self.db, "uuid": uuid,
            "pipeline": SimpleNamespace(extract_video_id=extract, CLIPS_DIR=self.scratch / "clips"),
            "manager": SimpleNamespace(notify=notify),
            "HTTPException": HTTPException,
            "ProfileSettings": lambda: SimpleNamespace(model_dump=lambda: {"review_before_send": True}),
            "now_iso": lambda: datetime.now(timezone.utc).isoformat(),
            "excluded_source_ids": excluded_source_ids,
        })
        self.profile = {"id": "profile", "name": "offline", "channels": ["https://youtube.invalid/channel"], "settings": {}}
        await self.db.profiles.insert_one(self.profile)

    async def make_job(self, url="https://youtu.be/abcdefghijk", **fields):
        return await self.namespace["_make_job"](self.profile, url, "offline source", "", 120, 2, 30, False, **fields)

    async def test_enqueue_does_not_mark_video_processed(self):
        job = await self.make_job()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(await self.db.processed.find({}).to_list(None), [])
        self.assertEqual(self.notifications, 1)

    async def test_concurrent_url_aliases_deduplicate_pending_job(self):
        jobs = await asyncio.gather(self.make_job(), self.make_job("https://www.youtube.com/watch?v=abcdefghijk"))
        self.assertEqual(jobs[0]["id"], jobs[1]["id"])
        self.assertEqual(len(await self.db.jobs.find({}).to_list(None)), 1)
        self.assertEqual(self.notifications, 1)

    async def test_failed_attempt_does_not_block_explicit_new_job(self):
        previous = await self.make_job()
        await self.db.jobs.update_one({"id": previous["id"]}, {"$set": {"status": "error"}})
        retry = await self.make_job()
        self.assertNotEqual(previous["id"], retry["id"])
        self.assertEqual(await self.db.processed.find({}).to_list(None), [])

    async def test_stale_profile_cannot_enqueue_after_profile_was_deleted(self):
        await self.db.profiles.delete_one({"id": self.profile["id"]})
        with self.assertRaises(HTTPException) as caught:
            await self.make_job()
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(await self.db.jobs.find({}).to_list(None), [])

    async def test_profile_deletion_serializes_against_concurrent_enqueue(self):
        previous = await self.make_job()
        cancellation_started, finish_cancellation = asyncio.Event(), asyncio.Event()

        async def cancel(job_id):
            cancellation_started.set()
            await finish_cancellation.wait()
            await self.db.jobs.update_one({"id": job_id}, {"$set": {"status": "cancelled"}})

        self.namespace["manager"].cancel = cancel
        deletion = asyncio.create_task(self.namespace["delete_profile"](self.profile["id"]))
        await asyncio.wait_for(cancellation_started.wait(), timeout=2)
        creation = asyncio.create_task(self.make_job("https://youtu.be/another1234"))
        try:
            await asyncio.sleep(.01)
            self.assertFalse(creation.done(), "Enqueue must wait for deletion's critical section")
        finally:
            finish_cancellation.set()
            await deletion
        with self.assertRaises(HTTPException) as caught:
            await asyncio.wait_for(creation, timeout=2)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIsNone(await self.db.jobs.find_one({"id": previous["id"]}))
        self.assertEqual(await self.db.jobs.find({}).to_list(None), [])

    async def test_autostart_recovers_legacy_failed_reservation_and_deduplicates_channels(self):
        failed = await self.make_job()
        await self.db.jobs.update_one({"id": failed["id"]}, {"$set": {"status": "error"}})
        await self.db.processed.insert_one({"profile_id": "profile", "video_id": "abcdefghijk"})
        video = {"video_id": "abcdefghijk", "url": "https://youtu.be/abcdefghijk", "title": "offline", "duration": 120, "views": 10, "thumbnail": ""}

        async def list_videos(url, limit):
            return [video]

        self.namespace["pipeline"].list_channel_videos = list_videos
        self.profile["channels"] = ["https://youtube.invalid/channel", "https://youtube.invalid/alias"]
        result = await self.namespace["_autostart_profile"](self.profile)
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(await self.db.jobs.find({}).to_list(None)), 2)
        # Creation never upgrades the legacy reservation to completed.
        self.assertFalse((await self.db.processed.find_one({"video_id": "abcdefghijk"})).get("completed", False))

    async def test_autostart_skips_successfully_completed_video(self):
        await self.db.processed.insert_one({"profile_id": "profile", "video_id": "abcdefghijk", "completed": True})
        video = {"video_id": "abcdefghijk", "url": "https://youtu.be/abcdefghijk", "title": "offline", "duration": 120, "views": 10, "thumbnail": ""}

        async def list_videos(url, limit):
            return [video]

        self.namespace["pipeline"].list_channel_videos = list_videos
        result = await self.namespace["_autostart_profile"](self.profile)
        self.assertEqual(result["created"], 0)
        self.assertEqual(await self.db.jobs.find({}).to_list(None), [])

    async def test_reset_unblocks_terminal_jobs_but_keeps_confirmed_publications(self):
        for identity, status in (("abcdefghijk", "done"), ("bbbbbbbbbbb", "done"),
                                 ("ccccccccccc", "waiting_quota"), ("ddddddddddd", "error")):
            job = await self.make_job(f"https://youtu.be/{identity}")
            await self.db.jobs.update_one({"id": job["id"]}, {"$set": {"status": status}})
            if identity == "bbbbbbbbbbb":
                await self.db.browser_posts.insert_one({"id": "youtube", "status": "published", "clip_id": job["id"] + "_0"})
            if identity == "ddddddddddd":
                await self.db.browser_posts.insert_one({"id": "tiktok", "status": "blocked", "clip_id": job["id"] + "_0"})
        await self.db.processed.insert_one({"profile_id": "profile", "video_id": "abcdefghijk", "completed": True})
        self.assertEqual(await excluded_source_ids(self.db, "profile"), {"abcdefghijk", "bbbbbbbbbbb", "ccccccccccc"})
        await self.db.processed.delete_many({"completed": True})
        self.assertEqual(await excluded_source_ids(self.db, "profile"), {"bbbbbbbbbbb", "ccccccccccc"})
        self.assertEqual(await excluded_source_ids(self.db, "other"), set())


class DeliveryTests(_TemporaryDatabaseTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.calls = []

        async def send(db, recipient, path, caption):
            self.calls.append(recipient)
            return {"ok": True}

        async def get_token(db):
            return "OFFLINE_NOT_A_REAL_TOKEN"

        self.namespace = isolated_functions("pipeline.py", {"DeliveryUnknown", "send_video_to_telegram", "deliver_clip", "_deliver_clip_locked"}, {
            "asyncio": asyncio, "httpx": httpx, "_DELIVERY_LOCKS": {},
            "CLIPS_DIR": self.scratch / "clips", "get_tg_token": get_token,
        })
        self.real_send = self.namespace["send_video_to_telegram"]
        self.namespace["send_video_to_telegram"] = send
        self.clip = {"id": "offline-clip", "filename": "never-opened.mp4", "caption": "offline",
                     "sent_to": [], "delivery_errors": {}, "delivery_inflight": []}
        await self.db.clips.insert_one(self.clip)

    async def deliver(self, recipients):
        # Intentionally keep this stale snapshot; the wrapper must re-read it.
        return await self.namespace["deliver_clip"](self.db, self.clip, recipients)

    async def test_partial_success_retries_only_failed_recipient(self):
        failed_once = False

        async def send(db, recipient, path, caption):
            nonlocal failed_once
            self.calls.append(recipient)
            if recipient == "b" and not failed_once:
                failed_once = True
                raise RuntimeError("Telegram explicitly rejected the recipient")
            return {"ok": True}

        self.namespace["send_video_to_telegram"] = send
        sent, errors = await self.deliver(["a", "b"])
        self.assertEqual(sent, ["a"])
        self.assertIn("b", errors)
        sent, errors = await self.deliver(["a", "b"])
        self.assertEqual(sent, ["b"])
        self.assertEqual(errors, {})
        self.assertEqual(self.calls, ["a", "b", "b"])
        self.assertEqual((await self.db.clips.find_one({"id": self.clip["id"]}))["sent_to"], ["a", "b"])

    async def test_other_recipient_success_preserves_unknown_delivery_marker(self):
        await self.db.clips.update_one({"id": self.clip["id"]}, {"$set": {"delivery_inflight": ["unknown-a"]}})
        sent, errors = await self.deliver(["unknown-a", "b"])
        self.assertEqual(sent, ["b"])
        self.assertIn("unknown-a", errors)
        current = await self.db.clips.find_one({"id": self.clip["id"]})
        self.assertEqual(current["delivery_inflight"], ["unknown-a"])
        await self.deliver(["unknown-a", "b"])
        self.assertEqual(self.calls, ["b"])

    async def test_concurrent_sends_of_same_clip_do_not_duplicate_upload(self):
        async def send(db, recipient, path, caption):
            self.calls.append(recipient)
            await asyncio.sleep(.02)
            return {"ok": True}

        self.namespace["send_video_to_telegram"] = send
        await asyncio.gather(self.deliver(["a"]), self.deliver(["a"]))
        self.assertEqual(self.calls, ["a"])

    async def test_timeout_remains_unknown_and_is_not_automatically_repeated(self):
        async def send(db, recipient, path, caption):
            self.calls.append(recipient)
            raise httpx.ReadTimeout("offline: remote acceptance unknown")

        self.namespace["send_video_to_telegram"] = send
        await self.deliver(["a"])
        await self.deliver(["a"])
        self.assertEqual(self.calls, ["a"])
        self.assertEqual((await self.db.clips.find_one({"id": self.clip["id"]}))["delivery_inflight"], ["a"])

    async def test_cancelled_upload_keeps_unknown_marker_after_restart(self):
        started = asyncio.Event()

        async def send(db, recipient, path, caption):
            self.calls.append(recipient)
            started.set()
            await asyncio.Event().wait()

        self.namespace["send_video_to_telegram"] = send
        first = asyncio.create_task(self.deliver(["a"]))
        await asyncio.wait_for(started.wait(), timeout=2)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.namespace["_DELIVERY_LOCKS"] = {}  # Simulate only process memory lost.
        await self.deliver(["a"])
        self.assertEqual(self.calls, ["a"])
        self.assertEqual((await self.db.clips.find_one({"id": self.clip["id"]}))["delivery_inflight"], ["a"])

    async def test_malformed_or_server_error_response_remains_unknown(self):
        folder = self.namespace["CLIPS_DIR"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / self.clip["filename"]).write_bytes(b"offline dummy video")
        self.namespace["send_video_to_telegram"] = self.real_send
        responses = [httpx.Response(200, text="malformed JSON"), httpx.Response(502, text="upstream error"),
                     httpx.Response(200, json={"unexpected": "missing ok receipt"})]
        real_client = httpx.AsyncClient
        calls = []

        async def handler(request):
            calls.append(request)
            return responses.pop(0)

        transport = httpx.MockTransport(handler)
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=transport, **kw)):
            for index in range(3):
                await self.db.clips.update_one({"id": self.clip["id"]}, {"$set": {
                    "delivery_inflight": [], "delivery_errors": {}, "sent_to": [],
                }})
                sent, errors = await self.deliver(["a"])
                self.assertEqual(sent, [])
                self.assertIn("a", errors)
                self.assertEqual((await self.db.clips.find_one({"id": self.clip["id"]}))["delivery_inflight"], ["a"])
                await self.deliver(["a"])
                self.assertEqual(len(calls), index + 1, "Unknown result must never trigger an automatic duplicate")


if __name__ == "__main__":
    unittest.main()
