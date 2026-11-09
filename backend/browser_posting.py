"""Durable delivery of new daily releases to a single bound account per platform."""
import asyncio
from contextlib import suppress
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from autopilot import DEFAULT_CONFIG, _tz
from browser_upload import AD_DISCLOSURES, DISCLOSURE_REQUIRED, URLS, BrowserSession, PostingBlocked, validate_debug_address

logger = logging.getLogger(__name__)
POST_INTERVAL = 90 * 60
VIEW_INTERVAL = 5 * 60
VIEW_GRACE = 2 * 60 * 60
VIEW_MILESTONES = {"24h": 24 * 3600, "48h": 48 * 3600}
UNCERTAIN_STAGES = {"uploading", "publishing"}
NON_REPEATABLE = UNCERTAIN_STAGES | {"needs_review", "published"}


def _timestamp(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else 0.0


def _public_video(platform, url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url or "")
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
    except (TypeError, ValueError):
        return None
    if platform == "youtube":
        if parsed.hostname == "youtu.be":
            video_id = parsed.path.strip("/")
        elif parsed.hostname in {"www.youtube.com", "youtube.com"}:
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith("/shorts/"):
                video_id = parsed.path.removeprefix("/shorts/").strip("/")
            else:
                return None
        else:
            return None
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return video_id, f"https://www.youtube.com/watch?v={video_id}"
    if platform == "tiktok" and parsed.hostname in {"www.tiktok.com", "tiktok.com"}:
        match = re.fullmatch(r"/@([A-Za-z0-9_.]+)/video/([0-9]{8,25})/?", parsed.path)
        if match:
            return match[2], f"https://www.tiktok.com/@{match[1]}/video/{match[2]}"
    return None


async def _public_views(platform, url):
    target = _public_video(platform, url)
    if target is None:
        return None
    from pipeline import YTDLP_ARGS, YTDLP_CMD, _run

    video_id, canonical = target
    output = await _run([*YTDLP_CMD, *YTDLP_ARGS, "--ignore-config", "--no-cookies", "--no-cookies-from-browser",
                         "--skip-download", "--no-playlist", "--no-warnings", "--socket-timeout", "10",
                         "--retries", "0", "--extractor-retries", "0", "--dump-single-json", canonical], timeout=30)
    metadata = json.loads(output)
    views = metadata.get("view_count")
    if (str(metadata.get("id")) == video_id and type(views) is int and views >= 0
            and metadata.get("availability") not in {"private", "unlisted", "needs_auth", "subscriber_only", "premium_only"}):
        return views
    return None


def _is_ad(clip):
    return clip.get("ad_kind") == "banner" or clip.get("has_ad") is True


def _supported_disclosure(platform, clip):
    disclosure = clip.get("ad_disclosure")
    return disclosure if clip.get("ad_kind") == "banner" and disclosure in AD_DISCLOSURES[platform] else ""


class BrowserPosting:
    def __init__(self, db, clips_dir, browser, *, now=time.time, view_reader=None):
        self.db = db
        self.clips_dir = Path(clips_dir).resolve()
        self.browser = browser
        self.now = now
        self.worker = None
        self.monitor = None
        self.view_reader = view_reader or _public_views
        self.wake = asyncio.Event()
        self.browser_lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()

    async def _config(self):
        raw = await self.db.app_settings.find_one({"id": "main"}) or {}
        saved = raw.get("browser_posting") or {}
        return {"browser": saved.get("browser", "edge"), "debug_address": saved.get("debug_address", ""),
                "accounts": saved.get("accounts", {}), "enabled_since": saved.get("enabled_since", {})}

    async def _save_config(self, config):
        await self.db.app_settings.update_one({"id": "main"}, {"$set": {"browser_posting": config}}, upsert=True)
        self.wake.set()

    async def configure(self, browser, debug_address):
        address = validate_debug_address(debug_address.strip()) if browser == "octo" else ""
        async with self.config_lock:
            config = await self._config()
            if (browser, address) != (config["browser"], config["debug_address"]):
                await self._save_config({"browser": browser, "debug_address": address,
                                         "accounts": {}, "enabled_since": {}})
        return await self.status()

    async def open(self, platform):
        if self.browser_lock.locked():
            raise PostingBlocked("Браузер занят отправкой. Дождись текущего шага.")
        async with self.browser_lock:
            await self.browser.open(platform, await self._config())

    async def bind(self, platform):
        if self.browser_lock.locked():
            raise PostingBlocked("Браузер занят отправкой. Дождись текущего шага.")
        async with self.browser_lock:
            before = await self._config()
            account = await self.browser.account(platform, before)
            async with self.config_lock:
                config = await self._config()
                if (config["browser"], config["debug_address"]) != (before["browser"], before["debug_address"]):
                    raise PostingBlocked("Профиль браузера изменился. Проверь вход ещё раз.")
                if config["accounts"].get(platform) != account:
                    config["enabled_since"].pop(platform, None)
                config["accounts"][platform] = account
                await self._save_config(config)
        return await self.status()

    async def enable(self, platform, enabled):
        async with self.config_lock:
            config = await self._config()
            if enabled:
                if not config["accounts"].get(platform):
                    raise PostingBlocked("Сначала проверь и привяжи текущий аккаунт.")
                config["enabled_since"].setdefault(platform, self.now())
            else:
                config["enabled_since"].pop(platform, None)
            await self._save_config(config)
        return await self.status()

    async def status(self):
        config = await self._config()
        records = await self.db.browser_posts.find({}).sort("updated_at", -1).to_list(12)
        return {**config, "day": await self._day(), "posts": records}

    async def start(self):
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._loop())
        if self.monitor is None or self.monitor.done():
            self.monitor = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        tasks = [task for task in (self.worker, self.monitor) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.worker = self.monitor = None
        await self.browser.close()

    async def _loop(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except Exception:
                logger.exception("Browser posting worker failed")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    async def _store(self, record, status, message="", url=""):
        now = self.now()
        if status in UNCERTAIN_STAGES:
            record["last_attempt_at"] = now
        elif status == "needs_review":
            record.setdefault("last_attempt_at", record.get("updated_at") or now)
        elif status == "published":
            record["published_at"] = now
        record.update(status=status, message=message, url=url, updated_at=now)
        await self.db.browser_posts.update_one({"id": record["id"]}, {"$set": record}, upsert=True)

    async def _monitor_loop(self):
        while True:
            try:
                await self.measure_views()
            except Exception:
                logger.exception("Public video view monitor failed")
            await asyncio.sleep(VIEW_INTERVAL)

    async def measure_views(self):
        for record in await self.db.browser_posts.find({"status": "published"}).to_list(None):
            platform, url = record.get("platform"), record.get("url", "")
            if _public_video(platform, url) is None:
                continue
            published_at = _timestamp(record.get("published_at")) or _timestamp(record.get("updated_at"))
            if not published_at:
                continue
            samples = dict(record.get("view_samples") or {})
            checks = dict(record.get("view_checks") or {})
            for label, interval in VIEW_MILESTONES.items():
                due_at = published_at + interval
                if label in checks or label in samples or not due_at <= self.now() <= due_at + VIEW_GRACE:
                    continue
                try:
                    views = await self.view_reader(platform, url)
                except Exception:
                    views = None
                checked_at = self.now()
                checks[label] = checked_at
                if type(views) is int and views >= 0 and due_at <= checked_at <= due_at + VIEW_GRACE:
                    samples[label] = {"views": views, "checked_at": checked_at, "due_at": due_at}
                fields = {"view_checks": checks}
                if samples:
                    fields["view_samples"] = samples
                await self.db.browser_posts.update_one({"id": record["id"], "status": "published", "url": url},
                                                      {"$set": fields})

    def _file(self, clip):
        name = str(clip.get("video_filename") or clip.get("filename") or "")
        if not re.fullmatch(r"[\w-]+\.mp4", name, re.I):
            raise PostingBlocked("У готового ролика нет MP4 для публикации.")
        path = (self.clips_dir / name).resolve()
        if path.parent != self.clips_dir or not path.is_file() or not path.stat().st_size:
            raise PostingBlocked("Готовый MP4 не найден.")
        return path

    async def _day(self):
        settings = await self.db.app_settings.find_one({"id": "main"}) or {}
        zone = (settings.get("autopilot") or {}).get("time_zone", DEFAULT_CONFIG["time_zone"])
        return datetime.fromtimestamp(self.now(), _tz(zone)).date().isoformat()

    async def _releases(self, day, enabled_at):
        by_clip = {}
        for run in await self.db.autopilot_runs.find({}).to_list(None):
            releases = run.get("releases")
            entries = list(releases) if isinstance(releases, list) else []
            if run.get("status") == "done":
                entries.append({"day": run.get("output_day"), "clip_id": run.get("output_clip_id"),
                                "ready_at": run.get("completed_at")})
            for item in entries:
                if not isinstance(item, dict) or item.get("day") != day:
                    continue
                clip_id, ready_at = item.get("clip_id"), _timestamp(item.get("ready_at"))
                if not isinstance(clip_id, str) or not clip_id or not ready_at:
                    continue
                # A completed run can also mirror the same clip in the legacy
                # fields. Preserve its original readiness for enable gating.
                by_clip[clip_id] = min(ready_at, by_clip.get(clip_id, ready_at))
        now = self.now()
        return [{"day": day, "clip_id": clip_id, "ready_at": ready_at}
                for clip_id, ready_at in sorted(by_clip.items(), key=lambda item: (item[1], item[0]))
                if enabled_at < ready_at <= now]

    async def _candidate(self, platform, config, *, retry=False):
        enabled_at, account = config["enabled_since"].get(platform), config["accounts"].get(platform)
        if enabled_at is None or not account:
            return None
        history = await self.db.browser_posts.find({"platform": platform}).to_list(None)
        if not retry:
            last_attempt = max((
                _timestamp(row.get("published_at")) or _timestamp(row.get("last_attempt_at"))
                or _timestamp(row.get("updated_at"))
                for row in history if row.get("status") in NON_REPEATABLE
            ), default=0)
            if last_attempt and self.now() < last_attempt + POST_INTERVAL:
                return None
        by_id = {row["id"]: row for row in history}
        sent_or_uncertain = {row.get("clip_id") for row in history if row.get("status") in NON_REPEATABLE}
        for release in await self._releases(await self._day(), enabled_at):
            day, clip_id = release["day"], release["clip_id"]
            if clip_id in sent_or_uncertain:
                continue
            key = f"{platform}:{day}:{clip_id}"
            legacy = by_id.get(f"{platform}:{day}")
            record = by_id.get(key)
            if record is None and legacy and legacy.get("clip_id") == clip_id:
                record = legacy
            if retry:
                if not record or record.get("status") != "blocked" or record.get("retry_allowed") is False:
                    continue
            elif record and record.get("status") != "preparing":
                continue
            if record and record.get("account") != account:
                continue
            clip = await self.db.clips.find_one({"id": clip_id})
            if not clip or clip.get("ready_for_publish") is not True or clip.get("needs_review"):
                continue
            if retry and _is_ad(clip) and not _supported_disclosure(platform, clip):
                continue
            return release, key, record, clip
        return None

    async def tick(self):
        if self.browser_lock.locked():
            return
        async with self.browser_lock:
            # A restarted worker cannot know whether an interrupted browser
            # action reached the platform. Recover even after midnight or pause.
            for record in await self.db.browser_posts.find({}).to_list(None):
                if record.get("status") in UNCERTAIN_STAGES:
                    await self._store(record, "needs_review", "Отправка прервалась. Проверь площадку перед повторной загрузкой.")
            for platform in URLS:
                # The preceding platform may take minutes or be paused while it works.
                config = await self._config()
                candidate = await self._candidate(platform, config)
                if candidate:
                    await self._post(platform, config, candidate)

    async def _post(self, platform, config, candidate):
        release, key, old, clip = candidate
        account = config["accounts"][platform]
        record = {**(old or {}), **release, "id": old["id"] if old else key, "platform": platform,
                  "account": account, "title": str(clip.get("title") or "Клип")[:100]}
        disclosure = _supported_disclosure(platform, clip) if _is_ad(clip) else ""
        if _is_ad(clip) and not disclosure:
            record["retry_allowed"] = False
            await self._store(record, "blocked", DISCLOSURE_REQUIRED)
            return
        if disclosure:
            record["ad_disclosure"] = disclosure
        await self._store(record, "preparing")

        async def checkpoint(stage):
            async with self.config_lock:
                current = await self._config()
                if (current["enabled_since"].get(platform) != config["enabled_since"][platform]
                        or current["accounts"].get(platform) != account
                        or current["browser"] != config["browser"]
                        or current["debug_address"] != config["debug_address"]):
                    raise PostingBlocked("Автопост выключен или изменился профиль браузера.")
                await self._store(record, stage)

        try:
            path = self._file(clip)
            caption = str(clip.get("publication_caption") or clip.get("caption") or record["title"])
            payload = {"title": record["title"], "caption": caption}
            if disclosure:
                payload["ad_disclosure"] = disclosure
            url = await self.browser.publish(platform, config, path, payload, account, checkpoint)
        except Exception as exc:
            uncertain = record["status"] in UNCERTAIN_STAGES
            message = str(exc) if isinstance(exc, PostingBlocked) else "Браузер не завершил шаг отправки."
            await self._store(record, "needs_review" if uncertain else "blocked", message)
        else:
            await self._store(record, "published", url=url)

    async def retry(self, platform):
        if self.browser_lock.locked():
            raise PostingBlocked("Браузер занят отправкой.")
        async with self.browser_lock, self.config_lock:
            config = await self._config()
            candidate = await self._candidate(platform, config, retry=True)
            if candidate is None:
                raise PostingBlocked("Повтор доступен только для остановки до начала загрузки.")
            _, _, record, _ = candidate
            await self._store(record, "preparing")
        self.wake.set()
        return await self.status()
