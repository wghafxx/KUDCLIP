"""Unit tests for KUDCLIP subtitle pipeline: _sanitize_words, build_ass non-overlap,
_snap_to_segment_start, fallback_moments, and code-level checks for download_segment
(bv*+ba/b, res:2160) and render_clip (crf 17, preset medium, audio 192k).
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/app/backend")
import pipeline  # noqa: E402


# ---------- _sanitize_words ----------
class TestSanitizeWords:
    def test_sorts_and_fixes_inversion(self):
        words = [
            {"word": "b", "start": 1.0, "end": 2.0},
            {"word": "a", "start": 0.5, "end": 0.9},
            {"word": "c", "start": 3.0, "end": 2.5},  # inverted
        ]
        out = pipeline._sanitize_words(words)
        assert [w["word"] for w in out] == ["a", "b", "c"]
        for w in out:
            assert w["end"] > w["start"]

    def test_resolves_overlap_with_previous(self):
        words = [
            {"word": "one", "start": 0.0, "end": 1.5},
            {"word": "two", "start": 1.0, "end": 2.0},  # overlaps prev end
            {"word": "three", "start": 1.8, "end": 3.0},
        ]
        out = pipeline._sanitize_words(words)
        for i in range(len(out) - 1):
            assert out[i]["end"] <= out[i + 1]["start"] + 1e-9, out

    def test_skips_empty_words(self):
        out = pipeline._sanitize_words([
            {"word": "", "start": 0.0, "end": 1.0},
            {"word": "  ", "start": 1.0, "end": 2.0},
            {"word": "hi", "start": 2.0, "end": 3.0},
        ])
        assert len(out) == 1 and out[0]["word"] == "hi"

    def test_min_duration_applied(self):
        out = pipeline._sanitize_words([
            {"word": "a", "start": 5.0, "end": 5.0},
        ])
        assert out[0]["end"] > out[0]["start"]


# ---------- build_ass non-overlap ----------
DIALOGUE_RE = re.compile(
    r"^Dialogue:\s*\d+,(\d+):(\d+):(\d+)\.(\d+),(\d+):(\d+):(\d+)\.(\d+),", re.M
)


def _ass_time_to_seconds(h, m, s, cs):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100.0


def _parse_dialogue_times(ass_text: str):
    times = []
    for m in DIALOGUE_RE.finditer(ass_text):
        st = _ass_time_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
        et = _ass_time_to_seconds(m.group(5), m.group(6), m.group(7), m.group(8))
        times.append((st, et))
    return times


class TestBuildAssNoOverlap:
    def _run(self, words):
        sanitized = pipeline._sanitize_words(words)
        ass = pipeline.build_ass(sanitized, "russo", "#06B6D4", offset=0.0)
        return ass, _parse_dialogue_times(ass)

    def test_no_overlap_with_dense_words(self):
        # Many words back-to-back, some overlapping
        raw = []
        t = 0.0
        for i in range(20):
            raw.append({"word": f"word{i}", "start": t, "end": t + 0.35})
            t += 0.28  # forces overlap
        ass, times = self._run(raw)
        assert len(times) >= 2
        # Layer 0 (GlowBg) and Layer 1 (Word) share same times. When sorted overall,
        # for events with same (start, end) we still expect end[i] <= start[i+1] for
        # different phrases. Group by unique intervals:
        unique = sorted(set(times))
        for i in range(len(unique) - 1):
            assert unique[i][1] <= unique[i + 1][0] + 1e-6, (
                f"overlap: {unique[i]} vs {unique[i + 1]}"
            )

    def test_no_overlap_with_inverted_and_overlapping_input(self):
        raw = [
            {"word": "hello", "start": 0.0, "end": 1.2},
            {"word": "world", "start": 0.8, "end": 2.0},  # overlaps
            {"word": "again", "start": 2.5, "end": 2.1},  # inverted
            {"word": "test", "start": 2.6, "end": 3.5},
            {"word": "final", "start": 3.2, "end": 4.0},  # overlaps
        ]
        ass, times = self._run(raw)
        unique = sorted(set(times))
        for i in range(len(unique) - 1):
            assert unique[i][1] <= unique[i + 1][0] + 1e-6, unique

    def test_min_dialogue_duration(self):
        raw = [{"word": "x", "start": 1.0, "end": 1.01}]
        ass, times = self._run(raw)
        assert len(times) >= 1
        for s, e in times:
            assert e - s >= 0.11

    def test_ass_has_styles_and_layers(self):
        ass, times = self._run([
            {"word": "one", "start": 0.0, "end": 0.5},
            {"word": "two", "start": 0.6, "end": 1.0},
        ])
        assert "Style: Word" in ass
        assert "Style: GlowBg" in ass
        # both layer 0 (glow) and layer 1 (word) lines present
        assert re.search(r"^Dialogue:\s*0,", ass, re.M)
        assert re.search(r"^Dialogue:\s*1,", ass, re.M)


# ---------- _snap_to_segment_start ----------
class TestSnapToSegmentStart:
    def test_snaps_when_close(self):
        segs = [{"start": 10.0, "end": 12.0}, {"start": 20.0, "end": 25.0}]
        assert pipeline._snap_to_segment_start(10.5, segs) == 10.0

    def test_no_snap_when_far(self):
        segs = [{"start": 10.0, "end": 12.0}]
        assert pipeline._snap_to_segment_start(30.0, segs) == 30.0

    def test_empty_segments(self):
        assert pipeline._snap_to_segment_start(5.0, []) == 5.0

    def test_picks_nearest(self):
        segs = [{"start": 5.0, "end": 6.0}, {"start": 10.0, "end": 11.0}, {"start": 15.0, "end": 16.0}]
        assert pipeline._snap_to_segment_start(9.8, segs) == 10.0


# ---------- fallback_moments ----------
class TestFallbackMoments:
    def test_returns_requested_count(self):
        moments = pipeline.fallback_moments(600.0, [40, 45, 50], "Test Video")
        assert len(moments) == 3
        for m in moments:
            assert m["end"] > m["start"]
            assert m["start"] >= 0
            assert m["end"] <= 600.0 + 1e-6
            assert "title" in m and "hashtags" in m

    def test_uses_highlights_first(self):
        moments = pipeline.fallback_moments(600.0, [40, 45], "V", banned=None, highlights=[100.0])
        assert len(moments) >= 1
        # first moment should be near highlight (h - 2)
        assert abs(moments[0]["start"] - 98.0) < 5.0

    def test_avoids_edges(self):
        moments = pipeline.fallback_moments(200.0, [30, 30], "V")
        for m in moments:
            assert m["start"] >= 200.0 * 0.06 - 5
            assert m["end"] <= 200.0 * 0.94 + 5


# ---------- Code-level checks: download_segment and render_clip ----------
class TestDownloadAndRenderCode:
    def test_download_segment_uses_high_quality_format(self):
        src = Path("/app/backend/pipeline.py").read_text(encoding="utf-8")
        # extract download_segment function block
        m = re.search(r"async def download_segment\([^)]*\):(.*?)\nasync def ", src, re.S)
        assert m, "download_segment not found"
        body = m.group(1)
        assert '"-f", "bv*+ba/b"' in body, "expected bv*+ba/b format"
        assert "res:2160" in body, "expected res:2160 sort spec"
        assert "--merge-output-format" in body and "mp4" in body

    def test_render_clip_uses_high_quality_encode(self):
        src = Path("/app/backend/pipeline.py").read_text(encoding="utf-8")
        m = re.search(r"async def render_clip\([^)]*\):(.*?)\nasync def ", src, re.S)
        assert m, "render_clip not found"
        body = m.group(1)
        assert '"-crf", "17"' in body
        assert '"-preset", "medium"' in body
        assert '"-b:a", "192k"' in body
