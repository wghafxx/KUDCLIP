"""Prepare clips from the newest YouTube upload and Twitch archive for each author."""

import asyncio
import logging
import re
import time
from datetime import datetime

import hype_scout
from source_history import excluded_source_ids
from stream_analysis import AUTHOR_DURATION_LIMITS

logger = logging.getLogger(__name__)
DEFAULTS = {"enabled": True, "hour": 19, "minute": 0, "fresh_hours": 36, "clips_per_source": 2,
            "youtube_clips": 6, "twitch_clips": 8,
            "post_interval_minutes": 60, "chat_analysis": True}
FINISHED = {"done", "review", "error", "cancelled"}
_WORD = re.compile(r"[а-яёa-z0-9]{3,}", re.I)


def config_of(raw):
    merged = {**DEFAULTS, **{k: v for k, v in (raw or {}).items() if k in DEFAULTS}}
    merged["hour"] = max(0, min(23, int(merged["hour"])))
    merged["minute"] = max(0, min(59, int(merged["minute"])))
    merged["fresh_hours"] = max(6, min(96, int(merged["fresh_hours"])))
    merged["clips_per_source"] = max(1, min(2, int(merged["clips_per_source"])))
    merged["youtube_clips"] = max(4, min(8, int(merged["youtube_clips"])))
    merged["twitch_clips"] = max(7, min(10, int(merged["twitch_clips"])))
    merged["post_interval_minutes"] = max(20, min(720, int(merged["post_interval_minutes"])))
    return merged


def _words(text):
    return {w.lower() for w in _WORD.findall(str(text or ""))}


def same_episode(a, b):
    first, second = _words(a), _words(b)
    if len(first) < 8 or len(second) < 8:
        return False
    return len(first & second) / min(len(first), len(second)) >= 0.5


def timecode_link(platform, url, seconds):
    seconds = int(seconds)
    if platform == "youtube":
        return f"{url}&t={seconds}s"
    if "/videos/" in url:
        return f"{url}?t={seconds // 3600}h{seconds % 3600 // 60}m{seconds % 60}s"
    return url


def platform_texts(clip, author, source_url):
    title = " ".join(str(clip.get("title") or "Момент дня").split())[:90]
    tags = " ".join(clip.get("hashtags") or ())
    author_tag = "#" + re.sub(r"\W+", "", author.lower()) if author else ""
    return {
        "youtube": {"title": f"{title} #shorts"[:100],
                    "caption": f"{title}\n\nОригинал: {author} — {source_url}\n\n{tags} {author_tag}".strip()},
        "tiktok": {"title": title, "caption": f"{title} {tags} {author_tag}".strip()[:2200]},
    }


