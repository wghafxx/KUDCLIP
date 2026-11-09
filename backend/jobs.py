"""A single persistent local worker. Quota waits do not occupy the worker."""
import asyncio
import time
from datetime import datetime, timezone

import groq_pool

TERMINAL = {'done', 'error', 'cancelled', 'review'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self, db, process):
        self.db, self.process = db, process
        self.worker = None
        self.current = {}
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.stopping = False

    async def start(self):
        if self.worker and not self.worker.done():
            return
        self.stopping = False
        # Work can be resumed because transcript, editorial stages and completed
        # clips are persisted independently of the task's current status.
        for job in await self.db.jobs.find({}).to_list(None):
            if job.get('status') not in TERMINAL | {'queued', 'waiting_quota'}:
                await self.db.jobs.update_one({'id': job['id']}, {'$set': {
                    'status': 'queued', 'status_text': 'Продолжение после перезапуска…', 'updated_at': stamp()}})
        self.worker = asyncio.create_task(self._loop())

    async def stop(self):
        if self.worker:
            self.stopping = True
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
            self.worker = None

    def notify(self):
        self.wake.set()

    async def cancel(self, job_id):
        async with self.lock:
            job = await self.db.jobs.find_one({'id': job_id})
            if not job:
                return
            await self.db.jobs.update_one({'id': job_id}, {'$set': {
                'status': 'cancelled', 'status_text': 'Отменено', 'updated_at': stamp()}})
            task = self.current.get(job_id)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.notify()

    async def retry(self, job_id):
        job = await self.db.jobs.find_one({'id': job_id})
        if not job:
            return None
        if job.get('status') not in TERMINAL | {'waiting_quota'}:
            return job
        await self.db.jobs.update_one({'id': job_id}, {'$set': {
            'status': 'queued', 'status_text': 'Повторная попытка…', 'next_attempt_at': 0,
            'updated_at': stamp(), 'last_error': ''}})
        self.notify()
        return await self.db.jobs.find_one({'id': job_id})

    async def _loop(self):
        while True:
            self.wake.clear()
            items = await self.db.jobs.find({}).sort('created_at', 1).to_list(None)
            ready = next((j for j in items if j.get('status') in {'queued', 'waiting_quota'}
                          and float(j.get('next_attempt_at') or 0) <= time.time()), None)
            if not ready:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                continue
            job_id = ready['id']
            task = asyncio.create_task(self.process(self.db, job_id))
            self.current[job_id] = task
            try:
                await task
            except groq_pool.GroqDeferred as exc:
                await self.db.jobs.update_one({'id': job_id}, {'$set': {
                    'status': 'waiting_quota', 'next_attempt_at': exc.retry_at,
                    'status_text': 'Ожидание бесплатной квоты: ' + exc.reason,
                    'updated_at': stamp()}})
            except asyncio.CancelledError:
                job = await self.db.jobs.find_one({'id': job_id})
                if job and job.get('status') != 'cancelled':
                    await self.db.jobs.update_one({'id': job_id}, {'$set': {
                        'status': 'queued', 'status_text': 'Продолжится при следующем запуске', 'updated_at': stamp()}})
                # A cancelled child must not terminate the queue itself.
                if self.stopping:
                    raise
            except Exception as exc:
                await self.db.jobs.update_one({'id': job_id}, {'$set': {
                    'status': 'error', 'status_text': str(exc)[:400], 'last_error': str(exc)[:400], 'updated_at': stamp()}})
            finally:
                self.current.pop(job_id, None)
