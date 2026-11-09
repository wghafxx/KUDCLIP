"""Free-plan Groq scheduler. It never switches to a paid provider or service tier.

Only keys explicitly attested with ``free_tier_confirmed is True`` may be used.
This is an account-owner attestation, not an API-based billing-plan check: Groq's
inference API does not establish whether the account has paid billing enabled.
Keep those accounts on Groq's Free plan / with paid spending disabled.

Published caps: https://console.groq.com/docs/rate-limits (2026-11-06).
The constants below deliberately leave 20% unused. Local consumption uses rolling
windows, independently of the UTC-day counters shown in the interface. Response
headers can LOWER these caps; they never enable more spending or bigger limits.
``scope_id`` (or legacy ``scope`` / ``organization_id``) groups keys belonging to
one organization. Without it, each key is treated as a separate account.

State lives in ``db.groq_usage``. A durable reservation is written before sending
each request. A timeout, process interruption, or missing usage receipt retains
that reservation. A single-process asyncio lock protects selection and updates;
run one backend worker (the file database is not a multiprocess queue).
"""

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


GROQ_BASE = "https://api.groq.com/openai/v1"
RESERVE_FRACTION = 0.20
DEFAULT_MAX_TOKENS = 1024
REQUEST_TIMEOUT = 300.0
_LOCK = asyncio.Lock()

# Adjust downward for an account with smaller limits. Do not raise these to the
# Developer-plan values. Unknown model ids are rejected without an HTTP request.
_TEXT_CAPS = {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000}
_AUDIO_CAPS = {"rpm": 20, "rpd": 2000, "ash": 7200, "asd": 28800}
FREE_LIMITS = {
    "qwen/qwen3.8-27b": dict(_TEXT_CAPS),
    "openai/gpt-oss-20b": dict(_TEXT_CAPS),
    "openai/gpt-oss-120b": dict(_TEXT_CAPS),
    # A legacy model may return model_not_found; it is never silently replaced.
    "llama-3.3-70b-versatile": dict(_TEXT_CAPS),
    "whisper-large-v3": dict(_AUDIO_CAPS),
    "whisper-large-v3-turbo": dict(_AUDIO_CAPS),
}
_RESOURCES = {
    "rpm": ("requests", 60), "rpd": ("requests", 86400),
    "tpm": ("tokens", 60), "tpd": ("tokens", 86400),
    "ash": ("audio_seconds", 3600), "asd": ("audio_seconds", 86400),
}
_HEADER_RESOURCES = {
    # Groq explicitly documents requests as RPD and tokens as TPM, not RPM/TPD.
    "requests": "rpd", "tokens": "tpm",
    "audio-seconds-hour": "ash", "audio-seconds-day": "asd",
}


class GroqUnavailable(RuntimeError):
    pass


class NoGroqKeys(GroqUnavailable):
    pass


class GroqModelUnavailable(GroqUnavailable):
    pass


class GroqDeferred(GroqUnavailable):
    def __init__(self, retry_at, reason):
        self.retry_at = float(retry_at)  # Unix UTC seconds; persist this in jobs.
        self.reason = str(reason)
        super().__init__(self.reason)

    @property
    def retry_after(self):
        return max(0.0, self.retry_at - time.time())


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def parse_duration(value):
    """Seconds from Retry-After or Groq durations such as '1h2m3.45s'."""
    if value is None:
        return None
    value = str(value).strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return _number(value)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*(ms|d|h|m|s)", value)
    if not parts or re.sub(r"(\d+(?:\.\d+)?)\s*(ms|d|h|m|s)", "", value).strip():
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": .001}
    result = sum(float(amount) * units[unit] for amount, unit in parts)
    return result if math.isfinite(result) else None


