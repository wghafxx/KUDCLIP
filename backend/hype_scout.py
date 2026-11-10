"""Fresh Twitch VOD discovery and hot-moment maps from viewer clips and chat velocity."""

import asyncio
import math
import re
import time
from datetime import datetime
from statistics import median

import httpx
import yt_dlp

from stream_analysis import AUTHOR_DURATION_LIMITS
from trend_sources import SourceScanUnavailable, _read_feed, youtube_signal_windows

GQL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
LAUGH = re.compile(r"(?:kekw|lul|omegalul|lol|xd|ахах|хаха|ору|аха|пх+|😂|🤣|клип|clip|жесть|wtf|ахуе|охуе|пизд|ебать|ну все)", re.I)
BIN = 30
CHAT_STEP = 60
_LOGIN = re.compile(r"[a-z0-9_]{1,25}")
_SIGNALS_ID = "vod_signals"


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def login_of(channel):
    value = str(channel or "").strip().lower().rstrip("/").rsplit("/", 1)[-1]
    return value if _LOGIN.fullmatch(value) else ""


async def _gql(client, query):
    response = await client.post(GQL, json={"query": query}, headers={"Client-ID": CLIENT_ID}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"])[:200])
    return data.get("data") or {}


async def streamer_snapshot(client, login, vods=6, clips=100):
    query = ('query{user(login:"%s"){displayName stream{viewersCount createdAt} '
             'videos(first:%d,type:ARCHIVE,sort:TIME){edges{node{id title lengthSeconds viewCount createdAt '
             'previewThumbnailURL(width:640,height:360)}}} '
             'clips(first:%d,criteria:{period:LAST_WEEK,sort:VIEWS_DESC}){edges{node{slug title viewCount createdAt '
             'durationSeconds videoOffsetSeconds video{id}}}}}}' % (login, vods, clips))
    user = (await _gql(client, query)).get("user")
    if not user:
        return None
    return {
        "login": login, "name": user.get("displayName") or login,
        "live": bool(user.get("stream")),
        "live_since": _epoch((user.get("stream") or {}).get("createdAt")),
        "vods": [edge["node"] for edge in (user.get("videos") or {}).get("edges") or []],
        "clips": [edge["node"] for edge in (user.get("clips") or {}).get("edges") or []],
    }


async def _chat_sample(client, vod_id, offset):
    query = ('query{video(id:"%s"){comments(contentOffsetSeconds:%d){edges{node{contentOffsetSeconds '
             'message{fragments{text}}}}}}}' % (vod_id, offset))
    try:
        edges = ((await _gql(client, query)).get("video") or {}).get("comments", {}).get("edges") or []
    except Exception:
        return None
    times = [edge["node"]["contentOffsetSeconds"] for edge in edges]
    if not times:
        return {"at": offset, "rate": 0.0, "laugh": 0.0}
    span = max(1, max(times) - min(times) + 1)
    texts = [" ".join(f.get("text") or "" for f in (edge["node"].get("message") or {}).get("fragments") or [])
             for edge in edges]
    return {"at": offset, "rate": len(times) / span, "laugh": sum(bool(LAUGH.search(t)) for t in texts) / len(texts)}


async def chat_curve(client, vod_id, duration, step=CHAT_STEP, concurrency=8):
    gate = asyncio.Semaphore(concurrency)

    async def one(offset):
        async with gate:
            return await _chat_sample(client, vod_id, offset)

    samples = await asyncio.gather(*(one(at) for at in range(0, int(duration), step)))
    return [s for s in samples if s]


def _zscores(values):
    if not values:
        return []
    mid = median(values)
    spread = median(abs(v - mid) for v in values) or 1e-6
    return [(v - mid) / (1.4826 * spread) for v in values]


def hot_windows(duration, clips, chat, *, count=6, before=75, after=45):
    """Rank 30-second bins by viewer clips and chat bursts, then cut non-overlapping windows."""
    bins = max(1, int(math.ceil(duration / BIN)))
    clip_score = [0.0] * bins
    for clip in clips:
        start = float(clip.get("videoOffsetSeconds") or 0)
        end = start + float(clip.get("durationSeconds") or 30)
        weight = 1 + math.log1p(float(clip.get("viewCount") or 0))
        for index in range(int(start // BIN), min(bins, int(end // BIN) + 1)):
            clip_score[index] += weight
    chat_score = [0.0] * bins
    if chat:
        rates = _zscores([s["rate"] for s in chat])
        for sample, z in zip(chat, rates):
            index = min(bins - 1, int(sample["at"] // BIN))
            chat_score[index] = max(chat_score[index], max(0.0, z) * (1 + 2 * sample["laugh"]))
    top_clip, top_chat = max(clip_score) or 1, max(chat_score) or 1
    score = [0.65 * c / top_clip + 0.35 * h / top_chat for c, h in zip(clip_score, chat_score)]
    windows = []
    for index in sorted(range(bins), key=lambda i: -score[i]):
        if score[index] <= 0.05 or len(windows) >= count:
            break
        center = index * BIN + BIN / 2
        start, end = max(0.0, center - before), min(duration, center + after)
        if any(start < w["end"] and end > w["start"] for w in windows):
            continue
        reasons = []
        if clip_score[index]:
            reasons.append("клипы зрителей")
        if chat_score[index] > 1:
            reasons.append("всплеск чата")
        windows.append({"start": round(start, 1), "end": round(end, 1), "kind": "highlight",
                        "title": " + ".join(reasons) or "активность", "heat": round(100 * score[index])})
    return sorted(windows, key=lambda w: w["start"])


def vod_hype(vod, clips, now):
    created = _epoch(vod.get("createdAt")) or now
    length = float(vod.get("lengthSeconds") or 0)
    hours = max(1.0, (now - created) / 3600)
    own = [c for c in clips if (c.get("video") or {}).get("id") == vod["id"]]
    clip_views = sum(float(c.get("viewCount") or 0) for c in own)
    raw = (0.5 * math.log1p(clip_views) + 0.3 * math.log1p(float(vod.get("viewCount") or 0) / hours)
           + 0.2 * math.log1p(len(own)) * 3)
    return {"clips": own, "clip_views": int(clip_views), "views_per_hour": float(vod.get("viewCount") or 0) / hours,
            "raw": raw, "ended_at": created + length}


async def _signals(db, client, vod, clips, use_chat, *, chat_step=CHAT_STEP):
    cache = await db.trend_observations.find_one({"id": _SIGNALS_ID}) or {"id": _SIGNALS_ID, "vods": {}}
    saved = cache.get("vods", {}).get(vod["id"])
    length = float(vod.get("lengthSeconds") or 0)
    if (saved and "chat_step" in saved and saved.get("length") == length and saved.get("clips") == len(clips)
            and saved.get("chat_step") == (chat_step if use_chat else None)):
        return saved["windows"]
    chat = await chat_curve(client, vod["id"], length, step=chat_step) if use_chat else []
    windows = hot_windows(length, clips, chat)
    vods = {k: v for k, v in cache.get("vods", {}).items() if v.get("at", 0) > time.time() - 7 * 86400}
    vods[vod["id"]] = {"windows": windows, "length": length, "clips": len(clips),
                      "chat_step": chat_step if use_chat else None, "at": time.time()}
    await db.trend_observations.update_one({"id": _SIGNALS_ID}, {"$set": {"vods": vods}}, upsert=True)
    return windows


async def scan_vods(db, profiles, now, *, fresh_hours=36, use_chat=True, top=3):
    """Rows compatible with trend_sources.scan_sources for streamers with VOD mode on."""
    owners = {}
    for profile in profiles:
        settings = profile.get("settings") or {}
        if not settings.get("stream_vods", True) or not settings.get("twitch_trending"):
            continue
        blocked = {login_of(item) for item in settings.get("blocked_streamers") or ()}
        for channel in profile.get("twitch_channels") or ():
            login = login_of(channel)
            if login and login not in blocked:
                owners.setdefault(login, set()).add(profile["id"])
    if not owners:
        return []
    async with httpx.AsyncClient() as client:
        gate = asyncio.Semaphore(4)

        async def snap(login):
            async with gate:
                try:
                    return await streamer_snapshot(client, login)
                except Exception:
                    return None

        snapshots = [s for s in await asyncio.gather(*(snap(login) for login in owners)) if s]
        pool = []
        for snapshot in snapshots:
            for vod in snapshot["vods"]:
                hype = vod_hype(vod, snapshot["clips"], now)
                # A VOD of a stream that is still live keeps growing; wait for it to end.
                if snapshot["live"] and vod is snapshot["vods"][0]:
                    continue
                if not 0 <= now - hype["ended_at"] < fresh_hours * 3600 or float(vod.get("lengthSeconds") or 0) < 600:
                    continue
                pool.append((snapshot, vod, hype))
        if not pool:
            return []
        best = max(item[2]["raw"] for item in pool) or 1
        pool.sort(key=lambda item: -item[2]["raw"])
        rows = []
        for snapshot, vod, hype in pool[:top]:
            windows = await _signals(db, client, vod, hype["clips"], use_chat)
            if not windows:
                continue
            score = round(100 * hype["raw"] / best, 1)
            video = {
                "video_id": "twitchvod:" + vod["id"], "url": f"https://www.twitch.tv/videos/{vod['id']}",
                "title": str(vod.get("title") or "Стрим")[:250], "duration": float(vod["lengthSeconds"]),
                "thumbnail": str(vod.get("previewThumbnailURL") or ""), "published_at": hype["ended_at"],
                "provider": "twitch", "source_kind": "vod",
                "channel_url": f"https://www.twitch.tv/{snapshot['login']}", "channel_title": snapshot["name"],
                "views": int(vod.get("viewCount") or 0), "views_source": "twitch_gql",
                "views_per_hour": hype["views_per_hour"], "clip_views": hype["clip_views"],
                "windows": windows, "trend_ready": True, "trend_score": max(1.0, score),
                "trend_status": "eligible", "trend_observed_at": now,
            }
            for profile_id in owners[snapshot["login"]]:
                rows.append({"profile_id": profile_id, "video": dict(video)})
        return rows


def heatmap_windows(duration, heatmap, count=4):
    points = sorted((p for p in heatmap or () if isinstance(p, dict) and p.get("value") is not None),
                    key=lambda p: -float(p["value"]))
    windows = []
    for point in points:
        if len(windows) >= count or float(point["value"]) < 0.3:
            break
        center = (float(point["start_time"]) + float(point["end_time"])) / 2
        start, end = max(0.0, center - 75), min(duration, center + 45)
        if any(start < w["end"] and end > w["start"] for w in windows):
            continue
        windows.append({"start": round(start, 1), "end": round(end, 1), "kind": "highlight",
                        "title": "самый пересматриваемый момент", "heat": round(100 * float(point["value"]))})
    return sorted(windows, key=lambda w: w["start"])


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


async def twitch_pick(db, login, now, *, fresh_hours=36, use_chat=True, exclude=()):
    """Select the newest finished, eligible archive from the author's channel."""
    async with httpx.AsyncClient() as client:
        snapshot = await streamer_snapshot(client, login)
        if not snapshot:
            return None
        best = None
        for index, vod in enumerate(snapshot["vods"]):
            duration = float(vod.get("lengthSeconds") or 0)
            created = _epoch(vod.get("createdAt"))
            if (created is None or not 600 <= duration <= AUTHOR_DURATION_LIMITS["twitch"]
                    or "twitchvod:" + vod["id"] in exclude):
                continue
            ended = created + duration
            if snapshot["live"]:
                live_since = snapshot.get("live_since")
                if (live_since is None and index == 0) or (live_since is not None and ended > live_since):
                    continue
            if not 0 <= now - ended < fresh_hours * 3600:
                continue
            hype = vod_hype(vod, snapshot["clips"], now)
            if best is None or ended > best[1]["ended_at"]:
                best = (vod, hype)
        if best is None:
            return None
        vod, hype = best
        duration = float(vod["lengthSeconds"])
        # At most 120 metadata samples, even for the longest eligible recording.
        windows = await _signals(db, client, vod, hype["clips"], use_chat,
                                 chat_step=max(CHAT_STEP, math.ceil(duration / 120)))
        reason = f"Последняя завершённая запись · {clock(duration)} · клипов зрителей: {len(hype['clips'])}"
        if windows:
            reason += ". Горячие места: " + ", ".join(f"{clock(w['start'])} — {w['title']}" for w in windows[:4])
        return {"video_id": "twitchvod:" + vod["id"], "url": f"https://www.twitch.tv/videos/{vod['id']}",
                "title": str(vod.get("title") or "Стрим")[:250], "duration": duration,
                "thumbnail": str(vod.get("previewThumbnailURL") or ""), "published_at": hype["ended_at"],
                "provider": "twitch", "source_kind": "vod", "channel_title": snapshot["name"],
                "views": int(vod.get("viewCount") or 0), "views_per_hour": hype["views_per_hour"],
                "windows": windows, "trend_ready": True, "trend_score": 100.0, "reason": reason}


def _youtube_windows(info, duration):
    windows = heatmap_windows(duration, info.get("heatmap")) if duration > 180 else []
    if windows:
        return windows, "Пики пересмотров"
    return youtube_signal_windows(info, duration), "Отметки на видео"


def _youtube_pick_sync(channel, now, fresh_hours, exclude=()):
    base = channel.rstrip("/")
    for suffix in ("/videos", "/streams", "/shorts", "/featured"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    options = {"skip_download": True, "quiet": True, "no_warnings": True, "socket_timeout": 15,
               "retries": 0, "extractor_retries": 0, "cachedir": False}
    entries = {}
    listing_loaded, listing_error = False, None
    with yt_dlp.YoutubeDL({**options, "extract_flat": "in_playlist", "playlistend": 5}) as ydl:
        for tab in ("videos", "streams"):
            try:
                info = ydl.extract_info(f"{base}/{tab}", download=False) or {}
                for entry in info.get("entries") or ():
                    if entry and entry.get("id"):
                        entries.setdefault(entry["id"], entry)
                listing_loaded = True
            except yt_dlp.utils.DownloadError as exc:
                if f"does not have a {tab} tab" not in str(exc):
                    listing_error = exc
    if not listing_loaded and listing_error:
        raise SourceScanUnavailable("YouTube не вернул список роликов канала.") from listing_error
    best = None
    detail_loaded, detail_error = False, None
    feeds = {}
    with yt_dlp.YoutubeDL({**options, "js_runtimes": {"deno": {}, "node": {}}}) as ydl:
        for identity, entry in entries.items():
            if entry.get("live_status") in {"is_live", "is_upcoming"} or identity in exclude:
                continue
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={identity}", download=False) or {}
                detail_loaded = True
            except yt_dlp.utils.DownloadError as exc:
                detail_error = exc
                continue
            duration = float(info.get("duration") or 0)
            if (not 61 <= duration <= AUTHOR_DURATION_LIMITS["youtube"]
                    or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}):
                continue
            released = info.get("release_timestamp")
            uploaded = info.get("timestamp")
            if released:
                ended = float(released) + (duration if info.get("live_status") == "was_live" else 0)
            elif uploaded:
                ended = float(uploaded)
            else:
                channel_id = info.get("channel_id")
                if channel_id not in feeds:
                    feeds[channel_id] = _read_feed(channel_id)
                ended = (feeds[channel_id].get(identity) or {}).get("published_at")
            if ended is None:
                continue
            age = now - ended
            if not 0 <= age < fresh_hours * 3600:
                continue
            windows, window_reason = _youtube_windows(info, duration)
            rate = float(info.get("view_count") or 0) * 3600 / max(1800.0, age)
            if best is None or ended > best[0]:
                reason = f"Последний выпуск · {clock(duration)} · опубликован {age / 3600:.1f} ч назад"
                if windows:
                    reason += f". {window_reason}: " + ", ".join(clock(w["start"]) for w in windows)
                best = (ended, {"video_id": info["id"], "url": f"https://www.youtube.com/watch?v={info['id']}",
                                "title": str(info.get("title") or "Видео")[:250], "duration": duration,
                                "thumbnail": info.get("thumbnail") or "", "published_at": ended, "provider": "youtube",
                                "source_kind": "vod" if info.get("live_status") == "was_live" else "video",
                                "channel_title": info.get("channel") or "", "views": int(info.get("view_count") or 0),
                                "views_per_hour": rate, "windows": windows, "trend_ready": True, "trend_score": 100.0,
                                "reason": reason})
    if not detail_loaded and detail_error:
        raise SourceScanUnavailable("YouTube не вернул метаданные роликов.") from detail_error
    return best[1] if best else None


async def youtube_pick(channel, now, *, fresh_hours=36, exclude=()):
    return await asyncio.to_thread(_youtube_pick_sync, channel, now, fresh_hours, set(exclude))


async def check_streamers(logins, now=None):
    """VOD availability report for the settings screen."""
    now = now or time.time()
    async with httpx.AsyncClient() as client:
        async def one(login):
            try:
                snapshot = await streamer_snapshot(client, login, vods=10, clips=20)
            except Exception:
                return {"login": login, "status": "error"}
            if not snapshot:
                return {"login": login, "status": "missing"}
            vods = snapshot["vods"]
            last = max((_epoch(v.get("createdAt")) or 0 for v in vods), default=0)
            return {"login": login, "name": snapshot["name"], "live": snapshot["live"], "vods": len(vods),
                    "last_vod_hours": round((now - last) / 3600, 1) if last else None,
                    "avg_hours": round(sum(float(v.get("lengthSeconds") or 0) for v in vods) / max(1, len(vods)) / 3600, 1),
                    "clips_week": len(snapshot["clips"]),
                    "status": "ok" if vods else "no_vods"}
        return await asyncio.gather(*(one(login) for login in logins))
