import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
import pipeline  # noqa: E402
from filedb import Database  # noqa: E402

db = Database(Path("/app/backend/data"))
AUDIO = Path("/tmp/qa_tone.mp3")


async def main():
    # whisper via groq
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
                    "-ac", "1", "-b:a", "32k", str(AUDIO)], capture_output=True, check=True)
    try:
        words, segments, text = await pipeline.groq_transcribe(db, AUDIO, 3.0)
        print("groq_transcribe OK. words:", len(words), "segments:", len(segments), "text:", repr(text[:100]))
    except Exception as e:
        print("groq_transcribe FAIL:", type(e).__name__, str(e)[:300])

    # candidate chat models
    for model in ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]:
        pipeline.GROQ_CHAT_MODEL = model
        try:
            ads = await pipeline.groq_detect_ads(db, [
                {"start": 0, "end": 8, "text": "Привет, подписывайтесь на канал!"},
                {"start": 8, "end": 40, "text": "Этот выпуск спонсирует Кофебокс, промокод MOZG."},
                {"start": 40, "end": 90, "text": "Глубокий сон отвечает за память."},
            ], 90)
            print(f"chat model {model} OK ->", ads)
        except Exception as e:
            print(f"chat model {model} FAIL:", str(e)[:200])


asyncio.run(main())