def _deadline(value, now):
    seconds = parse_duration(value)
    if seconds is not None:
        return now + seconds
    try:
        date = parsedate_to_datetime(str(value))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(now, date.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _day(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d")


def _scope(key):
    shared = key.get("scope_id") or key.get("scope") or key.get("organization_id")
    return "shared:" + str(shared).strip() if shared and str(shared).strip() else "key:" + str(key["id"])


def _state_id(scope, model):
    return hashlib.sha256((scope + "\0" + model).encode()).hexdigest()


def _clean_content(value):
    """Remove image data/URLs from the text estimate, counting images separately."""
    if isinstance(value, dict):
        if value.get("type") in ("image_url", "input_image", "image"):
            return "", 1
        clean, images = {}, 0
        for key, item in value.items():
            item, count = _clean_content(item)
            clean[key] = item
            images += count
        return clean, images
    if isinstance(value, list):
        clean, images = [], 0
        for item in value:
            item, count = _clean_content(item)
            clean.append(item)
            images += count
        return clean, images
    return value, 0


def estimate_tokens(json_body):
    """Planner estimate, including output reserve; actual usage replaces it later.

    UTF-8 bytes / 2 + message overhead + max output + 2048 per image. Base64
    image bytes are deliberately excluded. This is not a model tokenizer.
    """
    body = json_body or {}
    content = {k: body[k] for k in ("messages", "tools", "functions", "response_format") if k in body}
    clean, images = _clean_content(content)
    byte_count = len(json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    maximum = max(_number(body.get("max_tokens")), _number(body.get("max_completion_tokens"))) or DEFAULT_MAX_TOKENS
    return math.ceil(byte_count / 2) + 128 + 16 * len(body.get("messages") or []) + math.ceil(maximum) + images * 2048


def _prepare(path, data, json_body, audio_seconds):
    if path not in ("/chat/completions", "/audio/transcriptions", "/audio/translations"):
        raise GroqUnavailable("Разрешены только стандартные Groq chat и Whisper запросы.")
    body = dict(json_body or {}) if path == "/chat/completions" else dict(data or {})
    model = body.get("model")
    if model not in FREE_LIMITS:
        raise GroqModelUnavailable("Модель не входит в разрешённый список бесплатного Groq.")
    if body.get("service_tier") not in (None, "on_demand"):
        raise GroqUnavailable("Платные и ускоренные service_tier запрещены в бесплатном режиме.")
    if body.get("stream"):
        raise GroqUnavailable("Для учёта квот используйте запрос без stream.")
    audio = path.startswith("/audio/")
    if audio != ("ash" in FREE_LIMITS[model]):
        raise GroqModelUnavailable("Модель не поддерживается для этого типа Groq-запроса.")
    if audio:
        seconds = _number(audio_seconds)
        if seconds <= 0:
            raise GroqUnavailable("Для Whisper укажите длительность аудио до отправки запроса.")
        cost = {"requests": 1, "audio_seconds": max(10, math.ceil(seconds)), "tokens": 0}
    else:
        if not body.get("max_tokens") and not body.get("max_completion_tokens"):
            body["max_tokens"] = DEFAULT_MAX_TOKENS
        cost = {"requests": 1, "audio_seconds": 0, "tokens": estimate_tokens(body)}
    return model, body, cost


async def _load_state(db, scope, model, now):
    state = await db.groq_usage.find_one({"id": _state_id(scope, model)}) or {
        "id": _state_id(scope, model), "scope": scope, "model": model,
        "events": [], "blocks": {}, "provider": {}, "provider_limits": {},
    }
    state["events"] = [e for e in state.get("events", []) if _number(e.get("at")) > now - 86400]
    return state


async def _save_state(db, state):
    await db.groq_usage.update_one({"id": state["id"]}, {"$set": state}, upsert=True)


def _cap(model, resource, state):
    published = FREE_LIMITS[model][resource]
    reported = _number(state.get("provider_limits", {}).get(resource), published)
    return max(1, math.floor(min(published, reported or published) * (1 - RESERVE_FRACTION)))


def _availability(model, state, cost, now):
    ready, reason = now, "Бесплатная квота Groq временно исчерпана."
    for resource, until in state.get("blocks", {}).items():
        if _number(until) > ready:
            ready, reason = float(until), f"Groq: ожидается сброс лимита {resource}."
    for resource in FREE_LIMITS[model]:
        field, window = _RESOURCES[resource]
        needed, cap = cost[field], _cap(model, resource, state)
        if needed > cap:
            raise GroqUnavailable(f"Один запрос превышает бесплатный бюджет {resource}; разбейте аудио/текст на меньшие части.")
        events = sorted((e for e in state["events"] if e["at"] > now - window), key=lambda e: e["at"])
        used = sum(_number(e.get(field)) for e in events)
        if used + needed > cap:
            for event in events:
                used -= _number(event.get(field))
                if used + needed <= cap:
                    until = event["at"] + window
                    if until > ready:
                        ready, reason = until, f"Groq: локальный бесплатный бюджет {resource} исчерпан."
                    break
        provider = state.get("provider", {}).get(resource, {})
        if provider.get("reset_at", 0) > now and provider.get("remaining", 0) < needed:
            if provider["reset_at"] > ready:
                ready, reason = provider["reset_at"], f"Groq: ожидается сброс лимита {resource}."
    return ready, reason


async def _refresh_day(db, key, now):
    if key.get("day") != _day(now):
        fields = {"day": _day(now), "used_seconds": 0, "used_requests": 0, "used_tokens": 0}
        await db.groq_keys.update_one({"id": key["id"]}, {"$set": fields})
        key.update(fields)  # Never reset provider blocks or rolling budgets here.


def _key_pause(key, now):
    return max(now, _number(key.get("cooldown_until")), _number(key.get("paused_until")))


def _eligible(key):
    return (key.get("free_tier_confirmed") is True and not key.get("disabled")
            and not key.get("billing_blocked") and not key.get("paused")
            and key.get("enabled") is not False and bool(key.get("key")))


async def _select(db, model, cost, excluded, now):
    keys = await db.groq_keys.find({}).to_list(500)
    choices, waiting, oversized = [], [], []
    for key in keys:
        if not _eligible(key):
            continue
        await _refresh_day(db, key, now)
        state = await _load_state(db, _scope(key), model, now)
        try:
            at, reason = _availability(model, state, cost, now)
        except GroqUnavailable as error:
            oversized.append(error)
            continue
        at = max(at, _key_pause(key, now))
        if at > now:
            waiting.append((at, reason))
        elif key["id"] not in excluded:
            load = sum(_number(e.get("audio_seconds")) + _number(e.get("tokens")) / 100 for e in state["events"])
            choices.append((load, str(key["id"]), key, state))
    if choices:
        _, _, key, state = min(choices, key=lambda item: (item[0], item[1]))
        return key, state
    if waiting:
        at, reason = min(waiting, key=lambda item: item[0])
        raise GroqDeferred(at, reason)
    if oversized:
        raise oversized[0]
    if any(k.get("free_tier_confirmed") is not True and not k.get("disabled") for k in keys):
        raise NoGroqKeys("Подтвердите бесплатный тариф Groq для ключа в интерфейсе; неподтверждённые ключи не используются.")
    raise NoGroqKeys("Нет включённых бесплатных Groq-ключей. Проверьте паузу, ключи и подтверждение бесплатного тарифа.")


async def _reserve(db, key, state, cost, now):
    event = {"id": uuid.uuid4().hex, "key_id": key["id"], "at": now, **cost, "status": "reserved"}
    state["events"].append(event)
    for resource, snapshot in state.get("provider", {}).items():
        if snapshot.get("reset_at", 0) > now:
            snapshot["remaining"] = max(0, snapshot["remaining"] - cost[_RESOURCES[resource][0]])
    await _save_state(db, state)
    await db.groq_keys.update_one({"id": key["id"]}, {"$inc": {
        "used_requests": 1, "used_seconds": cost["audio_seconds"], "used_tokens": cost["tokens"],
    }})
    return event


def _update_headers(state, headers, model, now):
    for suffix, resource in _HEADER_RESOURCES.items():
        if resource not in FREE_LIMITS[model]:
            continue
        limit = _number(headers.get("x-ratelimit-limit-" + suffix))
        if limit:
            previous = state.setdefault("provider_limits", {}).get(resource, FREE_LIMITS[model][resource])
            state["provider_limits"][resource] = min(previous, limit)
        raw_remaining = headers.get("x-ratelimit-remaining-" + suffix)
        reset = _deadline(headers.get("x-ratelimit-reset-" + suffix), now)
        if raw_remaining is None or reset is None or reset <= now:
            continue
        reserve = math.ceil(min(limit or FREE_LIMITS[model][resource], FREE_LIMITS[model][resource]) * RESERVE_FRACTION)
        # A concurrently dispatched call might not yet be reflected in this
        # response's header. Keep all other in-flight reservations as well.
        pending = sum(_number(e.get(_RESOURCES[resource][0])) for e in state.get("events", [])
                      if e.get("status") == "reserved")
        remaining = max(0, _number(raw_remaining) - reserve - pending)
        old = state.setdefault("provider", {}).get(resource, {})
        if old.get("reset_at", 0) > now:
            remaining = min(remaining, old.get("remaining", remaining))
            reset = max(reset, old["reset_at"])
        state["provider"][resource] = {"remaining": remaining, "reset_at": reset}


def _rate_blocks(response, now):
    headers = response.headers
    retry = _deadline(headers.get("retry-after"), now)
    exhausted = {}
    for suffix, resource in _HEADER_RESOURCES.items():
        remaining = headers.get("x-ratelimit-remaining-" + suffix)
        if remaining is not None and _number(remaining, float("inf")) <= 0:
            reset = _deadline(headers.get("x-ratelimit-reset-" + suffix), now)
            fallback = now + _RESOURCES[resource][1]
            exhausted[resource] = max(retry or now, reset or retry or fallback)
    if exhausted:
        return exhausted
    # No exhausted-resource header: use Retry-After or a duration in Groq's body.
    # Never expose the raw body (it can contain account details or credentials).
    text = response.text.lower()
    match = re.search(r"try again in\s+((?:\d+(?:\.\d+)?\s*(?:ms|d|h|m|s)\s*)+)", text)
    body_seconds = parse_duration(match.group(1).strip()) if match else None
    if retry is not None or body_seconds is not None:
        return {"rate_limit": max(retry or now, now + (body_seconds or 0))}
    day_limit = any(marker in text for marker in ("per day", "requests/day", "tokens/day", "audio seconds per day", "(rpd)", "(tpd)", "(asd)"))
    return {"rate_limit": now + (86400 if day_limit else 60)}


def _error_kind(response):
    text = response.text.lower()
    try:
        error = response.json().get('error', {})
        code = str(error.get('code') or error.get('type') or '').lower() if isinstance(error, dict) else ''
    except (ValueError, AttributeError):
        code = ''
    explicit_billing = code in {'insufficient_credits', 'insufficient_credit', 'payment_required',
                                'billing_hard_limit_reached', 'blocked_api_access', 'spend_limit_reached'}
    # Groq's ordinary 429 messages may link to /settings/billing to advertise an
    # upgrade. Such a link is not a payment failure or a reason to disable Free.
    if response.status_code == 429 and not explicit_billing:
        return 'rate_limit'
    if explicit_billing:
        return 'billing'
    if response.status_code == 402 or any(marker in text for marker in (
        "insufficient_credit", "insufficient credit", "credit balance", "credit limit",
        "payment_required", "paid plan", "billing", "spend limit",
    )):
        return "billing"
    if response.status_code == 401 or (response.status_code == 403 and any(marker in text for marker in (
        "invalid_api_key", "invalid api key", "api_key_expired", "api key has expired", "authentication_error", "revoked api key",
    ))):
        return "auth"
    if response.status_code == 404 or (response.status_code in (400, 403) and "model" in text and any(marker in text for marker in (
        "not_found", "not found", "decommissioned", "does not exist", "does not have access", "model_permission", "permission to access",
    ))):
        return "model"
    return "other"


async def _finish(db, key, model, reservation, response, now):
    state = await _load_state(db, _scope(key), model, now)
    event = next((e for e in state["events"] if e["id"] == reservation["id"]), None)
    old_tokens, old_audio = reservation["tokens"], reservation["audio_seconds"]
    if event:
        event["status"] = "completed" if response.status_code < 400 else "uncertain"
        if 400 <= response.status_code < 500:
            event.update(tokens=0, audio_seconds=0, status="rejected")
        elif response.status_code < 400:
            try:
                receipt = response.json()
                usage = receipt.get("usage") or {}
                actual = usage.get("total_tokens")
                if actual is None and ("prompt_tokens" in usage or "completion_tokens" in usage):
                    actual = _number(usage.get("prompt_tokens")) + _number(usage.get("completion_tokens"))
                if actual is not None:
                    event["tokens"] = math.ceil(_number(actual, old_tokens))
                if old_audio and receipt.get("duration") is not None:
                    event["audio_seconds"] = max(old_audio, math.ceil(_number(receipt["duration"])))
            except (ValueError, AttributeError, TypeError):
                pass  # Missing/invalid receipt leaves the conservative reservation.
    _update_headers(state, response.headers, model, now)
    if response.status_code == 429:
        for resource, until in _rate_blocks(response, now).items():
            state.setdefault("blocks", {})[resource] = max(until, state.get("blocks", {}).get(resource, 0))
    await _save_state(db, state)
    current_key = await db.groq_keys.find_one({"id": key["id"]})
    if current_key:
        await _refresh_day(db, current_key, now)
        if event and _day(reservation["at"]) == _day(now):
            await db.groq_keys.update_one({"id": key["id"]}, {"$set": {
                "used_tokens": max(0, _number(current_key.get("used_tokens")) + event["tokens"] - old_tokens),
                "used_seconds": max(0, _number(current_key.get("used_seconds")) + event["audio_seconds"] - old_audio),
            }})


async def request(db, path, *, data=None, files=None, json_body=None, audio_seconds=0.0):
    """Return httpx.Response, or a typed failure; never sleep/retry blindly.

    Independent confirmed free accounts may be selected after 429/invalid-key.
    Keys in a shared scope share reservations and cooldowns. A timeout/5xx is not
    retried, since inference might already have consumed quota. Billing/credit
    errors immediately stop this call and block the affected account's keys.
    """
    model, body, cost = _prepare(path, data, json_body, audio_seconds)
    attempted = set()
    while True:
        async with _LOCK:
            now = time.time()
            key, state = await _select(db, model, cost, attempted, now)
            event = await _reserve(db, key, state, cost, now)
            attempted.add(key["id"])
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False, trust_env=False) as client:
                response = await client.post(
                    GROQ_BASE + path, headers={"Authorization": "Bearer " + key["key"]},
                    data=body if path.startswith("/audio/") else None, files=files,
                    json=body if path == "/chat/completions" else None,
                )
        except httpx.HTTPError:
            raise GroqUnavailable("Groq: ошибка сети или тайм-аут; запрос не повторён, резерв квоты сохранён.") from None
        async with _LOCK:
            await _finish(db, key, model, event, response, time.time())
            if response.status_code < 300:
                return response
            try:
                problem = response.json().get('error', {})
                if isinstance(problem, dict):
                    safe_message = re.sub(r'gsk_[A-Za-z0-9_-]+', '[key]', str(problem.get('message') or ''))[:700]
                    safe_message = re.sub(r'org_[A-Za-z0-9_-]+', '[organization]', safe_message)
                    await db.groq_usage.update_one({'id': state['id']}, {'$set': {'last_error': {
                        'status': response.status_code, 'code': str(problem.get('code') or ''),
                        'type': str(problem.get('type') or ''), 'message': safe_message}}})
            except (ValueError, AttributeError):
                pass
            kind = _error_kind(response)
            if kind == "billing":
                for account_key in await db.groq_keys.find({}).to_list(500):
                    if _scope(account_key) == _scope(key):
                        await db.groq_keys.update_one({"id": account_key["id"]}, {"$set": {"billing_blocked": True}})
                raise GroqUnavailable("Groq сообщил об оплате/кредитах. Запросы этого аккаунта остановлены; проверьте бесплатный тариф.")
            if kind == "auth":
                await db.groq_keys.update_one({"id": key["id"]}, {"$set": {"disabled": True}})
            elif kind == "model":
                raise GroqModelUnavailable("Groq: разрешённая модель недоступна или не разрешена для аккаунта.")
            elif response.status_code != 429:
                raise GroqUnavailable(f"Groq отклонил запрос (HTTP {response.status_code}); автоматического повторения нет.")


def _mask(raw):
    return raw[:4] + "…" + raw[-4:] if len(raw) > 12 else "***"


async def get_key_status(db):
    """Safe metadata only. UTC UI-counter reset never removes a provider pause."""
    async with _LOCK:
        now = time.time()
        keys = await db.groq_keys.find({}).to_list(500)
        states = await db.groq_usage.find({}).to_list(None)
        out = []
        for key in keys:
            await _refresh_day(db, key, now)
            model_budgets, reset_times, model_ready = {}, [], []
            for state in states:
                model = state.get("model")
                if state.get("scope") != _scope(key) or model not in FREE_LIMITS:
                    continue
                budgets = {}
                future = [float(t) for t in state.get("blocks", {}).values() if _number(t) > now]
                for resource in FREE_LIMITS[model]:
                    field, window = _RESOURCES[resource]
                    events = [e for e in state.get("events", []) if e["at"] > now - window]
                    used = sum(_number(e.get(field)) for e in events)
                    limit = _cap(model, resource, state)
                    remaining = max(0, limit - used)
                    snapshot = state.get("provider", {}).get(resource, {})
                    if snapshot.get("reset_at", 0) > now:
                        remaining = min(remaining, snapshot.get("remaining", remaining))
                        if remaining < 1:
                            future.append(snapshot["reset_at"])
                    expiry = min((e["at"] + window for e in events if _number(e.get(field)) > 0), default=None)
                    if remaining < 1 and expiry:
                        future.append(expiry)
                    budgets[resource] = {"limit": limit, "used": used, "remaining": remaining, "next_reset_at": expiry}
                next_reset = max(future) if future else None
                model_budgets[model] = {"budgets": budgets, "next_reset_at": next_reset}
                model_ready.append(next_reset is None)
                if next_reset:
                    reset_times.append(next_reset)
            pause = _key_pause(key, now)
            confirmed = key.get("free_tier_confirmed") is True
            manual_pause = bool(key.get("paused") or key.get("enabled") is False)
            if key.get("disabled") or key.get("billing_blocked"):
                status = "disabled"
            elif not confirmed:
                status = "unconfirmed"
            elif manual_pause or pause > now or (model_ready and not any(model_ready)):
                status = "cooldown"
            else:
                status = "active"
            next_reset = max(pause, min(reset_times, default=now)) if not manual_pause else None
            out.append({
                "id": key["id"], "mask": _mask(str(key.get("key", ""))), "status": status,
                "free_tier_confirmed": confirmed,
                "scope_id": key.get("scope_id") or key.get("scope") or key.get("organization_id") or "",
                "used_seconds": round(_number(key.get("used_seconds")), 1),
                "used_requests": int(_number(key.get("used_requests"))),
                "used_tokens": int(_number(key.get("used_tokens"))), "day": key["day"],
                "paused": manual_pause, "billing_blocked": bool(key.get("billing_blocked")),
                "next_reset_at": next_reset if next_reset and next_reset > now else None,
                "models": model_budgets,
            })
        return out
