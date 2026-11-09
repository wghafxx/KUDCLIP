"""Keyless discovery and view velocity for public YouTube and Twitch sources."""

import asyncio
from collections import defaultdict
from datetime import datetime
from http.client import HTTPException
from itertools import islice, zip_longest
import math
import re
from statistics import median
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import yt_dlp


TWITCH_SEEDS = (
    "sasavot", "5opka", "rostislav_999", "rostikfacekid", "bratishkinoff", "strogo",
    "buster", "evelone192", "paradeev1ch", "t2x2", "voodoosh",
)
YOUTUBE_TREND_QUERIES = (
    "Сасавот стрим лучшие моменты",
    "Пятёрка 5opka моменты со стрима",
    "Ростик rostislav_999 Dota 2 моменты со стрима",
    "Фейскид rostikfacekid лучшие моменты стрима",
)
FRESH_SECONDS = 48 * 3600
MIN_OBSERVATION_GAP = 300
MAX_OBSERVATION_GAP = 6 * 3600
MIN_VIEWS_PER_HOUR = {"youtube": 200, "twitch": 30}
MAX_SOURCE_SECONDS = 1800
MAX_WINDOW_SECONDS = 180
_LIST_LIMIT = {"youtube": 18, "twitch": 24}
_MAX_YOUTUBE_DETAILS = 6
_SEARCH_RESULTS_PER_QUERY = 4
_YOUTUBE_RECENT_VIDEOS_FILTER = "EgQIAxAB"  # Upload Date: This week + Result Type: Video
_FEED_LIMIT = 1024 * 1024
_OBSERVATION_ID = "view_counts"
_OBSERVATION_LOCK = asyncio.Lock()
_YT_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YT_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_TWITCH_LOGIN = re.compile(r"[a-z0-9_]{1,25}")
_TWITCH_SLUG = re.compile(r"[A-Za-z0-9_-]{1,180}")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv", "go.twitch.tv"}
_TWITCH_RESERVED = {"directory", "videos", "clips", "search", "settings", "downloads", "subscriptions", "turbo"}
_ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
         "m": "http://search.yahoo.com/mrss/"}


class SourceScanUnavailable(RuntimeError):
    """Every configured source failed to respond during this scan."""


def _url(value):
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme in {"http", "https"} and not (parsed.username or parsed.password or parsed.port):
            return parsed
    except ValueError:
        pass
    return None


def _url_identity(value):
    parsed = _url(value)
    if not parsed:
        return ""
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname == "clips.twitch.tv" and len(parts) == 1:
        slug = parse_qs(parsed.query).get("clip", [""])[0] if parts[0] == "embed" else parts[0]
        return "twitch:" + slug if _TWITCH_SLUG.fullmatch(slug) else ""
    if parsed.hostname in _TWITCH_HOSTS and len(parts) == 3 and parts[1] == "clip":
        return "twitch:" + parts[2] if _TWITCH_SLUG.fullmatch(parts[2]) else ""
    if parsed.hostname == "youtu.be" and len(parts) == 1:
        identity = parts[0]
    elif parsed.hostname in _YT_HOSTS and parts == ["watch"]:
        identity = parse_qs(parsed.query).get("v", [""])[0]
    elif parsed.hostname in _YT_HOSTS and len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
        identity = parts[1]
    else:
        return ""
    return identity if _YT_ID.fullmatch(identity) else ""


