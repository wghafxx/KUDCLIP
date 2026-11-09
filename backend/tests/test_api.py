"""KUDCLIP backend API tests: root, groq keys, app settings, profiles, channel videos, source archive."""
import io
import zipfile

import pytest
import requests

from conftest import BASE_URL

API = f"{BASE_URL}/api"
TEST_GROQ_KEY = "gsk_TESTKEY_kudclip_qa_0123456789abcdef"


# ---------- root ----------
class TestRoot:
    def test_root(self, api_client):
        r = api_client.get(f"{API}/")
        assert r.status_code == 200
        assert "KUDCLIP" in r.json()["message"]


# ---------- Groq keys pool ----------
class TestGroqKeys:
    def test_list_has_active_key(self, api_client):
        r = api_client.get(f"{API}/groq/keys")
        assert r.status_code == 200
        keys = r.json()
        assert isinstance(keys, list) and len(keys) >= 1
        k = keys[0]
        for f in ("id", "mask", "status", "used_seconds", "used_requests", "day"):
            assert f in k
        assert "_id" not in k
        assert k["status"] == "active", f"expected active, got {k['status']}"
        assert "…" in k["mask"] and len(k["mask"]) < 20, "key must be masked"

    def test_add_duplicate_and_persistence(self, api_client):
        created_id = None
        try:
            r = api_client.post(f"{API}/groq/keys", json={"key": TEST_GROQ_KEY})
            assert r.status_code == 200, r.text[:300]
            doc = r.json()
            created_id = doc["id"]
            assert "key" not in doc, "raw key must not be returned"
            assert doc["disabled"] is False

            # duplicate rejected
            dup = api_client.post(f"{API}/groq/keys", json={"key": TEST_GROQ_KEY})
            assert dup.status_code == 400, dup.text[:300]

            # persisted in file db
            listed = api_client.get(f"{API}/groq/keys").json()
            assert created_id in [x["id"] for x in listed]
            with open("/app/backend/data/groq_keys.json", encoding="utf-8") as f:
                assert TEST_GROQ_KEY in f.read(), "key not persisted to groq_keys.json"
        finally:
            if created_id:
                d = api_client.delete(f"{API}/groq/keys/{created_id}")
                assert d.status_code == 200
                assert created_id not in [x["id"] for x in api_client.get(f"{API}/groq/keys").json()]

    def test_short_key_validation(self, api_client):
        r = api_client.post(f"{API}/groq/keys", json={"key": "short"})
        assert r.status_code == 422


# ---------- App settings (Telegram token) ----------
class TestAppSettings:
    def test_set_and_clear_telegram_token(self, api_client):
        try:
            r = api_client.put(f"{API}/settings", json={"telegram_token": "123:test"})
            assert r.status_code == 200, r.text[:300]
            body = r.json()
            assert body["has_telegram_token"] is True
            assert "telegram_token_mask" in body
            assert "123:test" not in body["telegram_token_mask"] or body["telegram_token_mask"] == "***"

            g = api_client.get(f"{API}/settings")
            assert g.status_code == 200
            assert g.json()["has_telegram_token"] is True

            with open("/app/backend/data/app_settings.json", encoding="utf-8") as f:
                assert "123:test" in f.read(), "token not persisted"
        finally:
            c = api_client.put(f"{API}/settings", json={"telegram_token": ""})
            assert c.status_code == 200
            assert c.json()["has_telegram_token"] is False


