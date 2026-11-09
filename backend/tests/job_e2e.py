"""Long-running full job e2e (run manually, not part of pytest suite)."""
import json
import time

import requests
from dotenv import dotenv_values

BASE = dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"

p = requests.post(f"{API}/profiles", json={
    "name": "TEST_job_e2e",
    "telegram_chat_ids": [],
    "channels": [],
    "settings": {"clips_per_video": 1, "clip_len_min": 20, "clip_len_max": 30, "ad_enabled": False},
}, timeout=60).json()
pid = p["id"]
print("profile", pid, flush=True)

job = requests.post(f"{API}/jobs", json={
    "profile_id": pid,
    "video_url": "https://www.youtube.com/watch?v=2Z4m4lnjxkY",
    "num_clips": 1,
    "clip_duration": 25,
    "auto_send": False,
}, timeout=60).json()
jid = job["id"]
print("job", jid, flush=True)

last = None
deadline = time.time() + 600
final = None
while time.time() < deadline:
    time.sleep(10)
    jobs = requests.get(f"{API}/jobs", params={"profile_id": pid}, timeout=60).json()
    j = next((x for x in jobs if x["id"] == jid), None)
    if not j:
        print("JOB DISAPPEARED", flush=True)
        break
    cur = (j["status"], j.get("status_text"), j.get("progress"))
    if cur != last:
        print(cur, flush=True)
        last = cur
    if j["status"] in ("done", "error"):
        final = j
        break

print("FINAL:", json.dumps(final, ensure_ascii=False)[:2000], flush=True)
if final and final["status"] == "done":
    clips = requests.get(f"{API}/clips", params={"profile_id": pid}, timeout=60).json()
    print("CLIPS:", json.dumps(clips, ensure_ascii=False)[:2000], flush=True)
    for c in clips:
        r = requests.get(f"{API}/clips/{c['id']}/file", timeout=120)
        print("clip file", c["id"], r.status_code, len(r.content), flush=True)
print("cleanup:", requests.delete(f"{API}/profiles/{pid}", timeout=60).status_code, flush=True)
