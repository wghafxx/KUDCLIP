import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/app/backend")
import httpx
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
from filedb import Database  # noqa: E402

db = Database(Path("/app/backend/data"))


async def main():
    keys = await db.groq_keys.find({}).to_list(10)
    k = keys[0]["key"]
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {k}"})
        print("models status:", r.status_code)
        if r.status_code == 200:
            ids = sorted(m["id"] for m in r.json().get("data", []))
            print(json.dumps(ids, indent=0))
        else:
            print(r.text[:500])


asyncio.run(main())
