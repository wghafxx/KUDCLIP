"""Regression coverage for the per-author source pickers."""

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hype_scout
import yt_dlp


NOW = 1794255600
CHANNEL = "https://www.youtube.com/@kiwkisquad"
VIDEO_ID = "RNuOZjHoFhM"
VIDEO = {"id": VIDEO_ID, "title": "Игра", "duration": 4602, "timestamp": NOW - 9000,
         "live_status": "not_live", "view_count": 5000}


class AuthorYoutubePickerTests(unittest.TestCase):
    def pick(self, *videos, exclude=()):
        by_id = {video["id"]: video for video in videos}

        def extract(url, *, download):
            if url.endswith("/videos"):
                return {"entries": [{"id": identity} for identity in by_id]}
            if url.endswith("/streams"):
                raise yt_dlp.utils.DownloadError("This channel does not have a streams tab")
            return by_id[url.rsplit("=", 1)[1]]

        extractor = MagicMock()
        extractor.__enter__.return_value.extract_info.side_effect = extract
        with patch.object(yt_dlp, "YoutubeDL", return_value=extractor):
            return hype_scout._youtube_pick_sync(CHANNEL, NOW, 36, exclude)

    def test_recent_long_upload_without_markers_is_eligible_for_audio_scan(self):
        selected = self.pick(VIDEO)
        self.assertEqual(selected["video_id"], VIDEO_ID)
        self.assertEqual(selected["published_at"], VIDEO["timestamp"])
        self.assertEqual(selected["windows"], [])
        self.assertIsNone(self.pick(VIDEO, exclude={VIDEO_ID}))

    def test_real_chapters_are_retained_as_cues(self):
        selected = self.pick({**VIDEO, "chapters": [{"start_time": 900, "end_time": 1200, "title": "Раунд"}]})
        self.assertEqual(selected["windows"], [{"start": 900, "end": 1080, "kind": "chapter", "title": "Раунд"}])

    def test_age_and_feed_require_exact_publication_time(self):
        self.assertIsNone(self.pick({**VIDEO, "timestamp": NOW - 36 * 3600}))
        self.assertIsNone(self.pick({**VIDEO, "timestamp": NOW + 1}))
        missing_time = {**VIDEO, "timestamp": None, "upload_date": "20261109", "channel_id": "UC" + "A" * 22}
        with patch.object(hype_scout, "_read_feed", return_value={VIDEO_ID: {"published_at": NOW - 3600}}):
            self.assertEqual(self.pick(missing_time)["published_at"], NOW - 3600)
        with patch.object(hype_scout, "_read_feed", return_value={}):
            self.assertIsNone(self.pick(missing_time))

    def test_newest_eligible_upload_wins_with_three_hour_limit(self):
        newest = {**VIDEO, "id": "newest00001", "timestamp": NOW - 400, "duration": 10800, "view_count": 10}
        too_long = {**newest, "id": "longer00001", "timestamp": NOW - 300, "duration": 10801}
        processing = {**newest, "id": "stream00001", "timestamp": NOW - 200, "live_status": "post_live"}
        selected = self.pick(too_long, VIDEO, newest, processing)
        self.assertEqual(selected["video_id"], newest["id"])

    def test_unavailable_youtube_is_not_reported_as_no_new_uploads(self):
        extractor = MagicMock()
        extractor.__enter__.return_value.extract_info.side_effect = yt_dlp.utils.DownloadError("network failure")
        with patch.object(yt_dlp, "YoutubeDL", return_value=extractor):
            with self.assertRaises(hype_scout.SourceScanUnavailable):
                hype_scout._youtube_pick_sync(CHANNEL, NOW, 36)


class AuthorTwitchPickerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def vod(identity, created, length, views=0):
        return {"id": identity, "title": identity, "lengthSeconds": length,
                "createdAt": datetime.fromtimestamp(created, timezone.utc).isoformat(), "viewCount": views}

    async def test_finished_archive_without_markers_beats_older_high_view_count(self):
        snapshot = {"live": True, "live_since": NOW - 1200, "name": "Sasavot", "clips": [], "vods": [
            self.vod("playing", NOW - 1200, 1170),
            self.vod("too-long", NOW - 27000, 25201),
            self.vod("newest", NOW - 28800, 25200, 3),
            self.vod("popular", NOW - 20000, 7200, 100000),
        ]}
        with patch.object(hype_scout, "streamer_snapshot", new=AsyncMock(return_value=snapshot)), \
                patch.object(hype_scout, "_signals", new=AsyncMock(return_value=[])) as signals:
            selected = await hype_scout.twitch_pick(None, "sasavot", NOW)
        self.assertEqual(selected["video_id"], "twitchvod:newest")
        self.assertEqual(selected["windows"], [])
        self.assertEqual(signals.await_args.kwargs["chat_step"], 210)

    async def test_excluded_archive_does_not_fall_back_to_viewer_clip(self):
        snapshot = {"live": False, "name": "Sasavot", "vods": [self.vod("reviewed", NOW - 7200, 3600)],
                    "clips": [{"slug": "viewer-clip"}]}
        with patch.object(hype_scout, "streamer_snapshot", new=AsyncMock(return_value=snapshot)):
            self.assertIsNone(await hype_scout.twitch_pick(None, "sasavot", NOW, exclude={"twitchvod:reviewed"}))
