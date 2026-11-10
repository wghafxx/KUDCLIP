from fastapi import FastAPI, APIRouter, HTTPException, UploadFile, File, Form, Request, Depends
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import io
import time
import asyncio
import zipfile
import logging
import signal
from contextlib import suppress
from pathlib import Path
from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Literal
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
from PIL import Image, UnidentifiedImageError

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import pipeline
import gpu_runtime
from filedb import Database
import groq_pool
import ai_router
import local_asr
import hype_scout
from author_daily import AuthorDaily
from jobs import JobManager
from autopilot import Autopilot
from browser_posting import BrowserPosting, BrowserSession, PostingBlocked
from source_history import excluded_source_ids

data_dir = Path(os.environ.get('DATA_DIR') or ROOT_DIR / 'data')
db = Database(data_dir)

app = FastAPI()
api_router = APIRouter(prefix="/api")
manager = JobManager(db, pipeline.process_job)
enqueue_lock = asyncio.Lock()


async def enqueue_daily(profile, video):
    if await pipeline.needs_groq(db):
        keys = await groq_pool.get_key_status(db)
        if not any(k.get('free_tier_confirmed') and k.get('status') != 'disabled' for k in keys):
            raise groq_pool.NoGroqKeys('Нет ИИ для анализа: добавь бесплатные ключи Groq (раздел «ИИ и распознавание»)')
    return await _make_job(profile, video['url'], video['title'], video.get('thumbnail', ''),
                           video.get('duration') or 0, video['requested_clips'], 45, False,
                           autopilot_day=video.get('autopilot_day'),
                           autopilot_attempt_id=video.get('autopilot_attempt_id'), source_video=video)


autopilot = Autopilot(db, enqueue_daily)
browser_posting = BrowserPosting(db, pipeline.CLIPS_DIR, BrowserSession(data_dir / 'browser_profiles'))
author_daily = AuthorDaily(db, enqueue_daily)
author_posting = {}


async def author_poster(profile_id):
    """One isolated real-browser window (own profile folder and port) per author."""
    if profile_id not in author_posting:
        profiles = sorted(p['id'] for p in await db.profiles.find({}).to_list(None))
        if profile_id not in profiles:
            raise HTTPException(404, 'Автор не найден')
        poster = BrowserPosting(db, pipeline.CLIPS_DIR, BrowserSession(data_dir / 'browser_profiles' / f'author_{profile_id}'),
                                scope=profile_id, port=9400 + profiles.index(profile_id) % 500)
        author_posting[profile_id] = poster
        await poster.start()
    return author_posting[profile_id]


async def _thermal_shutdown(reason):
    app.state.thermal_stop_reason = reason
    logger.critical('%s. KudClip останавливается.', reason)
    try:
        await manager.stop(reason=reason)
    finally:
        request_shutdown = getattr(app.state, 'request_shutdown', None)
        if request_shutdown is not None:
            request_shutdown()
        else:
            signal.raise_signal(signal.SIGTERM)


@app.on_event('startup')
async def start_worker():
    try:
        await gpu_runtime.require_safe_temperature()
    except gpu_runtime.ThermalStop as exc:
        app.state.thermal_stop_reason = str(exc)
        raise RuntimeError(f'{exc}. Запуск KudClip остановлен.') from None
    await manager.start()
    await autopilot.start()
    await browser_posting.start()
    await author_daily.start()
    for profile in await db.profiles.find({}).to_list(None):
        if ((await db.app_settings.find_one({'id': 'main'})) or {}).get('author_posting', {}).get(profile['id']):
            await author_poster(profile['id'])
    app.state.thermal_monitor = asyncio.create_task(gpu_runtime.monitor(_thermal_shutdown))


