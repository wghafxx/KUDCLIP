"""Integration regressions. Fake APIs and temporary data; never user's database."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
WORK = Path(r'C:\Users\lolar\Documents\Codex\2026-11-06\c-users-lolar-desktop-v2-0\work') / 'pipeline-tests'
WORK.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BACKEND))
with patch.dict(os.environ, {'MEDIA_DIR': str(WORK / 'import-media')}):
    import pipeline
import groq_pool
from filedb import Database


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.root = Path(self.temp.name)
        self.raw, self.clips = self.root / 'raw', self.root / 'clips'
        self.raw.mkdir()
        self.clips.mkdir()
        self.db = Database(self.root / 'data')
        self.words = [{'word': f'w{i}', 'start': i, 'end': i + .5} for i in range(120)]
        self.segments = [{'start': i, 'end': i + 10, 'text': f'A finished sentence {i}.'} for i in range(0, 120, 10)]
        self.profile = {'id': 'p', 'settings': {'visual_analysis': False, 'clip_len_min': 10, 'clip_len_max': 60}}
        await self.db.profiles.insert_one(self.profile)
        self.context_line = 'Прятки: игроки стали предметами'
        self.moment = {'start': 10., 'end': 35., 'title': 'A complete story', 'quality_score': 90,
                       'hashtags': ['#shorts'], 'selection_reason': 'A meaningful outcome', 'context_line': self.context_line}
        async def create_file(*args, **kwargs):
            destination = Path(args[-1])
            destination.write_bytes(b'media')
        async def command(cmd, **kwargs):
            if str(cmd[-1]).endswith('.mp3'):
                Path(cmd[-1]).write_bytes(b'audio')
            return ''
        async def render(raw, ass, output, *args):
            output.write_bytes(b'rendered')
        self.transcribe = AsyncMock(return_value=(self.words, self.segments, 'text'))
        self.select = AsyncMock(return_value=[self.moment])
        self.render = AsyncMock(side_effect=render)
        self.send = AsyncMock()
        self.context = AsyncMock(return_value=self.context_line)
        self.copy = {'banner_text': 'A complete story', 'caption': 'A clear outcome.\n\n#shorts #story'}
        self.publication = AsyncMock(return_value=self.copy)

        async def package(source, *, title, banner_text, caption, profile_name, out_dir, clip_id, **kwargs):
            # This fixture tests the pipeline/package contract; real media,
            # encoding, images and archive validation live in test_ready_pack.
            names = {'video_filename': f'{clip_id}_ready.mp4', 'cover_filename': f'{clip_id}_cover.jpg',
                     'caption_filename': f'{clip_id}_caption.txt',
                     'package_filename': f'{clip_id}_publication.zip', 'readme_filename': f'{clip_id}_README.txt'}
            self.assertTrue(Path(source).is_file())
            (out_dir / names['video_filename']).write_bytes(b'packaged-render')
            (out_dir / names['cover_filename']).write_bytes(b'offline-cover')
            (out_dir / names['caption_filename']).write_bytes(caption.encode('utf-8'))
            (out_dir / names['readme_filename']).write_text('Offline publication package', encoding='utf-8')
            with zipfile.ZipFile(out_dir / names['package_filename'], 'w') as archive:
                for field, member in [('video_filename', 'video.mp4'), ('cover_filename', 'cover.jpg'),
                                      ('caption_filename', 'caption.txt'),
                                      ('readme_filename', 'README.txt')]:
                    archive.write(out_dir / names[field], member)
            return {**names, 'ready_for_publish': True, 'publication_caption': caption, 'banner_text': banner_text}

        self.package = AsyncMock(side_effect=package)
        self.patches = [
            patch.object(pipeline, 'RAW_DIR', self.raw), patch.object(pipeline, 'CLIPS_DIR', self.clips),
            patch.object(pipeline, 'download_full_audio', side_effect=create_file),
            patch.object(pipeline, 'download_segment', side_effect=create_file),
            patch.object(pipeline, '_run', side_effect=command),
            patch.object(pipeline, 'groq_transcribe', self.transcribe),
            patch.object(pipeline, '_all_ads', AsyncMock(return_value=[])),
            patch.object(pipeline, 'sponsorblock_segments', AsyncMock(return_value=([], []))),
            patch.object(pipeline.visual_analysis, 'sound_peaks', AsyncMock(return_value=[])),
            patch.object(pipeline.editorial, 'select_moments', self.select),
            patch.object(pipeline, 'render_clip', self.render),
            patch.object(pipeline, 'send_video_to_telegram', self.send),
            patch.object(pipeline, 'publication_metadata', self.publication),
            patch.object(pipeline, 'source_context', self.context),
            patch.object(pipeline.ready_pack, 'build_package', self.package),
            patch.object(pipeline.httpx, 'AsyncClient', side_effect=AssertionError('Live HTTP is forbidden in pipeline regressions')),
            patch.object(groq_pool, 'get_key_status', AsyncMock(return_value=[{'free_tier_confirmed': True, 'status': 'active'}])),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        if not self.root.resolve().is_relative_to(WORK.resolve()):
            raise AssertionError('Temporary cleanup escaped pipeline test workspace')
        self.temp.cleanup()

    async def job(self, job_id='j', profile_id='p', **fields):
        doc = {'id': job_id, 'profile_id': profile_id, 'video_url': 'https://youtu.be/abcdefghijk',
               'video_title': 'Source', 'video_duration': 120, 'params': {'num_clips': 3, 'auto_send': True},
               'clip_ids': [], 'status': 'queued', **fields}
        await self.db.jobs.insert_one(doc)
        return doc

    async def test_default_finishes_publication_package_with_semantic_end(self):
        await self.job()
        await pipeline.process_job(self.db, 'j')
        clip = await self.db.clips.find_one({'id': 'j_0'})
        self.assertEqual((clip['start'], clip['end']), (10, 35))
        self.assertFalse(clip['needs_review'])
        self.assertTrue(clip['ready_for_publish'])
        self.assertEqual(clip['filename'], 'j_0_ready.mp4')
        self.assertEqual(clip['caption'], self.copy['caption'])
        self.assertNotIn('banner_filename', clip)
        self.assertIsNone(self.package.await_args.kwargs['banner_text'])
        self.publication.assert_awaited_once()
        self.package.assert_awaited_once()
        self.assertEqual(self.select.await_args.kwargs['context_line'], '')
        self.context.assert_not_awaited()
        self.assertTrue((self.clips / clip['filename']).is_file())
        self.assertFalse((self.clips / 'j_0.mp4').exists())
        with zipfile.ZipFile(self.clips / clip['package_filename']) as archive:
            self.assertEqual(archive.read('caption.txt').decode('utf-8'), clip['caption'])
        self.send.assert_not_awaited()
        self.assertEqual((await self.db.jobs.find_one({'id': 'j'}))['status'], 'done')

    async def test_trend_windows_use_local_analysis_and_preserve_absolute_time(self):
        source = {'provider': 'youtube', 'windows': [{'start': 400, 'end': 520}, {'start': 1000, 'end': 1120}]}
        await self.job(video_duration=4800, source_video=source, autopilot_attempt_id='daily:1')
        other_words = [{**word, 'word': 'other_' + word['word']} for word in self.words]
        self.transcribe.side_effect = [(self.words, self.segments, ''), (other_words, self.segments, '')]
        with patch.object(pipeline, '_probe', AsyncMock(return_value={'streams': [{'codec_type': 'audio'}]})):
            await pipeline.process_job(self.db, 'j')
        clips = await self.db.clips.find({'job_id': 'j'}).sort('start').to_list(None)
        self.assertEqual([(clip['start'], clip['end']) for clip in clips], [(410, 435), (1010, 1035)])
        self.assertEqual([call.kwargs['duration'] for call in self.select.await_args_list], [120, 120])
        self.assertTrue(all(call.kwargs['trend_mode'] for call in self.select.await_args_list))
        calls = pipeline.download_segment.await_args_list
        self.assertEqual([call.args[1:3] for call in calls], [(400, 520), (1000, 1120), (10, 35), (10, 35)])
        self.assertTrue(all(isinstance(call.args[0], Path) for call in calls[-2:]))

    def test_trend_stories_skip_repeated_speech_across_sources(self):
        words = [{'word': token, 'start': index} for index, token in enumerate(
            'Я зашёл в старый дом увидел чемодан и там лежала полная коробка жуков'.split())]
        clip = {'start': 0, 'end': len(words), 'quality_score': 90}
        previous = {'subtitle_text': ' '.join(word['word'] for word in words[1:])}
        self.assertEqual(pipeline._dedup_stories([clip], words, [previous]), [])
        self.assertEqual(pipeline._dedup_stories([clip], words, [{'subtitle_text': 'Сегодня я ехал в школу'}]), [clip])

    async def test_explicit_review_still_keeps_complete_package_without_sending(self):
        self.profile['settings']['review_before_send'] = True
        self.profile['telegram_chat_ids'] = ['offline-recipient']
        await self.db.profiles.update_one({'id': 'p'}, {'$set': self.profile})
        await self.job()
        await pipeline.process_job(self.db, 'j')
        clip = await self.db.clips.find_one({'id': 'j_0'})
        self.assertTrue(clip['needs_review'])
        self.assertTrue(clip['ready_for_publish'])
        self.send.assert_not_awaited()
        self.assertEqual((await self.db.jobs.find_one({'id': 'j'}))['status'], 'review')

    async def test_failed_package_never_registers_ready_clip_or_processed_source(self):
        self.package.side_effect = RuntimeError('offline package validation failure')
        await self.job()
        with self.assertRaisesRegex(RuntimeError, 'validation failure'):
            await pipeline.process_job(self.db, 'j')
        self.assertEqual(await self.db.clips.find().to_list(None), [])
        self.assertEqual(await self.db.processed.find().to_list(None), [])
        self.send.assert_not_awaited()

    async def test_legacy_long_duration_settings_fit_the_65_second_publication_limit(self):
        await self.db.profiles.update_one({'id': 'p'}, {'$set': {
            'settings': {**self.profile['settings'], 'clip_len_min': 90, 'clip_len_max': 120}}})
        await self.job()
        await pipeline.process_job(self.db, 'j')
        arguments = self.select.await_args.kwargs
        self.assertEqual(arguments['max_duration'], 65)
        self.assertEqual(arguments['min_duration'], 65)
        self.assertEqual(arguments['context_line'], '')

    async def test_no_candidate_produces_zero_clips_not_evenly_spaced_fallback(self):
        self.select.return_value = []
        await self.job()
        await pipeline.process_job(self.db, 'j')
        self.assertEqual(await self.db.clips.find().to_list(None), [])
        self.render.assert_not_awaited()
        self.publication.assert_not_awaited()
        self.package.assert_not_awaited()
        self.assertEqual((await self.db.jobs.find_one({'id': 'j'}))['status'], 'done')

    async def test_quota_failure_keeps_transcript_for_resume(self):
        self.select.side_effect = groq_pool.GroqDeferred(9999999999, 'waiting')
        await self.job()
        with self.assertRaises(groq_pool.GroqDeferred):
            await pipeline.process_job(self.db, 'j')
        self.assertIsNone(await self.db.processed.find_one({'profile_id': 'p'}))
        self.select.side_effect = None
        self.select.return_value = [self.moment]
        await pipeline.process_job(self.db, 'j')
        self.assertEqual(self.transcribe.await_count, 1)
        self.assertTrue((await self.db.processed.find_one({'profile_id': 'p'}))['completed'])

    async def test_transcript_shared_across_profiles_and_resume_skips_rendered_clip(self):
        await self.job()
        await pipeline.process_job(self.db, 'j')
        await pipeline.process_job(self.db, 'j')
        self.assertEqual(self.render.await_count, 1)
        self.assertEqual(self.publication.await_count, 1)
        self.assertEqual(self.package.await_count, 1)
        await self.db.profiles.insert_one({**self.profile, 'id': 'p2'})
        await self.job('j2', 'p2')
        await pipeline.process_job(self.db, 'j2')
        self.assertEqual(self.transcribe.await_count, 1)
        self.assertEqual(self.render.await_count, 2)

    async def test_manual_subtitle_override_preserves_original_when_not_supplied(self):
        original = pipeline._edited_words(self.words, 10, 20, None)
        self.assertEqual(len(original), 10)
        edited = pipeline._edited_words(self.words, 10, 20, 'one two')
        self.assertEqual([w['word'] for w in edited], ['one', 'two'])
        self.assertTrue(all(10 <= w['start'] < w['end'] <= 20 for w in edited))

    def test_banned_legacy_fallback_cannot_return_rejected_ads(self):
        self.assertEqual(pipeline.fallback_moments(600, [40, 40], 'X', [(0, 600)]), [])

    def test_ass_timestamp_rolls_minutes_and_hours(self):
        self.assertEqual(pipeline._ass_time(59.999), '0:01:00.00')
        self.assertEqual(pipeline._ass_time(3599.999), '1:00:00.00')

    def test_no_pause_never_interrupts_speech_with_ad(self):
        self.assertIsNone(pipeline.pick_ad_point(self.words, 0, 30))
        self.assertIsNone(pipeline.pick_ad_point([], 0, 30))

    def test_same_video_url_variants_share_transcript(self):
        self.assertEqual(pipeline._analysis_id('https://youtu.be/abcdefghijk'),
                         pipeline._analysis_id('https://www.youtube.com/watch?v=abcdefghijk&t=100'))


class FFmpegTests(unittest.IsolatedAsyncioTestCase):
    async def test_silent_source_and_silent_ad_insert_with_real_ffmpeg(self):
        with tempfile.TemporaryDirectory(dir=WORK) as scratch:
            folder = Path(scratch)
            clip, ad, output = folder / 'clip.mp4', folder / 'ad.mp4', folder / 'out.mp4'
            for path, duration in [(clip, 4), (ad, 1)]:
                proc = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    f'color=c=red:size=160x288:rate=30:duration={duration}', '-c:v', 'libx264',
                    '-preset', 'ultrafast', str(path)], capture_output=True, timeout=20)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors='replace'))
            await pipeline.insert_ad(clip, ad, output, at=2)
            metadata = await pipeline._probe(output)
            self.assertAlmostEqual(float(metadata['format']['duration']), 5, delta=.3)
            self.assertTrue(any(s['codec_type'] == 'audio' for s in metadata['streams']))


if __name__ == '__main__':
    unittest.main()
