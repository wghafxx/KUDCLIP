"""A thermal trip aborts ongoing work and preserves the last job checkpoint."""

import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filedb import Database
import gpu_runtime
from jobs import JobManager


class ThermalSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        state = patch.multiple(gpu_runtime, _trip_reason=None, _temperature_seen=False)
        state.start()
        self.addCleanup(state.stop)

    async def test_exact_threshold_latches_and_never_falls_back_to_cpu(self):
        run = AsyncMock()
        with patch.object(gpu_runtime, '_temperature', side_effect=[69, 70, 30]) as sensor:
            self.assertTrue(await gpu_runtime.require_safe_temperature())
            for _ in range(2):
                with self.assertRaisesRegex(gpu_runtime.ThermalStop, '70 °C'):
                    await gpu_runtime.encode(run, ['ffmpeg', 'out.mp4'], 30)
        self.assertEqual(sensor.call_count, 2)
        run.assert_not_awaited()

    async def test_watchdog_stops_active_job_and_preserves_checkpoint(self):
        with TemporaryDirectory() as folder:
            db = Database(Path(folder))
            await db.jobs.insert_one({'id': 'hot', 'status': 'queued', 'created_at': '2026-01-01'})
            started = asyncio.Event()
            stopped = asyncio.Event()

            async def process(db, job_id):
                await db.jobs.update_one({'id': job_id}, {'$set': {'status': 'processing', 'completed_part': 5}})
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()

            manager = JobManager(db, process)
            await manager.start()
            await asyncio.wait_for(started.wait(), timeout=3)
            with patch.object(gpu_runtime, '_temperature', side_effect=[69, 70]):
                async def stop(reason):
                    await manager.stop(reason=reason)

                await asyncio.wait_for(gpu_runtime.monitor(stop, interval=0), timeout=3)
            job = await db.jobs.find_one({'id': 'hot'})
            self.assertTrue(stopped.is_set())
            self.assertEqual(job['status'], 'queued')
            self.assertEqual(job['completed_part'], 5)
            self.assertIn('70 °C', job['status_text'])

    async def test_unavailable_telemetry_prevents_gpu_use_and_trips_if_lost(self):
        with patch.object(gpu_runtime, '_temperature', side_effect=[None, 40, None]):
            self.assertFalse(await gpu_runtime.require_safe_temperature())
            self.assertTrue(await gpu_runtime.require_safe_temperature())
            with self.assertRaisesRegex(gpu_runtime.ThermalStop, 'датчиком'):
                await gpu_runtime.require_safe_temperature()
