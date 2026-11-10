"""Persistent selection and release queue for the local autopilot."""

import asyncio
import math
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from groq_pool import NoGroqKeys
from trend_sources import fresh, scan_sources
from hype_scout import scan_vods
from source_history import excluded_source_ids

DEFAULT_CONFIG = {
    "enabled": True, "hour": 10, "minute": 0, "time_zone": "Asia/Qyzylorda",
    "daily_target": 6, "max_source_videos": 4, "config_version": 2,
    "continuous": True, "fresh_hours": 36, "post_interval_minutes": 90, "chat_analysis": True,
}
FINISHED = {"done", "exhausted", "skipped"}
JOB_FINISHED = {"done", "review", "error", "cancelled"}
_RUN_LOCK = asyncio.Lock()


class _DiscoveryUnavailable(RuntimeError):
    pass


def _tz(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Qyzylorda":
            return timezone(timedelta(hours=5), name)
        if name == "UTC":
            return timezone.utc
        raise ValueError("Неизвестный часовой пояс") from None


def _number(value, fallback=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else fallback
    except (TypeError, ValueError):
        return fallback


def _config(raw):
    raw = dict(raw or {})
    if "local_time" in raw:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw["local_time"]))
        if not match:
            raise ValueError("Время должно иметь вид ЧЧ:ММ")
        raw["hour"], raw["minute"] = map(int, match.groups())
    if "timezone" in raw:
        raw["time_zone"] = raw["timezone"]
    if "max_sources_per_day" in raw:
        raw["max_source_videos"] = raw["max_sources_per_day"]
    result = {**DEFAULT_CONFIG, **{k: v for k, v in raw.items() if k in DEFAULT_CONFIG}}
    result["hour"], result["minute"] = int(result["hour"]), int(result["minute"])
    if not 0 <= result["hour"] <= 23 or not 0 <= result["minute"] <= 59:
        raise ValueError("Время запуска вне допустимого диапазона")
    result["time_zone"] = str(result["time_zone"])
    _tz(result["time_zone"])
    result["enabled"] = bool(result["enabled"])
    result["daily_target"] = max(1, min(30, int(
        result["daily_target"] if raw.get("config_version") == 2 else DEFAULT_CONFIG["daily_target"]
    )))
    result["config_version"] = 2
    result["max_source_videos"] = max(1, min(15, int(result["max_source_videos"])))
    result["continuous"] = bool(result["continuous"])
    result["chat_analysis"] = bool(result["chat_analysis"])
    result["fresh_hours"] = max(6, min(96, int(result["fresh_hours"])))
    result["post_interval_minutes"] = max(15, min(720, int(result["post_interval_minutes"])))
    return result


def releases_for(run):
    if "releases" in run:
        return run["releases"]
    if run.get("status") == "done" and run.get("output_clip_id"):
        return [{"clip_id": run["output_clip_id"], "day": run.get("output_day"),
                 "ready_at": run.get("completed_at")}]
    return []


