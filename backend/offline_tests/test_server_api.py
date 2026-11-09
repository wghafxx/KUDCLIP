"""ASGI API tests using isolated data and no running worker/network."""
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

BACKEND = Path(__file__).resolve().parents[1]
WORK = Path(r'C:\Users\lolar\Documents\Codex\2026-11-06\c-users-lolar-desktop-v2-0\work') / 'api-tests'
WORK.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BACKEND))


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'DATA_DIR': str(root / 'data'), 'MEDIA_DIR': str(root / 'media'), 'TELEGRAM_TOKEN': ''})
        self.env.start()
        spec = importlib.util.spec_from_file_location('_isolated_kudclip_server', BACKEND / 'server.py')
        self.server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.server
        with patch('dotenv.load_dotenv', return_value=False):
            spec.loader.exec_module(self.server)
        import pipeline
        self.media = root / 'media'
        self.media.mkdir(exist_ok=True)
        self.patches = [patch.object(pipeline, 'CLIPS_DIR', self.media), patch.object(pipeline, 'ADS_DIR', self.media)]
        for item in self.patches: item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url='http://isolated')
        self.profile = (await self.client.post('/api/profiles', json={'name': 'Test', 'channels': ['https://youtube.com/@demo']})).json()
        self.pid = self.profile['id']

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.server.manager.stop()
        for item in reversed(self.patches): item.stop()
        self.env.stop()
        if not Path(self.temp.name).resolve().is_relative_to(WORK.resolve()):
            raise AssertionError('Temporary cleanup escaped API test workspace')
        self.temp.cleanup()

    async def test_partial_profile_patch_preserves_other_fields_and_settings(self):
        result = await self.client.patch(f'/api/profiles/{self.pid}', json={'settings': {'genre': 'game'}})
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data['channels'], ['https://youtube.com/@demo'])
        self.assertFalse(data['settings']['review_before_send'])
        self.assertFalse(data['settings']['banner_enabled'])
        self.assertEqual(data['settings']['font'], 'montserrat_italic')
        self.assertEqual(data['settings']['genre'], 'game')
        self.assertEqual((await self.client.patch(f'/api/profiles/{self.pid}', json={'settings': {'clip_len_min': 100, 'clip_len_max': 30}})).status_code, 422)

    async def test_browser_posting_requires_local_bound_account_and_explicit_enable(self):
        path = '/api/browser-posting'
        self.assertEqual((await self.client.get(path)).status_code, 403)
        transport = httpx.ASGITransport(app=self.server.app, client=('127.0.0.1', 10000))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8001') as local:
            self.assertEqual((await local.get(path)).json()['enabled_since'], {})
            self.assertEqual((await local.put(path + '/youtube', json={'enabled': True})).status_code, 409)
            fake_account = AsyncMock(return_value='UC' + 'a' * 22)
            with patch.object(self.server.browser_posting.browser, 'account', fake_account):
                bound = await local.post(path + '/youtube/bind')
            self.assertEqual(bound.json()['accounts']['youtube'], 'UC' + 'a' * 22)
            self.assertEqual(bound.json()['enabled_since'], {})
            self.assertEqual((await local.put(path + '/youtube', json={'enabled': True},
                                               headers={'origin': 'https://unrelated.test'})).status_code, 403)
            enabled = await local.put(path + '/youtube', json={'enabled': True})
            self.assertEqual(enabled.status_code, 200)
            self.assertIn('youtube', enabled.json()['enabled_since'])

    async def test_banner_upload_preview_and_selection(self):
        artwork = io.BytesIO()
        Image.new('RGB', (90, 30), 'green').save(artwork, format='JPEG')
        uploaded = await self.client.post('/api/ads', data={'kind': 'banner', 'name': 'Test banner'},
                                          files={'file': ('test.jpg', artwork.getvalue(), 'image/jpeg')})
        self.assertEqual(uploaded.status_code, 200)
        banner = uploaded.json()
        self.assertEqual((banner['kind'], banner['name']), ('banner', 'Test banner'))
        preview = await self.client.get(f"/api/ads/{banner['id']}/file")
        self.assertEqual(preview.headers['content-type'], 'image/png')
        self.assertEqual(Image.open(io.BytesIO(preview.content)).size, (90, 30))
        profile = await self.client.patch(f'/api/profiles/{self.pid}', json={
            'settings': {'ad_id': banner['id'], 'ad_enabled': True, 'ad_disclosure': 'sponsored'},
        })
        self.assertEqual(profile.json()['settings']['ad_disclosure'], 'sponsored')
        rejected = await self.client.post('/api/ads', data={'kind': 'banner'},
                                          files={'file': ('fake.png', b'not an image', 'image/png')})
        self.assertEqual(rejected.status_code, 422)

    async def test_free_account_confirmation_and_safe_key_metadata(self):
        raw = 'gsk_OFFLINE_NOT_A_REAL_KEY_0123456789'
        created = (await self.client.post('/api/groq/keys', json={'key': raw})).json()
        self.assertNotIn('key', created)
        items = (await self.client.get('/api/groq/keys')).json()
        self.assertEqual(items[0]['status'], 'unconfirmed')
        self.assertNotIn(raw, str(items))
        confirmed = await self.client.put(f"/api/groq/keys/{created['id']}", json={'free_tier_confirmed': True})
        self.assertEqual(confirmed.json()['status'], 'active')

    async def test_job_enqueue_deduplicates_and_does_not_mark_processed(self):
        payload = {'profile_id': self.pid, 'video_url': 'https://youtu.be/abcdefghijk', 'video_duration': 120}
        first = (await self.client.post('/api/jobs', json=payload)).json()
        second = (await self.client.post('/api/jobs', json=payload)).json()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(await self.server.db.processed.find().to_list(None), [])
        self.assertFalse(first['settings_snapshot']['review_before_send'])
        self.assertFalse(first['settings_snapshot']['banner_enabled'])
        self.assertIsNone(self.server.manager.worker)
        self.assertIsNone(self.server.autopilot.worker)

    async def test_ready_publication_assets_are_served_without_workers_or_network(self):
        contents = {'ready.mp4': b'offline-video', 'cover.jpg': b'offline-cover', 'banner.png': b'offline-banner',
                    'publication.zip': b'offline-package'}
        for name, value in contents.items():
            (self.media / name).write_bytes(value)
        await self.server.db.clips.insert_one({'id': 'ready', 'profile_id': self.pid, 'filename': 'ready.mp4',
            'cover_filename': 'cover.jpg', 'banner_filename': 'banner.png', 'package_filename': 'publication.zip',
            'ready_for_publish': True, 'caption': 'Ready caption'})
        for suffix, name, content_type in [('file', 'ready.mp4', 'video/mp4'), ('cover', 'cover.jpg', 'image/jpeg'),
                                          ('banner', 'banner.png', 'image/png'), ('package', 'publication.zip', 'application/zip')]:
            result = await self.client.get(f'/api/clips/ready/{suffix}')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.content, contents[name])
            self.assertTrue(result.headers['content-type'].startswith(content_type))
        (self.media / 'cover.jpg').unlink()
        self.assertEqual((await self.client.get('/api/clips/ready/cover')).status_code, 404)
        self.assertIsNone(self.server.manager.worker)
        self.assertIsNone(self.server.autopilot.worker)

    async def test_cancel_retry_and_delete_job(self):
        job = (await self.client.post('/api/jobs', json={'profile_id': self.pid, 'video_url': 'https://youtu.be/abcdefghijk'})).json()
        self.assertEqual((await self.client.post(f"/api/jobs/{job['id']}/cancel")).json()['status'], 'cancelled')
        self.assertEqual((await self.client.post(f"/api/jobs/{job['id']}/retry")).json()['status'], 'queued')
        self.assertEqual((await self.client.delete(f"/api/jobs/{job['id']}")).status_code, 200)
        self.assertIsNone(await self.server.db.jobs.find_one({'id': job['id']}))

    async def test_clip_edit_keeps_original_and_feedback_is_saved(self):
        clip = {'id': 'original', 'job_id': 'old', 'profile_id': self.pid, 'video_url': 'https://youtu.be/abcdefghijk',
                'source_duration': 120, 'title': 'Original', 'start': 10, 'end': 40, 'filename': 'original.mp4'}
        await self.server.db.clips.insert_one(clip)
        result = await self.client.post('/api/clips/original/edit', json={'start': 15, 'end': 45, 'subtitle_text': 'New text', 'crop_x': .8})
        self.assertEqual(result.status_code, 200, result.text)
        edit = result.json()['params']['clip_edit']
        self.assertEqual(edit['original_clip_id'], 'original')
        self.assertEqual(edit['crop_x'], .8)
        self.assertEqual((await self.server.db.clips.find_one({'id': 'original'}))['start'], 10)
        feedback = await self.client.post('/api/clips/original/feedback', json={'rating': 'bad', 'reason': 'No ending'})
        self.assertEqual(feedback.json()['feedback']['reason'], 'No ending')
        self.assertEqual((await self.client.post('/api/clips/original/edit', json={'start': 115, 'end': 140})).status_code, 422)

    async def test_legacy_failed_video_remains_eligible_for_autostart(self):
        await self.server.db.jobs.insert_one({'id': 'failed', 'profile_id': self.pid, 'video_url': 'https://youtu.be/abcdefghijk', 'status': 'error'})
        await self.server.db.processed.insert_one({'profile_id': self.pid, 'video_id': 'abcdefghijk'})
        video = {'video_id': 'abcdefghijk', 'url': 'https://youtu.be/abcdefghijk', 'title': 'Retry me', 'thumbnail': '', 'duration': 120, 'views': 10}
        with patch.object(self.server.pipeline, 'list_channel_videos', AsyncMock(return_value=[video])):
            result = await self.client.post(f'/api/autostart/{self.pid}')
        self.assertEqual(result.json()['created'], 1)

    async def test_legacy_count_normalized_without_breaking_patch(self):
        await self.server.db.profiles.update_one({'id': self.pid}, {'$set': {'settings': {'auto_videos_count': 5}}})
        result = await self.client.patch(f'/api/profiles/{self.pid}', json={'settings': {'genre': 'story'}})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['settings']['auto_videos_count'], 3)

    async def test_free_only_settings_and_request_validation(self):
        settings = (await self.client.get('/api/settings')).json()
        self.assertTrue(settings['free_only'])
        self.assertFalse(settings['paid_fallbacks'])
        self.assertEqual((await self.client.post('/api/jobs', json={'profile_id': self.pid, 'video_url': 'x', 'num_clips': 0})).status_code, 422)


if __name__ == '__main__':
    unittest.main()