class AuthorDaily:
    def __init__(self, db, enqueue, *, now=time.time, poll=60):
        self.db, self.enqueue, self.now, self.poll = db, enqueue, now, poll
        self.worker = None
        self.wake = asyncio.Event()
        self.force = False
        self.reserve_only = False
        self.lock = asyncio.Lock()
        self.last_error = ""

    async def config(self):
        settings = await self.db.app_settings.find_one({"id": "main"}) or {}
        return config_of(settings.get("author_daily"))

    async def update(self, patch):
        merged = config_of({**await self.config(), **patch})
        await self.db.app_settings.update_one({"id": "main"}, {"$set": {"author_daily": merged}}, upsert=True)
        self.wake.set()
        return await self.status()

    async def start(self):
        await self.db.app_settings.update_one({"id": "main"}, {"$set": {"author_daily": await self.config()}}, upsert=True)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._loop())

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await self.worker
            self.worker = None

    async def run_now(self, *, reserve_only=False):
        self.force = True
        self.reserve_only = reserve_only
        self.wake.set()
        return await self.status()

    async def _loop(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Author daily run failed")
                self.last_error = str(exc)[:300]
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.poll)
            except asyncio.TimeoutError:
                pass

    def _day(self):
        return datetime.fromtimestamp(self.now()).astimezone()

    async def tick(self):
        async with self.lock:
            config, local = await self.config(), self._day()
            day = local.date().isoformat()
            due = local.replace(hour=config["hour"], minute=config["minute"], second=0, microsecond=0)
            force, reserve_only = self.force, self.reserve_only
            self.force, self.reserve_only = False, False
            if config["enabled"]:
                for profile in await self.db.profiles.find({}).to_list(None):
                    if (profile.get("settings") or {}).get("daily_enabled", True) is False:
                        continue
                    existing = await self.db.author_days.find_one({"id": f"{day}:{profile['id']}"})
                    if existing and existing.get("status") == "planning":
                        await self._plan(profile, day, config, existing)
                    elif (local >= due or force) and (not existing or force and existing.get("status") == "nothing"):
                        await self._plan(profile, day, config, existing, reserve_only=reserve_only)
            for row in await self.db.author_days.find({"status": "processing"}).to_list(None):
                await self._collect(row, config)

    async def _plan(self, profile, day, config, previous=None, *, reserve_only=False):
        resuming = previous and previous.get("status") == "planning"
        scan_attempt = (previous or {}).get("scan_attempt", 0)
        if previous and not resuming:
            scan_attempt += 1
        row = {"id": f"{day}:{profile['id']}", "profile_id": profile["id"], "day": day, "author": profile.get("name", ""),
               "status": "planning", "sources": [], "releases": [], "reserve": [], "skipped": [],
               "started_at": previous["started_at"] if resuming else self.now(),
               "finished_at": None, "scan_attempt": scan_attempt,
               "reserve_only": previous.get("reserve_only", False) if resuming else reserve_only}
        await self.db.author_days.update_one({"id": row["id"]}, {"$set": row}, upsert=True)
        used = await excluded_source_ids(self.db, profile["id"])
        login = next((hype_scout.login_of(c) for c in profile.get("twitch_channels") or () if hype_scout.login_of(c)), "")
        channel = next(iter(profile.get("channels") or ()), "")
        for platform, ready in (("twitch", bool(login)), ("youtube", bool(channel))):
            source = {"platform": platform, "status": "none"}
            attempt_id = f"author:{day}:{profile['id']}:{platform}"
            if scan_attempt:
                attempt_id += f":{scan_attempt}"
            # After an interruption the selection may already have reached the job queue.
            job = await self.db.jobs.find_one({"autopilot_attempt_id": attempt_id}) if resuming else None
            if job:
                video = job.get("source_video")
                if not video:
                    source.update(status="error", error="В сохранённой задаче нет данных об исходнике")
            elif not ready:
                video = None
                source["reason"] = "Источник не указан в карточке автора"
            else:
                try:
                    now = self.now()
                    if platform == "twitch":
                        video = await hype_scout.twitch_pick(self.db, login, now, fresh_hours=config["fresh_hours"],
                                                             use_chat=config["chat_analysis"], exclude=used)
                    else:
                        video = await hype_scout.youtube_pick(channel, now, fresh_hours=config["fresh_hours"], exclude=used)
                    if not video:
                        max_hours = AUTHOR_DURATION_LIMITS[platform] // 3600
                        source["reason"] = f"За {config['fresh_hours']} ч нет нового исходника длительностью до {max_hours} ч"
                except Exception as exc:
                    video = None
                    source.update(status="error", error=f"Площадка не ответила: {str(exc)[:200]}")
            if video:
                target = ((job or {}).get("params") or {}).get("num_clips") or config[f"{platform}_clips"]
                source.update(video_id=video["video_id"], url=video["url"], title=video["title"], reason=video["reason"],
                              requested_clips=target,
                              timecodes=[{"start": w["start"], "end": w["end"], "label": w["title"],
                                          "link": timecode_link(platform, video["url"], w["start"])} for w in video["windows"]])
                try:
                    if not job:
                        tagged = {**video, "author_stream": True, "autopilot_day": day,
                                  "autopilot_attempt_id": attempt_id, "requested_clips": target}
                        job = await self.enqueue(profile, tagged)
                    source.update(status="processing", job_id=job["id"])
                except Exception as exc:
                    source.update(status="error", error=str(exc)[:300])
            row["sources"].append(source)
        row["status"] = "processing" if any(s["status"] == "processing" for s in row["sources"]) else "nothing"
        await self.db.author_days.update_one({"id": row["id"]}, {"$set": row})

    async def _recent_episodes(self, profile_id, exclude_day):
        texts = []
        for row in await self.db.author_days.find({"profile_id": profile_id}).to_list(None):
            if row.get("day") == exclude_day or row.get("started_at", 0) < self.now() - 14 * 86400:
                continue
            for release in (row.get("releases") or []) + (row.get("reserve") or []):
                clip = await self.db.clips.find_one({"id": release["clip_id"]})
                if clip:
                    texts.append(clip.get("subtitle_text") or "")
        return texts

    async def _collect(self, row, config):
        pending = False
        candidates = []
        for source in row["sources"]:
            if source.get("status") not in {"processing", "rendered"}:
                continue
            job = await self.db.jobs.find_one({"id": source["job_id"]})
            if not job:
                source.update(status="error", error="Задача монтажа удалена")
                continue
            if job.get("status") not in FINISHED:
                pending = True
                continue
            if job.get("status") in {"error", "cancelled"}:
                source.update(status="error", error=str(job.get("last_error") or job.get("status_text") or "Монтаж не завершён")[:300])
                continue
            clips = [c for c in await self.db.clips.find({"job_id": job["id"]}).to_list(None)
                     if c.get("ready_for_publish") is True and not c.get("needs_review")]
            clips.sort(key=lambda c: -float(c.get("quality_score") or 0))
            source.update(status="rendered", clip_ids=[c["id"] for c in clips])
            target = source.get("requested_clips") or config["clips_per_source"]
            candidates += [(source, clip) for clip in clips[:target]]
            if not clips:
                source["status"] = "done"
                source["reason"] = (source.get("reason") or "") + " · Достойных моментов не нашлось — публикация пропущена"
        if pending:
            await self.db.author_days.update_one({"id": row["id"]}, {"$set": {"sources": row["sources"]}})
            return
        profile = await self.db.profiles.find_one({"id": row["profile_id"]}) or {}
        seen = await self._recent_episodes(row["profile_id"], row["day"])
        kept, skipped = [], []
        for source, clip in sorted(candidates, key=lambda item: -float(item[1].get("quality_score") or 0)):
            text = clip.get("subtitle_text") or ""
            if any(same_episode(text, other) for other in seen):
                skipped.append({"clip_id": clip["id"], "title": clip.get("title"), "reason": "Этот эпизод уже был в другом ролике"})
                continue
            seen.append(text)
            kept.append((source, clip))
        now = self.now()
        releases, reserve, scheduled = [], [], {}
        for source, clip in kept:
            texts = platform_texts(clip, profile.get("name", ""), source["url"])
            await self.db.clips.update_one({"id": clip["id"]}, {"$set": {"platform_texts": texts, "author_day": row["id"]}})
            platform = source["platform"]
            entry = {"clip_id": clip["id"], "day": row["day"], "ready_at": now, "platform": platform,
                     "title": clip.get("title"), "start": clip.get("start"), "end": clip.get("end"),
                     "quality": clip.get("quality_score")}
            source["prepared_clips"] = source.get("prepared_clips", 0) + 1
            if not row.get("reserve_only") and scheduled.get(platform, 0) < config["clips_per_source"]:
                scheduled[platform] = scheduled.get(platform, 0) + 1
                releases.append(entry)
            else:
                reserve.append(entry)
        for source in row["sources"]:
            if source.get("status") == "rendered":
                source["status"] = "done"
        status = "publishing" if releases else "ready" if reserve else "nothing"
        row.update(sources=row["sources"], skipped=skipped, status=status,
                   finished_at=now, releases=releases, reserve=reserve)
        await self.db.author_days.update_one({"id": row["id"]}, {"$set": row})

    async def status(self):
        config, local = await self.config(), self._day()
        due = local.replace(hour=config["hour"], minute=config["minute"], second=0, microsecond=0)
        return {**config, "time_zone": str(local.tzinfo), "next_run": due.strftime("%H:%M"), "last_error": self.last_error}

    async def reports(self, limit_days=7):
        rows = await self.db.author_days.find({}).sort("id", -1).to_list(limit_days * 15)
        posts = await self.db.browser_posts.find({}).to_list(None)
        by_clip = {}
        for post in posts:
            if post.get("scope"):
                by_clip.setdefault(post.get("clip_id"), []).append(
                    {"platform": post.get("platform"), "status": post.get("status"), "url": post.get("url", ""),
                     "message": post.get("message", "")})
        for row in rows:
            for release in row.get("releases") or []:
                release["posts"] = by_clip.get(release["clip_id"], [])
        return rows