class Autopilot:
    def __init__(self, db, enqueue_callback, *, scan=None, now=None, poll_interval=30):
        self.db, self.enqueue_callback = db, enqueue_callback
        self.scan = scan or scan_sources
        self.now = now or time.time
        self.poll_interval = max(.01, float(poll_interval))
        self.worker = None
        self.wake = asyncio.Event()
        self.last_error = ""

    def _now(self):
        value = self.now()
        if isinstance(value, datetime):
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        return float(value)

    async def _get_config(self):
        settings = await self.db.app_settings.find_one({"id": "main"}) or {}
        return _config(settings.get("autopilot"))

    async def start(self):
        if self.worker and not self.worker.done():
            return
        config = await self._get_config()
        await self.db.app_settings.update_one({"id": "main"}, {"$set": {"autopilot": config}}, upsert=True)
        self.worker = asyncio.create_task(self._loop())

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
            self.worker = None

    async def update(self, config):
        async with _RUN_LOCK:
            merged = _config({**await self._get_config(), **dict(config or {})})
            await self.db.app_settings.update_one({"id": "main"}, {"$set": {"autopilot": merged}}, upsert=True)
        self.wake.set()
        return await self.status()

    async def run_now(self):
        await self._tick(force=True)
        self.wake.set()
        return await self.status()

    async def _loop(self):
        while True:
            self.wake.clear()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = self._safe_error(exc)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _safe_error(exc):
        return re.sub(r"gsk_[\w-]+", "[скрытый ключ]", str(exc))[:400]

    async def _save(self, run):
        run["updated_at"] = self._now()
        await self.db.autopilot_runs.update_one({"id": run["id"]}, {"$set": run}, upsert=True)

    @staticmethod
    def _counts(runs, day):
        attempts = [a for run in runs for a in run.get("attempts", [])
                    if a.get("charge_day") == day and a.get("charged", True)]
        clips = {release["clip_id"] for run in runs for release in releases_for(run)
                 if release.get("day") == day}
        return {"day": day, "sources_started": len(attempts), "outputs": len(clips)}

    async def _counters(self, day):
        runs = await self.db.autopilot_runs.find({}).to_list(None)
        counts = self._counts(runs, day)
        await self.db.app_settings.update_one({"id": "main"}, {"$set": {"autopilot_counters": counts}}, upsert=True)
        return counts

    async def _find_job(self, attempt):
        if attempt.get("job_id"):
            return await self.db.jobs.find_one({"id": attempt["job_id"]})
        # These tags must be on the job at insert time, not written afterwards.
        for job in await self.db.jobs.find({}).to_list(None):
            if (job.get("autopilot_attempt_id") or (job.get("params") or {}).get("autopilot_attempt_id")) == attempt["id"]:
                return job
        return None

    async def _ready_clips(self, job, run, limit):
        threshold = _number(run.get("quality_threshold"), 75)
        clips = await self.db.clips.find({"job_id": job["id"]}).to_list(None)
        ready = {clip["id"]: clip for clip in clips if clip.get("ready_for_publish") is True
                 and not clip.get("needs_review")
                 and _number(clip.get("quality_score"), -1) >= threshold}
        return [ready[clip_id] for clip_id in job.get("clip_ids", []) if clip_id in ready][:limit]

    async def _observe(self, run, day, config, counts):
        """True means wait/finish; False permits another source in this run."""
        attempts = run.get("attempts", [])
        if not attempts or attempts[-1].get("observed"):
            return False
        attempt = attempts[-1]
        job = await self._find_job(attempt)
        if not job:
            if attempt.get("job_id"):
                attempt.update(status="deleted", observed=True)
                run["current_job_id"] = None
                await self._save(run)
                return False
            return False  # A persisted enqueuing attempt is replayed idempotently.
        attempt.update(job_id=job["id"], charged=True, status=job.get("status", "queued"))
        run["current_job_id"] = job["id"]
        if job.get("status") not in JOB_FINISHED:
            run["status"] = "waiting_quota" if job.get("status") == "waiting_quota" else "running"
            run["next_attempt_at"] = job.get("next_attempt_at")
            await self._save(run)
            return True
        remaining = max(0, config["daily_target"] - counts["outputs"])
        clips = await self._ready_clips(job, run, remaining) if job.get("status") == "done" else []
        if clips:
            ready_at = self._now()
            releases = run.setdefault("releases", [])
            releases.extend({"clip_id": clip["id"], "day": day, "ready_at": ready_at} for clip in clips)
            done = len(clips) >= remaining or run["id"] != day
            attempt.update(status="released", observed=True)
            run.update(status="done" if done else "selecting", completed_at=ready_at,
                       current_job_id=None, last_error="")
            await self._save(run)
            await self._counters(day)
            return True
        error = str(job.get("last_error") or job.get("status_text") or "")
        if job.get("status") == "error" and any(word in error.lower() for word in (
                "подтверд", "бесплатного аккаунта", "nogroqkeys", "платные сервисы отключены")):
            run.update(status="needs_setup", last_error="Подтвердите бесплатный Groq-аккаунт и повторите остановленную задачу.")
            await self._save(run)
            return True
        attempt.update(status="no_qualifying_clip", observed=True)
        run.update(current_job_id=None, status="selecting")
        await self._save(run)
        return False

    async def _discover(self, run, profiles, now, config=None):
        config = config or DEFAULT_CONFIG
        excluded = await excluded_source_ids(self.db)
        # During this run, move on to a different candidate after a failure.
        excluded.update(attempt.get("video_id") for attempt in run.get("attempts", [])
                        if attempt.get("job_id") or attempt.get("charged"))
        source_config = [
            [profile["id"], profile.get("channels", []), profile.get("twitch_channels", []),
             (profile.get("settings") or {}).get("twitch_trending") is True,
             (profile.get("settings") or {}).get("youtube_trending") is True]
            for profile in profiles
        ]
        if run.get("source_config") != source_config or now >= run.get("next_scan_at", 0):
            pool, failures = [], 0
            try:
                pool = await self.scan(self.db, profiles, now)
            except Exception:
                failures += 1
            try:
                pool += await scan_vods(self.db, profiles, now, fresh_hours=config["fresh_hours"],
                                        use_chat=config["chat_analysis"])
            except Exception:
                failures += 1
            if failures == 2:
                raise _DiscoveryUnavailable("Площадки не ответили на проверку источников. Повторим поиск позже.")
            warming = any(item["video"].get("trend_status") == "collecting" for item in pool)
            run.update(candidate_pool=pool, source_config=source_config,
                       next_scan_at=self._now() + (900 if warming else 1800))
            await self._save(run)
        valid_profiles = {profile["id"]: profile for profile in profiles}
        candidates = [item for item in run.get("candidate_pool", [])
                      if item["profile_id"] in valid_profiles
                      and (item["video"].get("source_kind") == "vod" or fresh(item["video"], now))
                      and item["video"]["video_id"] not in excluded
                      and item["video"].get("trend_ready") is True
                      and _number(item["video"].get("trend_score")) > 0]
        candidates.sort(key=lambda item: (item["video"].get("source_kind") != "vod", -item["video"]["trend_score"],
                                           -(item["video"].get("views_per_hour") or 0),
                                           item["video"]["video_id"], item["profile_id"]))
        if candidates:
            best = candidates[0]
            return valid_profiles[best["profile_id"]], best["video"]
        return None

    async def _dispatch(self, run, attempt, profile, video, day):
        attempt.update(status="enqueuing", charged=True, charge_day=day)
        run.update(status="enqueuing", selected_profile_id=profile["id"],
                   quality_threshold=_number((profile.get("settings") or {}).get("quality_threshold"), 75))
        await self._save(run)  # Reservation precedes the possibly interrupted callback.
        await self._counters(day)
        tagged = {**video, "autopilot_day": run["id"], "autopilot_attempt_id": attempt["id"],
                  "requested_clips": max(1, attempt.get("requested_clips", 1))}
        try:
            job = await self.enqueue_callback(profile, tagged)
        except asyncio.CancelledError:
            raise
        except NoGroqKeys as exc:
            attempt.update(status="needs_setup", charged=False)
            run.update(status="needs_setup", last_error=self._safe_error(exc))
            await self._save(run)
            await self._counters(day)
            return
        except Exception as exc:
            # The callback may have inserted a job before its response failed.
            # Keep the same attempt id and replay only that id on the next tick.
            run.update(status="enqueue_error", last_error=self._safe_error(exc), retry_at=self._now() + 60)
            await self._save(run)
            return
        if not isinstance(job, dict) or not job.get("id"):
            raise RuntimeError("Autopilot enqueue_callback должен вернуть сохранённую задачу с id")
        attempt.update(job_id=job["id"], status=job.get("status", "queued"))
        run.update(current_job_id=job["id"], status="running", last_error="", retry_at=0)
        await self._save(run)

    async def _tick(self, force=False):
        # The per-author evening run replaces the old global autopilot when it is on.
        if ((await self.db.app_settings.find_one({"id": "main"})) or {}).get("author_daily", {}).get("enabled"):
            return
        async with _RUN_LOCK:
            config, now = await self._get_config(), self._now()
            local = datetime.fromtimestamp(now, _tz(config["time_zone"]))
            day = local.date().isoformat()
            due = local.replace(hour=config["hour"], minute=config["minute"], second=0, microsecond=0).timestamp()
            runs = await self.db.autopilot_runs.find({}).to_list(None)
            counts = self._counts(runs, day)
            today = next((item for item in runs if item["id"] == day), None)
            if today and "releases" not in today:
                today["releases"] = releases_for(today)
                if today.get("status") == "exhausted" and counts["sources_started"] < config["max_source_videos"]:
                    today.update(status="selecting", retry_at=0)
                await self._save(today)
            # A carried-over quota wait owns the single global analysis slot.
            active = sorted((r for r in runs if r.get("status") not in FINISHED), key=lambda r: r["id"])
            run = active[0] if active else today
            if run and run.get("status") not in FINISHED:
                if await self._observe(run, day, config, counts):
                    await self._counters(day)
                    return
                if run["id"] != day:
                    run.update(status="skipped", last_error="Прошлый день завершён без готового клипа; старые дни не накапливаются.")
                    await self._save(run)
                    run = None
            if counts["outputs"] >= config["daily_target"]:
                if run and run.get("status") not in FINISHED:
                    run.update(status="done", completed_at=now, last_error="")
                    await self._save(run)
                return
            if run and run.get("status") in {"exhausted", "skipped"}:
                return
            if not force and (not config["enabled"] or (now < due and not config["continuous"])):
                return
            if run is None:
                run = {"id": day, "day": day, "status": "selecting", "started_at": now, "attempts": [], "last_error": ""}
                await self._save(run)
            if run.get("retry_at", 0) > now and not force:
                return
            profiles = []
            for profile in await self.db.profiles.find({}).to_list(None):
                settings = profile.get("settings") or {}
                if settings.get("daily_enabled", True) and (
                    profile.get("channels") or settings.get("youtube_trending")
                    or settings.get("twitch_trending") and profile.get("twitch_channels")
                ):
                    profiles.append(profile)
            if not profiles:
                run.update(status="needs_setup", last_error="Добавьте YouTube-канал или включите Twitch в профиле.")
                await self._save(run)
                return
            previous = run["attempts"][-1] if run["attempts"] else None
            if previous and not previous.get("job_id") and previous.get("status") in {"enqueuing", "needs_setup"}:
                profile = next((p for p in profiles if p["id"] == previous["profile_id"]), None)
                if profile and fresh(previous["video"], now):
                    await self._dispatch(run, previous, profile, previous["video"], day)
                    return
                previous.update(status="expired", charged=False, observed=True)
                await self._save(run)
            counts = await self._counters(day)
            if counts["sources_started"] >= config["max_source_videos"] or sum(a.get("charged", True) for a in run["attempts"]) >= config["max_source_videos"]:
                run.update(status="exhausted", last_error="Дневной лимит источников достигнут; слабый клип не создаётся.")
                await self._save(run)
                return
            try:
                candidate = await self._discover(run, profiles, now, config)
            except _DiscoveryUnavailable as exc:
                run.update(status="discovery_error", last_error=str(exc), retry_at=now + 60)
                await self._save(run)
                return
            if candidate is None:
                run.update(status="waiting_trend", last_error="Ждём новые записи стримов и хайповые клипы — проверка каждые 15–30 минут.",
                           retry_at=run.get("next_scan_at", now + 900))
                await self._save(run)
                return
            profile, video = candidate
            attempt = {"id": f"autopilot:{day}:{len(run['attempts']) + 1}", "profile_id": profile["id"],
                       "video_id": video["video_id"], "video": video, "started_at": now, "charge_day": day, "charged": True,
                       "requested_clips": config["daily_target"] - counts["outputs"]}
            run.update(source_provider=video["provider"], source_views_per_hour=video["views_per_hour"],
                       source_trend=f"{video['trend_score']:.0f}/100", source_published_at=video["published_at"])
            if "releases" not in run:
                run["releases"] = releases_for(run)
            run["attempts"].append(attempt)
            await self._dispatch(run, attempt, profile, video, day)

    async def status(self):
        config, now = await self._get_config(), self._now()
        local = datetime.fromtimestamp(now, _tz(config["time_zone"]))
        day = local.date().isoformat()
        runs = await self.db.autopilot_runs.find({}).sort("id", -1).to_list(None)
        today = next((r for r in runs if r["id"] == day), None)
        active = sorted((r for r in runs if r.get("status") not in FINISHED), key=lambda r: r["id"])
        current = active[0] if active else today
        last = current or (runs[0] if runs else None)
        due = local.replace(hour=config["hour"], minute=config["minute"], second=0, microsecond=0)
        if today and today.get("status") in FINISHED:
            due += timedelta(days=1)
        elif due.timestamp() < now:
            due = local
        return {**config, "status": "disabled" if not config["enabled"] else (current.get("status") if current else "scheduled"),
                "next_run_at": due.timestamp(), "last_run": last,
                "last_error": (last or {}).get("last_error") or self.last_error,
                "counters": self._counts(runs, day), "running": bool(self.worker and not self.worker.done())}