def source_id(value):
    """Normalize a source URL or metadata record without changing YouTube IDs."""
    if isinstance(value, dict):
        for field in ("url", "video_url", "webpage_url"):
            identity = _url_identity(value.get(field))
            if identity:
                return identity
        if value.get("provider") == "twitch":
            slug = str(value.get("clip_slug") or value.get("display_id") or "")
            if _TWITCH_SLUG.fullmatch(slug):
                return "twitch:" + slug
            identity = str(value.get("video_id") or "")
            return source_id(identity) if identity.startswith("twitch:") else ""
        value = value.get("video_id") or value.get("id") or ""
    value = str(value or "").strip()
    if value.startswith("twitch:") and _TWITCH_SLUG.fullmatch(value[7:]):
        return value
    if value.startswith("youtube:"):
        value = value[8:]
    return value if _YT_ID.fullmatch(value) else _url_identity(value)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _epoch(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        try:
            return value.timestamp() if value.utcoffset() is not None else None
        except (ValueError, OverflowError, OSError):
            return None
    return _number(value)


def _published(video):
    fields = ["published_at"]
    if not (video.get("timestamp_approximate") or video.get("approximate_date")):
        fields.extend(("timestamp", "release_timestamp"))
    if video.get("provider") == "twitch":
        fields.append("created_at")
    for field in fields:
        at = _epoch(video.get(field))
        if at is not None and at > 0:
            return at
    return None


def fresh(video, now):
    """Exact timestamps only. Date-only, future and 48-hour-old items fail closed."""
    published, observed = _published(video), _epoch(now)
    return published is not None and observed is not None and 0 <= observed - published < FRESH_SECONDS


def _youtube_channel(value):
    parsed = _url(value)
    if not parsed or parsed.hostname not in _YT_HOSTS:
        return ""
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] in {"channel", "c", "user"}:
        base, tail = parts[:2], parts[2:]
        if parts[0] == "channel" and not _YT_CHANNEL_ID.fullmatch(parts[1]):
            return ""
    elif parts and parts[0].startswith("@") and len(parts[0]) > 1:
        base, tail = parts[:1], parts[1:]
    else:
        return ""
    if len(tail) > 1 or (tail and tail[0] not in {"videos", "shorts", "streams"}):
        return ""
    if any(part in {".", ".."} or re.search(r"[%?#\\\s]", part) for part in base):
        return ""
    return "https://www.youtube.com/" + "/".join(base + (tail or ["videos"]))


def _twitch_channel(value):
    raw = str(value or "").strip().lower()
    if _TWITCH_LOGIN.fullmatch(raw) and raw not in _TWITCH_RESERVED:
        return "https://www.twitch.tv/" + raw
    parsed = _url(raw)
    if not parsed or parsed.hostname not in _TWITCH_HOSTS:
        return ""
    parts = parsed.path.strip("/").split("/")
    if (not _TWITCH_LOGIN.fullmatch(parts[0]) or parts[0] in _TWITCH_RESERVED
            or (len(parts) != 1 and parts[1:] not in (["clips"], ["videos"]))):
        return ""
    return "https://www.twitch.tv/" + parts[0]


def _views(value):
    value = _number(value)
    return int(value) if value is not None and 0 <= value < 2**53 else None


def _parse_feed(payload, channel_id):
    result = {}
    if len(payload) > _FEED_LIMIT:
        return result
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return result
    for entry in root.findall("a:entry", _ATOM):
        identity = entry.findtext("yt:videoId", namespaces=_ATOM)
        author = entry.findtext("yt:channelId", namespaces=_ATOM)
        at = _epoch(entry.findtext("a:published", namespaces=_ATOM))
        if author != channel_id or not identity or not _YT_ID.fullmatch(identity) or at is None:
            continue
        stats = entry.find("m:group/m:community/m:statistics", _ATOM)
        result[identity] = {"published_at": at, "views": _views(stats.get("views")) if stats is not None else None}
    return result


def _read_feed(channel_id):
    if not channel_id or not _YT_CHANNEL_ID.fullmatch(channel_id):
        return {}
    request = Request(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                      headers={"User-Agent": "Mozilla/5.0", "Accept": "application/atom+xml"})
    try:
        with urlopen(request, timeout=8) as response:
            return _parse_feed(response.read(_FEED_LIMIT + 1), channel_id)
    except (OSError, ValueError, HTTPException):
        return {}


def _options(limit):
    return {"extract_flat": "in_playlist", "playlistend": limit, "skip_download": True,
            "quiet": True, "no_warnings": True, "socket_timeout": 12, "retries": 0,
            "extractor_retries": 0, "noplaylist": True, "cachedir": False}


def _merge_feed_item(item, feed_item):
    if feed_item:
        item["published_at"] = feed_item["published_at"]
        if feed_item["views"] is not None:
            item["views"], item["views_source"] = feed_item["views"], "atom"