# ---------- Profiles CRUD ----------
class TestProfiles:
    def test_profile_crud(self, api_client):
        payload = {
            "name": "TEST_qa_profile",
            "telegram_chat_ids": ["111", "222"],
            "channels": ["https://www.youtube.com/@TED"],
            "settings": {"clips_per_video": 2, "clip_duration": 25, "ad_enabled": True},
        }
        r = api_client.post(f"{API}/profiles", json=payload)
        assert r.status_code == 200, r.text[:300]
        p = r.json()
        pid = p["id"]
        try:
            assert p["name"] == "TEST_qa_profile"
            assert p["telegram_chat_ids"] == ["111", "222"]
            assert p["settings"]["clips_per_video"] == 2
            assert p["settings"]["ad_enabled"] is True
            assert p["settings"]["font"] == "russo"  # default applied

            g = api_client.get(f"{API}/profiles/{pid}")
            assert g.status_code == 200
            assert g.json()["name"] == "TEST_qa_profile"
            assert "_id" not in g.json()

            listed = api_client.get(f"{API}/profiles").json()
            assert pid in [x["id"] for x in listed]
            assert all("_id" not in x for x in listed)

            upd = dict(payload)
            upd["name"] = "TEST_qa_profile_upd"
            upd["telegram_chat_ids"] = ["333"]
            upd["settings"] = {"clips_per_video": 5, "glow_color": "#FF0000"}
            u = api_client.put(f"{API}/profiles/{pid}", json=upd)
            assert u.status_code == 200, u.text[:300]
            assert u.json()["name"] == "TEST_qa_profile_upd"

            g2 = api_client.get(f"{API}/profiles/{pid}").json()
            assert g2["telegram_chat_ids"] == ["333"]
            assert g2["settings"]["clips_per_video"] == 5
            assert g2["settings"]["glow_color"] == "#FF0000"
        finally:
            d = api_client.delete(f"{API}/profiles/{pid}")
            assert d.status_code == 200
        assert api_client.get(f"{API}/profiles/{pid}").status_code == 404

    def test_get_missing_profile_404(self, api_client):
        assert api_client.get(f"{API}/profiles/nope1234").status_code == 404

    def test_update_missing_profile_404(self, api_client):
        r = api_client.put(f"{API}/profiles/nope1234", json={"name": "x"})
        assert r.status_code == 404

    def test_job_missing_profile_404(self, api_client):
        r = api_client.post(f"{API}/jobs", json={"profile_id": "nope1234", "video_url": "https://youtu.be/2Z4m4lnjxkY"})
        assert r.status_code == 404


# ---------- Ads / clips listing ----------
class TestListings:
    @pytest.mark.parametrize("path", ["/ads", "/clips", "/jobs"])
    def test_list_endpoints(self, api_client, path):
        r = api_client.get(f"{API}{path}")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_missing_ad_file_404(self, api_client):
        assert api_client.get(f"{API}/ads/nope1234/file").status_code == 404

    def test_missing_clip_file_404(self, api_client):
        assert api_client.get(f"{API}/clips/nope1234/file").status_code == 404


# ---------- Channel videos (yt-dlp) ----------
class TestChannelVideos:
    def test_channel_videos(self, api_client):
        r = api_client.get(f"{API}/channel/videos", params={"url": "https://www.youtube.com/@TED", "limit": 5}, timeout=180)
        assert r.status_code == 200, r.text[:400]
        vids = r.json()["videos"]
        assert len(vids) >= 1
        v = vids[0]
        for f in ("video_id", "title", "url", "views"):
            assert f in v

    def test_bad_channel_url(self, api_client):
        r = api_client.get(f"{API}/channel/videos", params={"url": "https://www.youtube.com/@____qa_no_such_channel_zzz", "limit": 3}, timeout=180)
        assert r.status_code in (200, 502), r.text[:300]
        if r.status_code == 200:
            assert r.json()["videos"] == []


# ---------- Source archive ----------
class TestSourceArchive:
    def test_archive_contains_windows_launcher(self):
        r = requests.get(f"{API}/source/archive", timeout=300)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        assert "ЗАПУСК_WINDOWS.bat" in names, names[:20]
        assert "ЗАПУСК_WINDOWS.md" in names
        assert any(n.endswith("backend/server.py") for n in names)
        assert not any(n.endswith(".env") for n in names), "archive must not contain .env"
