"""Direct tests of pipeline functions: sponsorblock_segments, insert_ad, pick_ad_point."""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, "/app/backend")
import pipeline  # noqa: E402


# ---------- SponsorBlock ----------
class TestSponsorBlock:
    def test_known_video_highlight(self):
        banned, highlights = asyncio.run(pipeline.sponsorblock_segments("2Z4m4lnjxkY"))
        assert isinstance(banned, list)
        assert isinstance(highlights, list)
        assert len(highlights) >= 1, f"expected highlights, got {highlights}"
        assert abs(highlights[0] - 25.059) < 1.0, highlights

    def test_unknown_video(self):
        banned, highlights = asyncio.run(pipeline.sponsorblock_segments("zzzzzzzzzzz"))
        assert banned == []
        assert highlights == []


# ---------- pick_ad_point ----------
class TestPickAdPoint:
    def test_picks_gap_near_middle(self):
        words = [
            {"word": "a", "start": 0.0, "end": 5.0},
            {"word": "b", "start": 14.0, "end": 20.0},  # gap 5..14 -> center ~9.6
            {"word": "c", "start": 20.5, "end": 30.0},
        ]
        t = pipeline.pick_ad_point(words, 0.0, 30.0)
        assert 9.0 <= t <= 14.0, t

    def test_no_words_defaults_to_middle(self):
        t = pipeline.pick_ad_point([], 0.0, 30.0)
        assert abs(t - 15.0) < 0.01

    def test_clamped_in_range(self):
        t = pipeline.pick_ad_point(None, 0.0, 10.0)
        assert 2.0 <= t <= 8.0


# ---------- insert_ad ----------
def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


class TestInsertAd:
    def test_ad_inserted_extends_duration(self):
        tmp = Path(tempfile.mkdtemp())
        clip = tmp / "clip.mp4"
        ad = tmp / "ad.mp4"
        out = tmp / "out.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=16",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=16",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(clip),
        ], capture_output=True, check=True)
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:size=720x720:rate=30:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", str(ad),
        ], capture_output=True, check=True)

        asyncio.run(pipeline.insert_ad(clip, ad, out, at=8.0))
        assert out.exists()
        dur = _ffprobe_duration(out)
        assert abs(dur - 20.0) < 1.0, f"expected ~20s, got {dur}"