def _youtube_sync(channel, limit, now):
    with yt_dlp.YoutubeDL(_options(limit)) as ydl:
        info = ydl.extract_info(channel, download=False)
        if not info:
            raise SourceScanUnavailable("YouTube не вернул данные канала.")
        entries = [dict(item) for item in islice(info.get("entries") or (), limit) if isinstance(item, dict)]
        explicit = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})/", channel)
        channel_id = explicit[1] if explicit else (info.get("channel_id") or info.get("uploader_id"))
        if not _YT_CHANNEL_ID.fullmatch(str(channel_id or "")):
            channel_id = next((item["channel_id"] for item in entries
                               if _YT_CHANNEL_ID.fullmatch(str(item.get("channel_id") or ""))), "")
        feed = _read_feed(channel_id)
        details_left = _MAX_YOUTUBE_DETAILS
        result = []
        for item in entries:
            identity = source_id(item)
            if not identity or identity.startswith("twitch:"):
                continue
            if channel_id and item.get("channel_id") and item["channel_id"] != channel_id:
                continue
            _merge_feed_item(item, feed.get(identity))
            duration = _number(item.get("duration"))
            at = _published(item)
            if at is not None and not fresh(item, now):
                continue
            if item.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                continue
            if details_left and (at is None or duration is None or duration > MAX_SOURCE_SECONDS):
                details_left -= 1
                try:
                    detail = ydl.extract_info(f"https://www.youtube.com/watch?v={identity}", download=False) or {}
                except yt_dlp.utils.DownloadError:
                    detail = {}
                if channel_id and detail.get("channel_id") and detail["channel_id"] != channel_id:
                    continue
                item.update({key: value for key, value in detail.items() if value is not None})
            result.append(item)
        return result


def _shorts_link(item):
    if item.get("media_type") == "short" or item.get("is_short") is True or item.get("is_shorts") is True:
        return True
    for field in ("url", "webpage_url", "original_url"):
        page = _url(item.get(field))
        if page and page.hostname in _YT_HOSTS and page.path.startswith("/shorts/"):
            return True
    return False


def _confirmed_non_short(item):
    if _shorts_link(item):
        return False
    duration = _number(item.get("duration"))
    if duration is None:
        return False
    if duration > 180:
        return True
    # YouTube classifies recently uploaded square/vertical videos <=180s as
    # Shorts. Require real landscape dimensions for a shorter search result.
    # https://support.google.com/youtube/answer/15424877
    dimensions = [(item.get("width"), item.get("height"))]
    dimensions.extend((fmt.get("width"), fmt.get("height")) for fmt in (item.get("formats") or ())
                      if isinstance(fmt, dict) and fmt.get("vcodec") not in (None, "none", "images"))
    aspects = [(width, height) for w, h in dimensions
               if (width := _number(w)) and (height := _number(h)) and width > 0 and height > 0]
    return bool(aspects) and all(width > height for width, height in aspects)


def _youtube_search_sync(queries, limit, now):
    # YouTube removed global upload-date sorting; yt-dlp removed ytsearchdate.
    # Use the public recent-video filter and order exact verified dates locally.
    # https://github.com/yt-dlp/yt-dlp/issues/15898
    limit = min(limit, _MAX_YOUTUBE_DETAILS)
    if limit <= 0:
        return []
    buckets, seen, feed_cache, result = [], set(), {}, []
    with yt_dlp.YoutubeDL(_options(_SEARCH_RESULTS_PER_QUERY)) as ydl:
        for query in queries:
            try:
                params = urlencode({"search_query": query, "sp": _YOUTUBE_RECENT_VIDEOS_FILTER})
                data = ydl.extract_info(f"https://www.youtube.com/results?{params}", download=False)
                if data is not None:
                    buckets.append(list(islice(data.get("entries") or (), _SEARCH_RESULTS_PER_QUERY)))
            except yt_dlp.utils.DownloadError:
                continue
        if not buckets:
            raise SourceScanUnavailable("Публичный поиск YouTube сейчас недоступен.")
        remaining = limit
        for batch in zip_longest(*buckets):
            for flat in batch:
                if not isinstance(flat, dict) or _shorts_link(flat):
                    continue
                identity = source_id(flat)
                if not identity or identity.startswith("twitch:") or identity in seen:
                    continue
                if (flat.get("live_status") in {"is_live", "is_upcoming", "post_live"}
                        or (_published(flat) is not None and not fresh(flat, now))):
                    continue
                if not remaining:
                    break
                remaining -= 1
                seen.add(identity)
                try:
                    detail = ydl.extract_info(f"https://www.youtube.com/watch?v={identity}", download=False) or {}
                except yt_dlp.utils.DownloadError:
                    detail = {}
                detail_identity = source_id(detail)
                if detail_identity and detail_identity != identity:
                    continue
                if flat.get("channel_id") and detail.get("channel_id") and flat["channel_id"] != detail["channel_id"]:
                    continue
                item = {**flat, **{key: value for key, value in detail.items() if value is not None}}
                channel_id = str(item.get("channel_id") or "")
                if _YT_CHANNEL_ID.fullmatch(channel_id):
                    if channel_id not in feed_cache:
                        feed_cache[channel_id] = _read_feed(channel_id)
                    _merge_feed_item(item, feed_cache[channel_id].get(identity))
                if _confirmed_non_short(item) and fresh(item, now):
                    result.append(item)
            if not remaining:
                break
    return sorted(result, key=_published, reverse=True)


