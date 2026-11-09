"""Groq key validity + groq_detect_ads / groq_pick_moments smoke test (no YouTube needed)."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
import pipeline  # noqa: E402
from filedb import Database  # noqa: E402

db = Database(Path(os.environ.get("DATA_DIR") or "/app/backend/data"))

SEGMENTS = [
    {"start": 0, "end": 8, "text": "Привет друзья, подписывайтесь на канал и ставьте лайк!"},
    {"start": 8, "end": 30, "text": "Сегодня говорим про то, как работает мозг во время сна."},
    {"start": 30, "end": 50, "text": "Этот выпуск спонсирует магазин Кофебокс, промокод MOZG даёт 20% скидки."},
    {"start": 50, "end": 90, "text": "Итак, фаза глубокого сна отвечает за консолидацию памяти."},
    {"start": 90, "end": 120, "text": "Спасибо за просмотр, до встречи в следующем видео."},
]


async def main():
    try:
        ads = await pipeline.groq_detect_ads(db, SEGMENTS, 120)
        print("groq_detect_ads OK:", ads)
    except Exception as e:
        print("groq_detect_ads FAIL:", type(e).__name__, str(e)[:300])
    try:
        moments = await pipeline.groq_pick_moments(db, SEGMENTS, 120, [30], "Как работает мозг во сне",
                                                   banned=[(30.0, 50.0)], highlights=[60.0])
        print("groq_pick_moments OK:", moments)
    except Exception as e:
        print("groq_pick_moments FAIL:", type(e).__name__, str(e)[:300])
    print("keys after:", [(k["id"], k["used_requests"], k.get("cooldown_until")) for k in await db.groq_keys.find({}).to_list(10)])


asyncio.run(main())
