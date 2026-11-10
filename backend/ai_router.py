"""Free-only AI routing: the Groq free pool, with local Ollama as a backup when Groq limits are used up."""

import json
import re

import httpx

DEFAULTS = {"llm": "auto", "asr": "auto", "ollama_model": "qwen3:8b", "ollama_url": "http://127.0.0.1:11434",
            "whisper_model": "large-v3-turbo"}


async def settings(db):
    raw = ((await db.app_settings.find_one({"id": "main"})) or {}).get("ai") or {}
    return {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}


async def provider(db):
    config = await settings(db)
    return ("groq" if config["llm"] == "auto" else config["llm"]), config


def _json(text):
    text = re.sub(r"<think>.*?</think>", "", str(text), flags=re.S)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise RuntimeError("ИИ вернул ответ не в формате JSON; повторите задачу")
        value = json.loads(match[0])
    if not isinstance(value, dict):
        raise RuntimeError("ИИ вернул неверный формат анализа; повторите задачу")
    return value


def _split(messages):
    system, texts, images = [], [], []
    for message in messages:
        content = message.get("content")
        parts = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        for part in parts:
            if part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                images.append(url.split(",", 1)[1] if url.startswith("data:") else url)
            elif message.get("role") == "system":
                system.append(part.get("text") or "")
            else:
                texts.append(part.get("text") or "")
    return "\n".join(system), "\n\n".join(texts), images


async def ollama_ready(config):
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            return (await client.get(config["ollama_url"].rstrip("/") + "/api/tags")).status_code == 200
    except httpx.HTTPError:
        return False


async def _ollama(config, messages, temperature, max_tokens):
    system, text, images = _split(messages)
    body = {"model": config["ollama_model"], "stream": False, "format": "json", "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": 12288},
            "messages": [{"role": "system", "content": system or "Отвечай только JSON."},
                         {"role": "user", "content": text, **({"images": images} if images else {})}]}
    async with httpx.AsyncClient(timeout=900) as client:
        response = await client.post(config["ollama_url"].rstrip("/") + "/api/chat", json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"Ollama: {response.text[:200]}. Запусти Ollama и скачай модель {config['ollama_model']}")
    return _json(response.json()["message"]["content"])


async def chat_json(db, messages, temperature=0.3, max_tokens=1500, groq_fallback=None):
    from groq_pool import GroqUnavailable

    name, config = await provider(db)
    if name == "ollama":
        return await _ollama(config, messages, temperature, max_tokens)
    try:
        return await groq_fallback()
    except GroqUnavailable:
        # Free Groq limits are exhausted: keep working on this PC instead of waiting for the reset.
        if config["llm"] == "auto" and await ollama_ready(config):
            return await _ollama(config, messages, temperature, max_tokens)
        raise