def _twitch_sync(channel, limit):
    # The public extractor offers top clips for 24h and 7d. Start with the
    # daily list so a popular old weekly clip cannot crowd out today's clips.
    entries, seen, received = [], set(), False
    per_range = min(20, max(1, (limit + 1) // 2))
    with yt_dlp.YoutubeDL(_options(limit)) as ydl:
        for window in ("24hr", "7d"):
            try:
                result = ydl.extract_info(channel + f"/clips?filter=clips&range={window}", download=False)
                if not result:
                    continue
                current = list(islice(result.get("entries") or (), per_range))
            except yt_dlp.utils.DownloadError:
                continue
            received = True
            for item in current:
                identity = source_id({**item, "provider": "twitch"}) if isinstance(item, dict) else ""
                if identity and identity not in seen:
                    seen.add(identity)
                    entries.append(item)
    if not received:
        raise SourceScanUnavailable("Twitch не вернул данные канала.")
    return entries[:limit]


def _window(raw, duration, kind):
    if not isinstance(raw, dict):
        return None
    start = _number(raw.get("start_time", raw.get("start")))
    end = _number(raw.get("end_time", raw.get("end")))
    if start is None or end is None or not 0 <= start < end <= duration:
        return None
    title = str(raw.get("title") or "")[:120]
    if kind == "chapter" and re.search(r"\b(?:intro|outro|sponsor|реклама|вступление|заключение)\b", title, re.I):
        return None
    return {"start": start, "end": min(end, start + MAX_WINDOW_SECONDS), "kind": kind, "title": title}


def _windows(raw, duration):
    selected = []

    def add(window):
        if window and len(selected) < 3 and not any(window["start"] < prior["end"] and window["end"] > prior["start"]
                                                    for prior in selected):
            selected.append(window)

    for item in raw.get("highlights") or ():
        add(_window(item, duration, "highlight"))
    heatmap = [item for item in (raw.get("heatmap") or ()) if isinstance(item, dict) and 0.5 <= (_number(item.get("value")) or 0) <= 1]
    for item in sorted(heatmap, key=lambda item: -(_number(item.get("value")) or 0)):
        mark = _window(item, duration, "highlight")
        if mark:
            anchor = (mark["start"] + mark["end"]) / 2
            start = max(0, min(anchor - MAX_WINDOW_SECONDS / 2, duration - MAX_WINDOW_SECONDS))
            add({**mark, "anchor": anchor, "start": start, "end": start + MAX_WINDOW_SECONDS})
    chapters = [window for item in (raw.get("chapters") or ()) if (window := _window(item, duration, "chapter"))]
    chapters.sort(key=lambda window: window["start"])
    if chapters:
        # Include early, late and central chapters before adjacent alternatives.
        for index in [0, len(chapters) - 1, len(chapters) // 2, *range(len(chapters))]:
            add(chapters[index])
    return sorted(selected, key=lambda window: window["start"])


def _candidate(raw, provider, channel, now):
    if not isinstance(raw, dict) or raw.get("availability") not in (None, "public", "unlisted"):
        return None
    if raw.get("is_live") is True or raw.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        return None
    identity = source_id({**raw, "provider": provider})
    if not identity or (identity.startswith("twitch:") != (provider == "twitch")):
        return None
    if provider == "twitch":
        owners = {str(raw[field]).lower() for field in ("broadcaster_login", "channel_login") if raw.get(field)}
        for field in ("url", "webpage_url"):
            page = _url(raw.get(field))
            if page and page.hostname in _TWITCH_HOSTS and "/clip/" in page.path:
                owners.add(page.path.strip("/").split("/")[0].lower())
        if owners - {channel.rsplit("/", 1)[1]}:
            return None
    else:
        explicit = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})/", channel)
        if explicit and raw.get("channel_id") and raw["channel_id"] != explicit[1]:
            return None
    published = _published({**raw, "provider": provider})
    duration = _number(raw.get("duration"))
    if published is None or not 0 <= now - published < FRESH_SECONDS or duration is None or duration < 5:
        return None
    if provider == "twitch" and duration > MAX_SOURCE_SECONDS:
        return None
    windows = _windows(raw, duration) if duration > MAX_SOURCE_SECONDS else []
    if duration > MAX_SOURCE_SECONDS and not windows:
        return None
    url = ("https://clips.twitch.tv/" + identity[7:] if provider == "twitch"
           else "https://www.youtube.com/watch?v=" + identity)
    views = _views(raw.get("views"))
    if views is None:
        views = _views(raw.get("view_count"))
    video = {"video_id": identity, "url": url, "title": str(raw.get("title") or "Без названия")[:250],
             "duration": duration, "thumbnail": str(raw.get("thumbnail") or
                 (f"https://i.ytimg.com/vi/{identity}/hqdefault.jpg" if provider == "youtube" else "")),
             "published_at": published, "provider": provider, "channel_url": channel,
             "channel_title": str(raw.get("channel") or raw.get("uploader") or channel.rsplit("/", 1)[-1])[:120],
             "views": views, "views_source": str(raw.get("views_source") or "yt_dlp")[:32]}
    if windows:
        video["windows"] = windows
    if provider == "twitch":
        vod_id = str(raw.get("vod_id") or raw.get("video_id") or "")
        offset = _number(raw.get("vod_offset"))
        if re.fullmatch(r"[0-9]+", vod_id) and offset is not None and offset >= 0:
            video["twitch_vod"] = {"id": vod_id, "url": f"https://www.twitch.tv/videos/{vod_id}",
                                   "start": offset, "end": offset + duration}
    return video


def _sample(history, video, now):
    if video["views"] is None:
        return history, None, None
    current = {"at": now, "views": video["views"], "source": video["views_source"]}
    if history and history[-1]["at"] > now:
        return history, None, None
    history = [item for item in history if item["source"] == current["source"]
               and now - item["at"] <= MAX_OBSERVATION_GAP]
    if not history or video["views"] < history[-1]["views"]:
        return [current], None, None
    if now - history[-1]["at"] >= MIN_OBSERVATION_GAP:
        history = [history[-1], current]
    if len(history) < 2:
        return history, None, None
    first, last = history[-2:]
    rate = (last["views"] - first["views"]) * 3600 / (last["at"] - first["at"])
    return history, rate, last["at"]


async def _measure(db, videos, now):
    async with _OBSERVATION_LOCK:
        state = await db.trend_observations.find_one({"id": _OBSERVATION_ID}) or {}
        prior = state.get("sources") or {}
        saved = {identity: history for identity, history in prior.items()
                 if history and history[-1]["at"] >= now - FRESH_SECONDS}
        measured = {}
        for identity, video in videos.items():
            history, rate, observed_at = _sample(saved.get(identity, []), video, now)
            if history:
                saved[identity] = history
            measured[identity] = (rate, observed_at)
        if saved != prior:
            await db.trend_observations.update_one({"id": _OBSERVATION_ID}, {"$set": {"sources": saved}}, upsert=True)
        return measured


def _owner_key(channel):
    base = channel.rsplit("/", 1)[0]
    return base if "/channel/" in base else base.casefold()


def _search_channels(raw):
    channels = [_youtube_channel(raw.get(field)) for field in ("channel_url", "uploader_url")]
    channel_id = str(raw.get("channel_id") or "")
    if _YT_CHANNEL_ID.fullmatch(channel_id):
        channels.append(f"https://www.youtube.com/channel/{channel_id}/videos")
    return list(dict.fromkeys(channel for channel in channels if channel))


async def scan_sources(db, profiles, now, *, list_youtube=None, list_twitch=None, list_youtube_search=None):
    """Return eligible-source rows, measured rows first and warming rows last.

    Channel overrides accept (canonical channel URL, limit). The optional search
    override accepts (queries, limit). Search results from outside the profile's
    channel allowlist are discovery_only. Profiles may share observations.
    """
    now = _epoch(now)
    if now is None:
        raise ValueError("Для поиска нужен точный момент времени с часовым поясом.")
    channels = defaultdict(set)
    youtube_owners = defaultdict(dict)
    search_profiles = set()
    for profile in profiles:
        profile_id = profile.get("id")
        if not profile_id:
            continue
        for raw in profile.get("channels") or ():
            if channel := _youtube_channel(raw):
                channels[("youtube", channel)].add(profile_id)
                youtube_owners[profile_id][_owner_key(channel)] = channel
        if (profile.get("settings") or {}).get("youtube_trending") is True:
            search_profiles.add(profile_id)
        if (profile.get("settings") or {}).get("twitch_trending") is True:
            for raw in profile.get("twitch_channels") or ():
                if channel := _twitch_channel(raw):
                    channels[("twitch", channel)].add(profile_id)
    if not channels and not search_profiles:
        return []
    limit_concurrent = asyncio.Semaphore(3)

    async def fetch(provider, channel):
        async with limit_concurrent:
            limit = _LIST_LIMIT[provider]
            try:
                if provider == "youtube":
                    raw = await list_youtube(channel, limit) if list_youtube else await asyncio.to_thread(_youtube_sync, channel, limit, now)
                else:
                    raw = await list_twitch(channel, limit) if list_twitch else await asyncio.to_thread(_twitch_sync, channel, limit)
                return [item for data in islice(raw or (), limit) if (item := _candidate(data, provider, channel, now))]
            except Exception as error:
                return error

    async def fetch_search():
        async with limit_concurrent:
            try:
                if list_youtube_search:
                    return await list_youtube_search(YOUTUBE_TREND_QUERIES, _MAX_YOUTUBE_DETAILS)
                return await asyncio.to_thread(_youtube_search_sync, YOUTUBE_TREND_QUERIES, _MAX_YOUTUBE_DETAILS, now)
            except Exception as error:
                return error

    tasks = [fetch(provider, channel) for provider, channel in channels]
    if search_profiles:
        tasks.append(fetch_search())
    results = await asyncio.gather(*tasks)
    if all(isinstance(result, Exception) for result in results):
        raise SourceScanUnavailable("Не удалось проверить ни один из настроенных источников. Повторите поиск позже.")
    rows, unique_videos = {}, {}

    def add(profile_id, video):
        identity = video["video_id"]
        if identity not in unique_videos or (unique_videos[identity]["views"] is None and video["views"] is not None):
            unique_videos[identity] = video
        rows.setdefault((profile_id, identity), {"profile_id": profile_id, "video": dict(video)})

    for scope, result in zip(channels, results):
        if isinstance(result, Exception):
            continue
        for video in result:
            for profile_id in channels[scope]:
                add(profile_id, video)
    if search_profiles and not isinstance(results[-1], Exception):
        for raw in islice(results[-1] or (), _MAX_YOUTUBE_DETAILS):
            if not isinstance(raw, dict) or not _confirmed_non_short(raw):
                continue
            found_channels = _search_channels(raw)
            for profile_id in search_profiles:
                authorized = next((youtube_owners[profile_id][_owner_key(channel)] for channel in found_channels
                                   if _owner_key(channel) in youtube_owners[profile_id]), "")
                channel = authorized or next(iter(found_channels), "")
                video = _candidate(raw, "youtube", channel, now)
                if video:
                    video.update(discovery_only=not bool(authorized), discovered_via="youtube_search")
                    add(profile_id, video)
    measured = await _measure(db, unique_videos, now)
    cohorts = defaultdict(dict)
    for row in rows.values():
        video = row["video"]
        rate, _ = measured[video["video_id"]]
        if rate is not None and rate > 0:
            cohorts[(video["provider"], video["channel_url"])][video["video_id"]] = rate
    for row in rows.values():
        video = row["video"]
        rate, at = measured[video["video_id"]]
        floor = MIN_VIEWS_PER_HOUR[video["provider"]]
        qualified = rate is not None and rate >= floor
        discovery_only = video.get("discovery_only", False)
        score = None if rate is None else 0.0
        if qualified:
            baseline = median(cohorts[(video["provider"], video["channel_url"])].values())
            score = round(50 * rate / (rate + floor) + 50 * rate / (rate + baseline), 3)
        status = "eligible" if qualified else ("collecting" if rate is None else "below_threshold")
        video.update(trend_ready=qualified and not discovery_only, trend_score=score, views_per_hour=rate,
                     trend_observed_at=at, trend_status="discovery_only" if discovery_only else status)
    return sorted(rows.values(), key=lambda row: (row["video"].get("discovery_only", False),
                                                  not row["video"]["trend_ready"], -(row["video"]["trend_score"] or 0),
                                                  -row["video"]["published_at"], str(row["profile_id"]), row["video"]["video_id"]))
