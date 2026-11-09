"""Offline discovery and persisted velocity checks, with public listings mocked."""

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filedb import Database
import trend_sources as sources


NOW = datetime(2026, 11, 9, 9, tzinfo=timezone.utc).timestamp()
YT = "https://www.youtube.com/@example/videos"
TWITCH = "https://www.twitch.tv/sasavot"


def youtube(video_id="abcdefghijk", **changes):
    return {"id": video_id, "url": f"https://youtu.be/{video_id}", "timestamp": NOW - 7200,
            "title": "A real uploaded video", "duration": 120, "view_count": 1000, **changes}


def twitch(slug="ExampleClip-A1", **changes):
    return {"id": "opaque-internal-id", "url": f"https://www.twitch.tv/sasavot/clip/{slug}",
            "timestamp": NOW - 7200, "title": "A live highlight", "duration": 40,
            "view_count": 100, **changes}


class SourceIdentityTests(unittest.TestCase):
    def test_configured_twitch_channel_uses_verified_username_and_rejects_other_paths(self):
        canonical = "https://www.twitch.tv/rostislav_999"
        self.assertEqual(sources._twitch_channel(canonical), canonical)
        self.assertEqual(sources._twitch_channel("https://m.twitch.tv/ROSTISLAV_999/clips"), canonical)
        self.assertFalse(sources._twitch_channel("https://www.twitch.tv/rostislav_999/clip/NotAChannel"))
        self.assertFalse(sources._twitch_channel("https://twitch.tv.example.org/rostislav_999"))
        self.assertFalse(sources._twitch_channel("https://www.twitch.tv/videos"))

    def test_provider_ids_preserve_youtube_ids_and_twitch_slugs(self):
        self.assertEqual(sources.source_id("https://youtu.be/abcdefghijk?t=14"), "abcdefghijk")
        self.assertEqual(sources.source_id("https://www.youtube.com/shorts/abcdefghijk"), "abcdefghijk")
        self.assertEqual(sources.source_id("youtube:abcdefghijk"), "abcdefghijk")
        canonical = "twitch:ExampleClip-A1"
        self.assertEqual(sources.source_id(twitch()), canonical)
        self.assertEqual(sources.source_id("https://clips.twitch.tv/ExampleClip-A1?tt_content=clip"), canonical)
        self.assertEqual(sources.source_id(canonical), canonical)
        self.assertFalse(sources.source_id("https://twitch.tv.example.org/sasavot/clip/ExampleClip-A1"))
        self.assertFalse(sources.source_id("https://www.twitch.tv/videos/123456"))
        self.assertFalse(sources.source_id({"provider": "twitch", "id": "abcdefghijk"}))

    def test_freshness_requires_exact_timestamp_and_excludes_boundary_and_future(self):
        self.assertTrue(sources.fresh({"timestamp": NOW - 48 * 3600 + 1}, NOW))
        self.assertFalse(sources.fresh({"timestamp": NOW - 48 * 3600}, NOW))
        self.assertFalse(sources.fresh({"published_at": NOW + 1}, NOW))
        self.assertFalse(sources.fresh({"upload_date": "20261109"}, NOW))
        self.assertFalse(sources.fresh({"published_at": "2026-11-09"}, NOW))
        self.assertFalse(sources.fresh({"published_at": "2026-11-09T08:00:00"}, NOW))
        self.assertFalse(sources.fresh({"timestamp": NOW - 1, "timestamp_approximate": True}, NOW))
        self.assertTrue(sources.fresh({"published_at": "2026-11-09T13:00:00+05:00"}, NOW))
        self.assertTrue(sources.fresh({"provider": "twitch", "created_at": "2026-11-09T08:00:00Z"}, NOW))

    def test_atom_uses_published_and_matching_channel_only(self):
        channel = "UC" + "A" * 22
        payload = f'''<feed xmlns="http://www.w3.org/2005/Atom"
                    xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/">
          <entry><yt:videoId>abcdefghijk</yt:videoId><yt:channelId>{channel}</yt:channelId>
            <published>2026-11-06T06:00:00Z</published><updated>2026-11-09T08:30:00Z</updated>
            <media:group><media:community><media:statistics views="12345"/></media:community></media:group>
          </entry>
          <entry><yt:videoId>zzzzzzzzzzz</yt:videoId><yt:channelId>{channel}</yt:channelId>
            <published>2026-11-09</published></entry>
          <entry><yt:videoId>yyyyyyyyyyy</yt:videoId><yt:channelId>another</yt:channelId>
            <published>2026-11-09T08:00:00Z</published></entry>
        </feed>'''.encode()
        found = sources._parse_feed(payload, channel)
        self.assertEqual(set(found), {"abcdefghijk"})
        self.assertEqual(found["abcdefghijk"]["views"], 12345)
        self.assertFalse(sources.fresh(found["abcdefghijk"], NOW))

    def test_keyless_youtube_listing_prefers_atom_and_enriches_missing_exact_time(self):
        channel = "UC" + "A" * 22
        first = youtube(timestamp=None, upload_date="20261109", channel_id=channel)
        second = youtube("zzzzzzzzzzz", timestamp=None, upload_date="20261109", channel_id=channel)
        extractor = MagicMock()
        extractor.__enter__.return_value = extractor
        extractor.extract_info.side_effect = [
            {"channel_id": channel, "entries": [first, second]},
            {"timestamp": NOW - 300, "channel_id": channel, "duration": 120},
        ]
        with patch.object(sources.yt_dlp, "YoutubeDL", return_value=extractor) as factory, patch.object(
            sources, "_read_feed", return_value={"abcdefghijk": {"published_at": NOW - 1000, "views": 2345}}
        ) as feed:
            result = sources._youtube_sync(YT, 18, NOW)
        feed.assert_called_once_with(channel)
        self.assertEqual(extractor.extract_info.call_count, 2)
        self.assertTrue(all(call.kwargs == {"download": False} for call in extractor.extract_info.call_args_list))
        self.assertTrue(factory.call_args.args[0]["skip_download"])
        self.assertEqual((result[0]["published_at"], result[0]["views"], result[0]["views_source"]), (NOW - 1000, 2345, "atom"))
        self.assertTrue(all(sources.fresh(item, NOW) for item in result))

    def test_keyless_twitch_listing_checks_daily_and_weekly_without_duplicate_slug(self):
        first = twitch()
        second = twitch("OtherClip")
        extractor = MagicMock()
        extractor.__enter__.return_value = extractor
        extractor.extract_info.side_effect = [{"entries": [first]}, {"entries": [first, second]}]
        with patch.object(sources.yt_dlp, "YoutubeDL", return_value=extractor):
            result = sources._twitch_sync(TWITCH, 24)
        self.assertEqual(result, [first, second])
        calls = extractor.extract_info.call_args_list
        self.assertIn("range=24hr", calls[0].args[0])
        self.assertIn("range=7d", calls[1].args[0])
        self.assertTrue(all(call.kwargs == {"download": False} for call in calls))

    def test_youtube_search_rotates_queries_bounds_details_and_sorts_verified_dates(self):
        queries = sources.YOUTUBE_TREND_QUERIES
        ids = [[str(index * 10 + offset).zfill(11) for offset in range(4)] for index in range(len(queries))]
        explicit_short = youtube("shorTExAmPL", url="https://www.youtube.com/shorts/shorTExAmPL")
        expired = ids[1][0]
        date_only = ids[2][1]
        extractor = MagicMock()
        extractor.__enter__.return_value = extractor

        def extract(target, *, download):
            self.assertFalse(download)
            page = urlsplit(target)
            params = parse_qs(page.query)
            if page.path == "/results":
                self.assertEqual(params["sp"], ["EgQIAxAB"])
                index = queries.index(params["search_query"][0])
                entries = [youtube(identity, duration=600, timestamp=None) for identity in ids[index]]
                if index == 0:
                    entries.insert(1, explicit_short)
                return {"entries": entries}
            identity = params["v"][0]
            timestamp = NOW - 10000 + int(identity) * 100
            if identity == expired:
                timestamp = NOW - 48 * 3600
            if identity == date_only:
                timestamp = None
            return youtube(identity, duration=600, timestamp=timestamp, upload_date="20261109")

        extractor.extract_info.side_effect = extract
        with patch.object(sources.yt_dlp, "YoutubeDL", return_value=extractor) as factory, patch.object(
            sources, "_read_feed", return_value={}
        ):
            rows = sources._youtube_search_sync(queries, 20, NOW)
        self.assertTrue(factory.call_args.args[0]["skip_download"])
        calls = [call.args[0] for call in extractor.extract_info.call_args_list]
        self.assertEqual([parse_qs(urlsplit(target).query)["search_query"][0] for target in calls[:len(queries)]], list(queries))
        details = [parse_qs(urlsplit(target).query)["v"][0] for target in calls[len(queries):]]
        self.assertEqual(details[:len(queries)], [group[0] for group in ids])
        self.assertEqual(len(details), 6)
        self.assertNotIn(explicit_short["id"], details)
        self.assertIn(date_only, details)
        self.assertEqual([row["id"] for row in rows], sorted(set(details) - {expired, date_only}, reverse=True))

    def test_reported_short_rejects_an_apparently_landscape_result(self):
        self.assertFalse(sources._confirmed_non_short(youtube(width=1920, height=1080, media_type="short")))


class ScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.work = Path(os.environ.get("KUDCLIP_TEST_WORKDIR", str(Path.cwd() / "work"))).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="trend-test-", dir=self.work)
        self.root = Path(self.temp.name).resolve()
        self.db = Database(self.root / "data")
        self.profile = {"id": "p", "channels": ["https://youtube.com/@example"],
                        "twitch_channels": ["https://twitch.tv/SASAVOT/"], "settings": {"twitch_trending": True}}
        self.list_youtube = AsyncMock(return_value=[youtube()])
        self.list_twitch = AsyncMock(return_value=[twitch()])
        self.list_youtube_search = AsyncMock(return_value=[])
        for item in (patch.object(sources, "_OBSERVATION_LOCK", asyncio.Lock()),
                     patch.object(sources, "_youtube_sync", side_effect=AssertionError("Live discovery forbidden")),
                     patch.object(sources, "_twitch_sync", side_effect=AssertionError("Live discovery forbidden")),
                     patch.object(sources, "_youtube_search_sync", side_effect=AssertionError("Live search forbidden"))):
            item.start()
            self.addCleanup(item.stop)

    async def asyncTearDown(self):
        if self.root.parent != self.work or not self.root.name.startswith("trend-test-"):
            raise AssertionError("Temporary directory outside the test workspace")
        self.temp.cleanup()

    async def scan(self, now=NOW, profiles=None):
        return await sources.scan_sources(self.db, profiles if profiles is not None else [self.profile], now,
                                          list_youtube=self.list_youtube, list_twitch=self.list_twitch,
                                          list_youtube_search=self.list_youtube_search)

    async def test_youtube_search_is_opt_in_and_unauthorized_authors_only_suggestions(self):
        self.profile["settings"] = {"youtube_trending": False}
        authorized = youtube("zzzzzzzzzzz", uploader_url="https://www.youtube.com/@Example", channel="Known creator",
                             width=1920, height=1080)
        suggestion = youtube("yyyyyyyyyyy", duration=220, channel_url="https://youtube.com/@other",
                             uploader="Other creator")
        vertical = youtube("xxxxxxxxxxx", width=1080, height=1920)
        unknown_aspect = youtube("wwwwwwwwwww")
        short_url = youtube("vvvvvvvvvvv", duration=500, webpage_url="https://www.youtube.com/shorts/vvvvvvvvvvv")
        expired = youtube("uuuuuuuuuuu", duration=600, timestamp=NOW - 48 * 3600)
        self.list_youtube_search.return_value = [authorized, suggestion, vertical, unknown_aspect, short_url, expired]
        self.assertEqual([row["video"]["video_id"] for row in await self.scan()], ["abcdefghijk"])
        self.list_youtube_search.assert_not_awaited()

        self.profile["settings"]["youtube_trending"] = True
        rows = {row["video"]["video_id"]: row["video"] for row in await self.scan()}
        self.list_youtube_search.assert_awaited_once_with(sources.YOUTUBE_TREND_QUERIES, 6)
        self.assertEqual(set(rows), {"abcdefghijk", "zzzzzzzzzzz", "yyyyyyyyyyy"})
        self.assertFalse(rows["zzzzzzzzzzz"]["discovery_only"])
        self.assertTrue(rows["yyyyyyyyyyy"]["discovery_only"])
        self.assertEqual(rows["yyyyyyyyyyy"]["channel_title"], "Other creator")

        self.list_youtube_search.return_value = [{**authorized, "view_count": 1400}, {**suggestion, "view_count": 1600}]
        rows = {row["video"]["video_id"]: row["video"] for row in await self.scan(NOW + 3600)}
        self.assertTrue(rows["zzzzzzzzzzz"]["trend_ready"])
        discovered = rows["yyyyyyyyyyy"]
        self.assertFalse(discovered["trend_ready"])
        self.assertEqual(discovered["trend_status"], "discovery_only")
        self.assertEqual(discovered["views_per_hour"], 600)
        self.assertGreater(discovered["trend_score"], 0)

    async def test_youtube_search_failure_keeps_configured_channel_and_search_alone_is_discovery(self):
        self.profile["settings"] = {"youtube_trending": True}
        self.list_youtube_search.side_effect = RuntimeError("Public search failed")
        self.assertEqual([row["video"]["video_id"] for row in await self.scan()], ["abcdefghijk"])
        self.profile["channels"] = []
        self.list_youtube_search.side_effect = None
        self.list_youtube_search.return_value = [youtube("zzzzzzzzzzz", duration=600)]
        rows = await self.scan()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["video"]["discovery_only"])

    async def test_twitch_is_opt_in_and_unconfigured_seeds_are_never_fetched(self):
        self.profile["settings"] = {"twitch_trending": False}
        self.assertEqual([r["video"]["provider"] for r in await self.scan()], ["youtube"])
        self.list_twitch.assert_not_awaited()
        self.profile["settings"]["twitch_trending"] = True
        self.profile["twitch_channels"] = []
        self.assertTrue(await self.scan())
        self.list_twitch.assert_not_awaited()
        self.assertIn("rostikfacekid", sources.TWITCH_SEEDS)

    async def test_twitch_rejects_foreign_channel_and_uses_verified_vod_offset_only(self):
        self.list_twitch.return_value = [
            twitch("FromZero", video_id="123456", vod_offset=0),
            twitch("NoOffset", video_id="123456", vod_offset=None),
            twitch("Wrong", url="https://www.twitch.tv/another/clip/Wrong", broadcaster_login="sasavot"),
            twitch("BadHost", url="https://clips.twitch.tv.evil.example/BadHost"),
        ]
        rows = {r["video"]["video_id"]: r["video"] for r in await self.scan()}
        self.assertEqual(set(rows), {"abcdefghijk", "twitch:FromZero", "twitch:NoOffset"})
        self.assertEqual(rows["twitch:FromZero"]["url"], "https://clips.twitch.tv/FromZero")
        self.assertEqual(rows["twitch:FromZero"]["twitch_vod"], {
            "id": "123456", "url": "https://www.twitch.tv/videos/123456", "start": 0, "end": 40})
        self.assertNotIn("twitch_vod", rows["twitch:NoOffset"])
        self.list_twitch.assert_awaited_once_with(TWITCH, 24)

    async def test_first_observation_and_repeat_under_five_minutes_do_not_claim_growth(self):
        self.profile["settings"] = {}
        a = (await self.scan())[0]["video"]
        self.assertFalse(a["trend_ready"])
        self.assertIsNone(a["views_per_hour"])
        self.list_youtube.return_value = [youtube(view_count=1010)]
        b = (await self.scan(NOW + 240))[0]["video"]
        self.assertIsNone(b["trend_score"])

        # A new backend DB object reuses the first persisted observation.
        self.db = Database(self.root / "data")
        self.list_youtube.return_value = [youtube(view_count=1020)]
        c = (await self.scan(NOW + 300))[0]["video"]
        self.assertTrue(c["trend_ready"])
        self.assertEqual(c["views_per_hour"], 240)
        self.assertEqual(c["trend_observed_at"], NOW + 300)

    async def test_velocity_floors_and_normalization_across_platforms(self):
        self.list_youtube.return_value = [youtube(), youtube("zzzzzzzzzzz", view_count=100000)]
        await self.scan()
        self.list_youtube.return_value = [youtube(view_count=1400), youtube("zzzzzzzzzzz", view_count=100000)]
        self.list_twitch.return_value = [twitch(view_count=160)]
        rows = [r["video"] for r in await self.scan(NOW + 3600)]
        ready = {row["provider"]: row for row in rows if row["trend_ready"]}
        self.assertEqual(ready["youtube"]["views_per_hour"], 400)
        self.assertEqual(ready["twitch"]["views_per_hour"], 60)
        self.assertEqual(ready["youtube"]["trend_score"], ready["twitch"]["trend_score"])
        self.assertEqual(rows[-1]["video_id"], "zzzzzzzzzzz")
        self.assertEqual(rows[-1]["trend_status"], "below_threshold")

    async def test_counter_correction_or_metric_switch_requires_two_new_observations(self):
        self.profile["settings"] = {}
        await self.scan()
        self.list_youtube.return_value = [youtube(view_count=1200, views_source="atom")]
        row = (await self.scan(NOW + 900))[0]["video"]
        self.assertIsNone(row["views_per_hour"])
        self.list_youtube.return_value = [youtube(view_count=1300, views_source="atom")]
        self.assertTrue((await self.scan(NOW + 1800))[0]["video"]["trend_ready"])
        self.list_youtube.return_value = [youtube(view_count=1000, views_source="atom")]
        self.assertIsNone((await self.scan(NOW + 2700))[0]["video"]["views_per_hour"])

    async def test_shared_channels_and_video_aliases_do_not_manufacture_second_sample(self):
        self.profile["channels"].append(YT)
        self.profile["settings"] = {}
        self.list_youtube.return_value = [youtube(), youtube(url="https://www.youtube.com/shorts/abcdefghijk")]
        rows = await self.scan(profiles=[self.profile, {**self.profile, "id": "p2"}])
        self.assertEqual({r["profile_id"] for r in rows}, {"p", "p2"})
        self.assertEqual(len(rows), 2)
        self.list_youtube.assert_awaited_once()
        state = await self.db.trend_observations.find_one({"id": "view_counts"})
        self.assertEqual(len(state["sources"]["abcdefghijk"]), 1)

    async def test_long_video_requires_real_anchors_and_at_most_three_bounded_windows(self):
        self.profile["settings"] = {}
        self.list_youtube.return_value = [
            youtube(duration=7200, chapters=[{"start_time": 0, "end_time": 800, "title": "Реклама"},
                  {"start_time": 1000, "end_time": 1900, "title": "Early match"},
                  {"start_time": 3500, "end_time": 4300, "title": "Late match"},
                  {"start_time": 6200, "end_time": 7200, "title": "End"}],
                heatmap=[{"start_time": 5100, "end_time": 5102, "value": .95}]),
            youtube("zzzzzzzzzzz", duration=7200),
            youtube("yyyyyyyyyyy", timestamp=None, upload_date="20261109"),
            youtube("xxxxxxxxxxx", live_status="is_live"),
        ]
        rows = await self.scan()
        self.assertEqual([r["video"]["video_id"] for r in rows], ["abcdefghijk"])
        windows = rows[0]["video"]["windows"]
        self.assertEqual(len(windows), 3)
        self.assertTrue(all(0 <= w["start"] < w["end"] <= 7200 and w["end"] - w["start"] <= 180 for w in windows))
        self.assertTrue(any(w["kind"] == "highlight" and w["start"] <= 5101 <= w["end"] for w in windows))
        self.assertTrue(any(w["kind"] == "chapter" and w["start"] == 6200 for w in windows))

    async def test_one_provider_failure_keeps_other_results_and_total_failure_signals_retry(self):
        self.list_twitch.side_effect = RuntimeError("Offline platform failed")
        self.assertEqual([r["video"]["provider"] for r in await self.scan()], ["youtube"])
        self.list_youtube.side_effect = RuntimeError("Offline platform failed")
        with self.assertRaises(sources.SourceScanUnavailable):
            await self.scan()


if __name__ == "__main__":
    unittest.main()