@app.on_event('shutdown')
async def stop_worker():
    monitor = getattr(app.state, 'thermal_monitor', None)
    if monitor is not None:
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
    await manager.stop()
    await browser_posting.stop()
    await author_daily.stop()
    for poster in author_posting.values():
        await poster.stop()
    await autopilot.stop()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class ProfileSettings(BaseModel):
    font: str = "montserrat_italic"
    glow_color: str = "#06B6D4"
    saturation: float = Field(default=1.35, ge=0.5, le=2)
    clip_duration: int = 30
    clip_len_min: int = Field(default=20, ge=5, le=180)
    clip_len_max: int = Field(default=90, ge=5, le=180)
    clips_per_video: int = Field(default=1, ge=1, le=6)
    background: Literal['blur', 'crop'] = "blur"
    crop_x: float = Field(default=0.5, ge=0, le=1)
    ad_id: str = ""
    ad_enabled: bool = False
    ad_disclosure: Literal['', 'own', 'sponsored'] = ''
    youtube_trending: bool = False
    twitch_trending: bool = False
    retention_style: Literal['clean', 'split'] = 'clean'
    retention_loop_id: str = ""
    hook_sound: bool = False
    auto_videos_count: int = Field(default=1, ge=1, le=3)
    genre: Literal['auto', 'game', 'story', 'education', 'humor', 'debate'] = 'auto'
    audience: Literal['new', 'fans'] = 'new'
    selection_preferences: str = Field(default='', max_length=2000)
    review_before_send: bool = False
    visual_analysis: bool = True
    quality_threshold: float = Field(default=75, ge=0, le=100)
    banner_enabled: bool = False
    daily_enabled: bool = True
    sub_glow: bool = False
    sub_size: int = Field(default=55, ge=40, le=140)
    sub_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    sub_position: Literal['bottom', 'middle', 'top'] = 'bottom'
    sub_uppercase: bool = False
    sub_words: int = Field(default=3, ge=1, le=6)
    sub_animate: bool = False
    sub_outline: int = Field(default=4, ge=0, le=12)
    banner_position: Literal['top', 'middle', 'bottom'] = 'top'
    banner_scale: float = Field(default=0.67, ge=0.3, le=1)
    banner_mode: Literal['timed', 'always'] = 'timed'
    banner_seconds: float = Field(default=3.5, ge=1, le=30)
    stream_vods: bool = True
    blocked_streamers: List[str] = []
    youtube_queries: List[str] = Field(default_factory=list, max_length=20)
    title_banner: bool = True
    brand_label: str = Field(default="", max_length=48)
    brand_color: str = Field(default="#22DDEB", pattern=r"^#[0-9A-Fa-f]{6}$")
    brand_highlight: str = Field(default="#BFF84F", pattern=r"^#[0-9A-Fa-f]{6}$")
    brand_panel: str = Field(default="#09101D", pattern=r"^#[0-9A-Fa-f]{6}$")

    @model_validator(mode='after')
    def duration_order(self):
        if self.clip_len_min > self.clip_len_max:
            raise ValueError('Минимальная длина клипа должна быть не больше максимальной')
        return self


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    telegram_chat_id: Optional[str] = ""
    telegram_chat_ids: List[str] = []
    channels: List[str] = []
    twitch_channels: List[str] = []
    settings: ProfileSettings = ProfileSettings()


class Profile(ProfileCreate):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: str = Field(default_factory=now_iso)


class JobCreate(BaseModel):
    profile_id: str
    video_url: str
    video_title: str = ""
    video_thumbnail: str = ""
    video_duration: float = 0
    num_clips: int = Field(default=3, ge=1, le=6)
    clip_duration: int = 30
    auto_send: bool = True


@api_router.get("/")
async def root():
    return {"message": "KUDCLIP API работает", 'version': '3.0-autopilot'}


# ---------- Profiles ----------
@api_router.post("/profiles")
async def create_profile(body: ProfileCreate):
    profile = Profile(**body.model_dump())
    await db.profiles.insert_one(profile.model_dump())
    return profile


@api_router.get("/profiles")
async def list_profiles():
    items = await db.profiles.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    for item in items:
        item['settings'] = normalized_settings(item.get('settings'))
    return items


def normalized_settings(settings):
    result = {**ProfileSettings().model_dump(), **(settings or {})}
    result.update(font='montserrat_italic', banner_enabled=False)
    result['auto_videos_count'] = max(1, min(3, int(result.get('auto_videos_count') or 1)))
    return result


@api_router.get("/profiles/{profile_id}")
async def get_profile(profile_id: str):
    p = await db.profiles.find_one({"id": profile_id}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Профиль не найден")
    p['settings'] = normalized_settings(p.get('settings'))
    return p


@api_router.put("/profiles/{profile_id}")
async def update_profile(profile_id: str, body: ProfileCreate):
    res = await db.profiles.update_one({"id": profile_id}, {"$set": body.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Профиль не найден")
    return await db.profiles.find_one({"id": profile_id}, {"_id": 0})


class ProfilePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    telegram_chat_id: Optional[str] = None
    telegram_chat_ids: Optional[List[str]] = None
    channels: Optional[List[str]] = None
    twitch_channels: Optional[List[str]] = None
    settings: Optional[dict] = None


@api_router.patch('/profiles/{profile_id}')
async def patch_profile(profile_id: str, body: ProfilePatch):
    async with enqueue_lock:
        current = await db.profiles.find_one({'id': profile_id})
        if not current:
            raise HTTPException(404, 'Профиль не найден')
        fields = body.model_dump(exclude_none=True)
        if 'settings' in fields:
            try:
                fields['settings'] = ProfileSettings(**{**normalized_settings(current.get('settings')), **fields['settings']}).model_dump()
            except ValueError as exc:
                raise HTTPException(422, str(exc))
        await db.profiles.update_one({'id': profile_id}, {'$set': fields})
        return await db.profiles.find_one({'id': profile_id})


@api_router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    async with enqueue_lock:
        for job in await db.jobs.find({'profile_id': profile_id}).to_list(None):
            await manager.cancel(job['id'])
        for clip in await db.clips.find({'profile_id': profile_id}).to_list(None):
            (pipeline.CLIPS_DIR / clip['filename']).unlink(missing_ok=True)
        await db.clips.delete_many({'profile_id': profile_id})
        await db.processed.delete_many({'profile_id': profile_id})
        await db.profiles.delete_one({"id": profile_id})
        await db.jobs.delete_many({"profile_id": profile_id})
    return {"ok": True}


# ---------- Groq keys pool ----------
class GroqKeyIn(BaseModel):
    key: str = Field(min_length=10)
    free_tier_confirmed: bool = False
    scope_id: str = Field(default='', max_length=100)


@api_router.post("/groq/keys")
async def add_groq_key(body: GroqKeyIn):
    key = body.key.strip()
    if await db.groq_keys.find_one({"key": key}):
        raise HTTPException(400, "Такой ключ уже добавлен")
    doc = {
        "id": str(uuid.uuid4())[:8],
        "key": key,
        "disabled": False,
        "cooldown_until": 0,
        "day": pipeline._today(),
        "used_seconds": 0,
        "used_requests": 0,
        "created_at": now_iso(),
        'free_tier_confirmed': body.free_tier_confirmed,
        'scope_id': body.scope_id.strip(),
    }
    await db.groq_keys.insert_one(dict(doc))
    if body.free_tier_confirmed:
        await manager.recheck_quota()
    doc.pop("key")
    return doc


def _mask(key: str) -> str:
    return key[:4] + "…" + key[-4:] if len(key) > 12 else "***"


@api_router.get("/groq/keys")
async def list_groq_keys():
    return await groq_pool.get_key_status(db)


class GroqKeyPatch(BaseModel):
    free_tier_confirmed: bool
    scope_id: Optional[str] = Field(default=None, max_length=100)


@api_router.put('/groq/keys/{key_id}')
async def update_groq_key(key_id: str, body: GroqKeyPatch):
    key = await db.groq_keys.find_one({'id': key_id})
    if not key:
        raise HTTPException(404, 'Ключ не найден')
    fields = body.model_dump(exclude_none=True)
    if body.free_tier_confirmed:
        fields['billing_blocked'] = False
    await db.groq_keys.update_one({'id': key_id}, {'$set': fields})
    await manager.recheck_quota()
    return next(k for k in await groq_pool.get_key_status(db) if k['id'] == key_id)


@api_router.delete("/groq/keys/{key_id}")
async def delete_groq_key(key_id: str):
    result = await db.groq_keys.delete_one({"id": key_id})
    if result.deleted_count:
        await manager.recheck_quota()
    return {"ok": True}


# ---------- App settings (Telegram токен и т.п.) ----------
class AppSettingsIn(BaseModel):
    telegram_token: Optional[str] = None


@api_router.get("/settings")
async def get_settings():
    doc = await db.app_settings.find_one({"id": "main"}) or {}
    tok = (doc.get("telegram_token") or os.environ.get("TELEGRAM_TOKEN") or "").strip()
    return {"has_telegram_token": bool(tok), "telegram_token_mask": _mask(tok) if tok else "",
            'free_only': True, 'paid_fallbacks': False, 'source_duration_limit': 1800,
            'quota_reserve': 0.2}


@api_router.put("/settings")
async def put_settings(body: AppSettingsIn):
    fields = {}
    if body.telegram_token is not None:
        fields["telegram_token"] = body.telegram_token.strip()
    if fields:
        await db.app_settings.update_one({"id": "main"}, {"$set": fields}, upsert=True)
    return await get_settings()


# ---------- Ads ----------
@api_router.post("/ads")
async def upload_ad(file: UploadFile = File(...),
                    kind: Literal['video', 'banner', 'loop'] = Form('video'),
                    name: str = Form('', max_length=150)):
    ad_id = str(uuid.uuid4())[:8]
    ext = Path(file.filename or 'ad.mp4').suffix.lower()
    if kind != 'banner' and ext not in {'.mp4', '.mov', '.webm', '.mkv', '.m4v'}:
        raise HTTPException(415, 'Выберите видеофайл MP4, MOV, WebM, MKV или M4V')
    limit_mb = 10 if kind == 'banner' else 200
    content = bytearray()
    while chunk := await file.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > limit_mb * 1024 * 1024:
            raise HTTPException(413, f'Файл больше {limit_mb} МБ')
    if not content:
        raise HTTPException(422, 'Файл пуст')
    if kind == 'banner':
        try:
            with Image.open(io.BytesIO(content)) as artwork:
                if artwork.format not in {'PNG', 'JPEG', 'WEBP'} or max(artwork.size) > 4096:
                    raise ValueError('Invalid banner image')
                normalized = io.BytesIO()
                artwork.convert('RGBA').save(normalized, format='PNG')
                content = normalized.getvalue()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise HTTPException(422, 'Выберите изображение PNG, JPG или WebP размером до 4096 пикселей') from exc
        ext = '.png'
    filename = f'{ad_id}{ext}'
    pipeline.save_ad_file(filename, content)
    doc = {
        "id": ad_id,
        "filename": filename,
        "name": name.strip() or file.filename or filename,
        "kind": kind,
        "size_mb": round(len(content) / 1024 / 1024, 1),
        "created_at": now_iso(),
    }
    await db.ads.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


@api_router.get("/ads")
async def list_ads():
    ads = await db.ads.find({}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return [{"kind": "video", **ad} for ad in ads]


@api_router.get("/ads/{ad_id}/file")
async def ad_file(ad_id: str):
    ad = await db.ads.find_one({"id": ad_id})
    if not ad:
        raise HTTPException(404, "Реклама не найдена")
    path = pipeline.ADS_DIR / ad["filename"]
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    if ad.get('kind') == 'banner':
        return FileResponse(path, media_type='image/png')
    return FileResponse(path, filename=ad['name'], content_disposition_type='inline')


@api_router.delete("/ads/{ad_id}")
async def delete_ad(ad_id: str):
    ad = await db.ads.find_one({"id": ad_id})
    if ad:
        (pipeline.ADS_DIR / ad["filename"]).unlink(missing_ok=True)
        await db.ads.delete_one({"id": ad_id})
    return {"ok": True}


# ---------- Channel videos ----------
@api_router.get("/channel/videos")
async def channel_videos(url: str, limit: int = 30):
    try:
        videos = await pipeline.list_channel_videos(url, limit)
        return {"videos": videos}
    except Exception as e:
        raise HTTPException(424, f"Не удалось получить видео с канала: {str(e)[:300]}")


# ---------- Jobs ----------
async def _make_job(profile: dict, video_url: str, video_title: str, video_thumbnail: str,
                    video_duration: float, num_clips: int, clip_duration: int, auto_send: bool, clip_edit=None,
                    autopilot_day=None, autopilot_attempt_id=None, source_video=None):
    async with enqueue_lock:
        profile = await db.profiles.find_one({'id': profile['id']})
        if not profile:
            raise HTTPException(404, 'Профиль удалён')
        if autopilot_attempt_id:
            existing = await db.jobs.find_one({'autopilot_attempt_id': autopilot_attempt_id})
            if existing:
                return existing
        if not clip_edit and not autopilot_attempt_id:
            for existing in await db.jobs.find({'profile_id': profile['id']}).to_list(None):
                if (pipeline.extract_video_id(existing.get('video_url', '')) == pipeline.extract_video_id(video_url)
                        and existing.get('status') not in {'done', 'review', 'error', 'cancelled'}):
                    return existing
        return await _insert_job(profile, video_url, video_title, video_thumbnail, video_duration,
                                 num_clips, clip_duration, auto_send, clip_edit, autopilot_day, autopilot_attempt_id, source_video)


async def _insert_job(profile, video_url, video_title, video_thumbnail, video_duration,
                      num_clips, clip_duration, auto_send, clip_edit=None, autopilot_day=None, autopilot_attempt_id=None,
                      source_video=None):
    job_id = str(uuid.uuid4())[:8]
    job = {
        "id": job_id,
        "profile_id": profile["id"],
        "video_url": video_url,
        "video_title": video_title,
        "video_thumbnail": video_thumbnail,
        "video_duration": video_duration,
        'settings_snapshot': normalized_settings(profile.get('settings')),
        "params": {
            "num_clips": num_clips,
            "clip_duration": clip_duration,
            "auto_send": auto_send,
            'clip_edit': clip_edit,
        },
        "status": "queued",
        "status_text": "В очереди...",
        "progress": 0,
        "clip_ids": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
        'autopilot_day': autopilot_day,
        'autopilot_attempt_id': autopilot_attempt_id,
        'source_video': source_video,
    }
    await db.jobs.insert_one(dict(job))
    manager.notify()
    job.pop("_id", None)
    return job


@api_router.post("/jobs")
async def create_job(body: JobCreate):
    profile = await db.profiles.find_one({"id": body.profile_id})
    if not profile:
        raise HTTPException(404, "Профиль не найден")
    return await _make_job(profile, body.video_url, body.video_title, body.video_thumbnail,
                           body.video_duration, body.num_clips, body.clip_duration, body.auto_send)


@api_router.get("/jobs")
async def list_jobs(profile_id: Optional[str] = None):
    q = {"profile_id": profile_id} if profile_id else {}
    return await db.jobs.find(q, {"_id": 0}).sort("created_at", -1).to_list(100)


@api_router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    await manager.cancel(job_id)
    await db.jobs.delete_one({"id": job_id})
    return {"ok": True}


@api_router.post('/jobs/{job_id}/cancel')
async def cancel_job(job_id: str):
    if not await db.jobs.find_one({'id': job_id}):
        raise HTTPException(404, 'Задача не найдена')
    await manager.cancel(job_id)
    return await db.jobs.find_one({'id': job_id})


@api_router.post('/jobs/{job_id}/retry')
async def retry_job(job_id: str):
    job = await manager.retry(job_id)
    if not job:
        raise HTTPException(404, 'Задача не найдена')
    return job


class AutopilotSettings(BaseModel):
    enabled: Optional[bool] = None
    hour: Optional[int] = Field(default=None, ge=0, le=23)
    minute: Optional[int] = Field(default=None, ge=0, le=59)
    daily_target: Optional[int] = Field(default=None, ge=1, le=30)
    max_source_videos: Optional[int] = Field(default=None, ge=1, le=15)
    continuous: Optional[bool] = None
    chat_analysis: Optional[bool] = None
    fresh_hours: Optional[int] = Field(default=None, ge=6, le=96)
    post_interval_minutes: Optional[int] = Field(default=None, ge=15, le=720)


class AiSettings(BaseModel):
    llm: Optional[Literal['auto', 'ollama', 'groq']] = None
    asr: Optional[Literal['auto', 'local', 'groq']] = None
    ollama_model: Optional[str] = Field(default=None, max_length=80)
    whisper_model: Optional[Literal['large-v3-turbo', 'large-v3', 'medium', 'small']] = None


async def _ai_status():
    config = await ai_router.settings(db)
    name, _ = await ai_router.provider(db)
    keys = await groq_pool.get_key_status(db)
    return {**config, 'active_llm': name, 'local_gpu': local_asr.available(), 'ollama_ready': await ai_router.ollama_ready(config),
            'groq_keys': sum(1 for k in keys if k.get('free_tier_confirmed') and k.get('status') != 'disabled')}


@api_router.get('/ai')
async def ai_status():
    return await _ai_status()


@api_router.put('/ai')
async def ai_update(body: AiSettings):
    current = ((await db.app_settings.find_one({'id': 'main'})) or {}).get('ai') or {}
    patch = body.model_dump(exclude_none=True)
    await db.app_settings.update_one({'id': 'main'}, {'$set': {'ai': {**current, **patch}}}, upsert=True)
    return await _ai_status()


class StreamerCheck(BaseModel):
    channels: List[str] = Field(default_factory=list, max_length=40)


@api_router.post('/streamers/check')
async def streamers_check(body: StreamerCheck):
    logins = list(dict.fromkeys(login for c in body.channels if (login := hype_scout.login_of(c))))
    return {'streamers': await hype_scout.check_streamers(logins)}


@api_router.get('/autopilot')
async def autopilot_status():
    return await autopilot.status()


@api_router.put('/autopilot')
async def autopilot_update(body: AutopilotSettings):
    return await autopilot.update(body.model_dump(exclude_none=True))


@api_router.post('/autopilot/run')
async def autopilot_run_now():
    return await autopilot.run_now()


def local_posting_panel(request: Request):
    local = {'localhost', '127.0.0.1', '::1'}
    origin = request.headers.get('origin')
    if (not request.client or request.client.host not in local or request.url.hostname not in local
            or (origin and urlsplit(origin).hostname not in local)):
        raise HTTPException(403, 'Автопост можно настроить только на этом компьютере.')


posting_router = APIRouter(prefix='/browser-posting', dependencies=[Depends(local_posting_panel)])
PostingPlatform = Literal['youtube', 'tiktok']


class BrowserPostingSettings(BaseModel):
    browser: Literal['edge', 'chrome', 'opera', 'octo'] = 'edge'
    debug_address: str = Field(default='', max_length=200)


class BrowserPostingToggle(BaseModel):
    enabled: bool


async def posting_action(operation):
    try:
        return await operation
    except PostingBlocked as exc:
        raise HTTPException(409, str(exc)) from None


@posting_router.get('')
async def browser_posting_status():
    return await browser_posting.status()


@posting_router.put('')
async def browser_posting_config(body: BrowserPostingSettings):
    return await posting_action(browser_posting.configure(body.browser, body.debug_address))


@posting_router.post('/{platform}/open')
async def browser_posting_open(platform: PostingPlatform):
    await posting_action(browser_posting.open(platform))
    return {'ok': True}


@posting_router.post('/{platform}/bind')
async def browser_posting_bind(platform: PostingPlatform):
    return await posting_action(browser_posting.bind(platform))


@posting_router.put('/{platform}')
async def browser_posting_toggle(platform: PostingPlatform, body: BrowserPostingToggle):
    return await posting_action(browser_posting.enable(platform, body.enabled))


@posting_router.post('/{platform}/retry')
async def browser_posting_retry(platform: PostingPlatform):
    return await posting_action(browser_posting.retry(platform))


api_router.include_router(posting_router)

authors_router = APIRouter(prefix='/authors', dependencies=[Depends(local_posting_panel)])


class AuthorDailySettings(BaseModel):
    enabled: Optional[bool] = None
    hour: Optional[int] = Field(default=None, ge=0, le=23)
    minute: Optional[int] = Field(default=None, ge=0, le=59)
    fresh_hours: Optional[int] = Field(default=None, ge=6, le=96)
    clips_per_source: Optional[int] = Field(default=None, ge=1, le=2)
    youtube_clips: Optional[int] = Field(default=None, ge=4, le=8)
    twitch_clips: Optional[int] = Field(default=None, ge=7, le=10)
    post_interval_minutes: Optional[int] = Field(default=None, ge=20, le=720)
    chat_analysis: Optional[bool] = None


class AuthorProxy(BaseModel):
    proxy: str = Field(default='', max_length=120, pattern=r'^((https?|socks5)://)?[\w.-]+:\d{2,5}$|^$')


class AuthorRunOptions(BaseModel):
    reserve_only: bool = False


@authors_router.get('/schedule')
async def authors_schedule():
    return await author_daily.status()


@authors_router.put('/schedule')
async def authors_schedule_update(body: AuthorDailySettings):
    return await author_daily.update(body.model_dump(exclude_none=True))


@authors_router.post('/run')
async def authors_run(body: Optional[AuthorRunOptions] = None):
    return await author_daily.run_now(reserve_only=bool(body and body.reserve_only))


@authors_router.get('/reports')
async def authors_reports():
    return {'days': await author_daily.reports()}


@authors_router.get('/{profile_id}/posting')
async def author_posting_status(profile_id: str):
    return await (await author_poster(profile_id)).status()


@authors_router.post('/{profile_id}/posting/{platform}/open')
async def author_posting_open(profile_id: str, platform: PostingPlatform):
    await posting_action((await author_poster(profile_id)).open(platform))
    return {'ok': True}


@authors_router.post('/{profile_id}/posting/{platform}/bind')
async def author_posting_bind(profile_id: str, platform: PostingPlatform):
    return await posting_action((await author_poster(profile_id)).bind(platform))


@authors_router.put('/{profile_id}/posting/{platform}')
async def author_posting_toggle(profile_id: str, platform: PostingPlatform, body: BrowserPostingToggle):
    return await posting_action((await author_poster(profile_id)).enable(platform, body.enabled))


@authors_router.post('/{profile_id}/posting/{platform}/retry')
async def author_posting_retry(profile_id: str, platform: PostingPlatform):
    return await posting_action((await author_poster(profile_id)).retry(platform))


@authors_router.put('/{profile_id}/proxy')
async def author_proxy(profile_id: str, body: AuthorProxy):
    return await posting_action((await author_poster(profile_id)).set_proxy(body.proxy.strip()))


api_router.include_router(authors_router)


# ---------- Auto-start ----------
async def _autostart_profile(profile: dict):
    settings = profile.get("settings", {})
    count = int(settings.get("auto_videos_count", 1))
    channels = profile.get("channels") or []
    if not channels:
        return {"profile_id": profile["id"], "profile_name": profile["name"], "created": 0, "error": "Нет каналов в профиле"}

    all_videos = []
    errors = []
    for ch in channels:
        try:
            vids = await pipeline.list_channel_videos(ch, 15)
            all_videos.extend(vids)
        except Exception as e:
            errors.append(f"{ch}: {str(e)[:100]}")

    done_ids = await excluded_source_ids(db, profile["id"])
    by_id = {v['video_id']: v for v in all_videos}
    candidates = [v for v in by_id.values() if v['video_id'] not in done_ids and 30 <= (v.get('duration') or 0) <= 1800]
    candidates.sort(key=lambda v: v["views"], reverse=True)
    picked = candidates[:count]

    jobs = []
    for v in picked:
        job = await _make_job(
            profile, v["url"], v["title"], v["thumbnail"], v.get("duration") or 0,
            int(settings.get("clips_per_video", 3)), int(settings.get("clip_duration", 30)), True,
        )
        jobs.append(job["id"])

    return {
        "profile_id": profile["id"],
        "profile_name": profile["name"],
        "created": len(jobs),
        "job_ids": jobs,
        "picked": [{"title": v["title"], "views": v["views"]} for v in picked],
        "error": "; ".join(errors) if errors and not jobs else None,
    }


@api_router.post("/autostart/{profile_id}")
async def autostart(profile_id: str):
    profile = await db.profiles.find_one({"id": profile_id})
    if not profile:
        raise HTTPException(404, "Профиль не найден")
    return await _autostart_profile(profile)


@api_router.post("/autostart-all")
async def autostart_all():
    profiles = await db.profiles.find({}).to_list(200)
    results = []
    for p in profiles:
        try:
            results.append(await _autostart_profile(p))
        except Exception as e:
            results.append({"profile_id": p["id"], "profile_name": p.get("name", ""), "created": 0, "error": str(e)[:200]})
    return {"results": results, "total_jobs": sum(r.get("created", 0) for r in results)}


# ---------- Clips ----------
@api_router.get("/clips")
async def list_clips(profile_id: Optional[str] = None):
    q = {"profile_id": profile_id} if profile_id else {}
    return await db.clips.find(q, {"_id": 0}).sort("created_at", -1).to_list(300)


@api_router.get("/clips/{clip_id}/file")
async def clip_file(clip_id: str):
    clip = await db.clips.find_one({"id": clip_id})
    if not clip:
        raise HTTPException(404, "Клип не найден")
    path = pipeline.CLIPS_DIR / clip["filename"]
    if not path.exists():
        raise HTTPException(404, "Файл клипа не найден")
    return FileResponse(path, media_type="video/mp4", filename=clip["filename"])


@api_router.get('/clips/{clip_id}/cover')
async def cover_file(clip_id: str):
    return await clip_asset(clip_id, 'cover_filename', 'image/jpeg')


@api_router.get('/clips/{clip_id}/banner')
async def banner_file(clip_id: str):
    return await clip_asset(clip_id, 'banner_filename', 'image/png')


@api_router.get('/clips/{clip_id}/package')
async def package_file(clip_id: str):
    return await clip_asset(clip_id, 'package_filename', 'application/zip')


async def clip_asset(clip_id, field, media_type):
    clip = await db.clips.find_one({'id': clip_id})
    name = (clip or {}).get(field)
    if not name or not (pipeline.CLIPS_DIR / name).is_file():
        raise HTTPException(404, 'Готовый файл пока отсутствует')
    return FileResponse(pipeline.CLIPS_DIR / name, media_type=media_type, filename=name)


@api_router.post("/clips/{clip_id}/send")
async def send_clip(clip_id: str, chat_id: Optional[str] = None, force: bool = False):
    clip = await db.clips.find_one({"id": clip_id})
    if not clip:
        raise HTTPException(404, "Клип не найден")
    profile = await db.profiles.find_one({"id": clip["profile_id"]}) or {}
    recipients = [chat_id] if chat_id else pipeline._recipients(profile)
    if not recipients:
        raise HTTPException(400, "Получатели не указаны. Добавьте Telegram Chat ID в профиль.")
    path = pipeline.CLIPS_DIR / clip["filename"]
    if not path.exists():
        raise HTTPException(404, "Файл клипа не найден")
    sent, errors = await pipeline.deliver_clip(db, clip, recipients, force=force)
    if not sent and errors:
        raise HTTPException(424, '; '.join(f'{rid}: {error}' for rid, error in errors.items())[:700])
    return {'ok': True, 'sent': sent, 'errors': [f'{rid}: {error}' for rid, error in errors.items()],
            'already_sent': not sent and not errors}


class ClipFeedback(BaseModel):
    rating: Literal['good', 'bad']
    reason: str = Field(default='', max_length=1000)


@api_router.post('/clips/{clip_id}/feedback')
async def clip_feedback(clip_id: str, body: ClipFeedback):
    if not await db.clips.find_one({'id': clip_id}):
        raise HTTPException(404, 'Клип не найден')
    await db.clips.update_one({'id': clip_id}, {'$set': {'feedback': {**body.model_dump(), 'updated_at': now_iso()}}})
    return await db.clips.find_one({'id': clip_id})


class ClipEdit(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: Optional[str] = Field(default=None, max_length=100)
    caption: Optional[str] = Field(default=None, max_length=1000)
    subtitle_text: Optional[str] = Field(default=None, max_length=10000)
    crop_x: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode='after')
    def valid_range(self):
        if not 5 <= self.end - self.start <= 180:
            raise ValueError('Выберите от 5 до 180 секунд')
        return self


@api_router.post('/clips/{clip_id}/edit')
async def edit_clip(clip_id: str, body: ClipEdit):
    clip = await db.clips.find_one({'id': clip_id})
    if not clip:
        raise HTTPException(404, 'Клип не найден')
    profile = await db.profiles.find_one({'id': clip['profile_id']})
    if not profile:
        raise HTTPException(404, 'Профиль удалён')
    source = await db.jobs.find_one({'id': clip['job_id']}) or {}
    url = clip.get('video_url') or source.get('video_url')
    duration = float(clip.get('source_duration') or source.get('video_duration') or 0)
    if not url or (duration and body.end > duration + 0.1):
        raise HTTPException(422, 'Границы выходят за исходное видео или источник недоступен')
    edit = body.model_dump(exclude_none=True)
    edit['title'] = body.title or clip.get('title', 'Клип')
    edit['hashtags'] = clip.get('hashtags') or pipeline.DEFAULT_HASHTAGS
    edit['original_clip_id'] = clip_id
    return await _make_job(profile, url, edit['title'], source.get('video_thumbnail', ''), duration,
                           1, int(body.end - body.start), False, clip_edit=edit)


@api_router.delete("/clips/{clip_id}")
async def delete_clip(clip_id: str):
    clip = await db.clips.find_one({"id": clip_id})
    if clip:
        (pipeline.CLIPS_DIR / clip["filename"]).unlink(missing_ok=True)
        await db.clips.delete_one({"id": clip_id})
    return {"ok": True}


# ---------- Telegram helpers ----------
@api_router.get("/telegram/chats")
async def telegram_chats():
    try:
        return {"chats": await pipeline.get_telegram_chats(db)}
    except Exception as e:
        raise HTTPException(424, str(e)[:300])


# ---------- Source archive ----------
EXCLUDE_DIRS = {"node_modules", "__pycache__", ".git", "build", "media", ".cache", "coverage", "data"}
APP_ROOT = ROOT_DIR.parent


@api_router.get("/source/archive")
async def source_archive():
    archive_path = pipeline.MEDIA / "kudclip_source.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for base in [APP_ROOT / "backend", APP_ROOT / "frontend", APP_ROOT / "README.md",
                     APP_ROOT / "ЗАПУСК_WINDOWS.md", APP_ROOT / "ЗАПУСК_WINDOWS.bat"]:
            if base.is_file():
                zf.write(base, base.name)
                continue
            if not base.exists():
                continue
            for f in base.rglob("*"):
                if f.is_dir():
                    continue
                if any(part in EXCLUDE_DIRS for part in f.parts):
                    continue
                if f.name == ".env":
                    continue
                if f.suffix in (".zip", ".mp3", ".log"):
                    continue
                if f.suffix == ".mp4" and f.name != "background.mp4":
                    continue
                zf.write(f, str(f.relative_to(APP_ROOT)))
    archive_path.write_bytes(buf.getvalue())
    return FileResponse(archive_path, media_type="application/zip", filename="kudclip_source.zip")


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

