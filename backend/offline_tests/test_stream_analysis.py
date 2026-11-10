"""Long source review uses bounded windows throughout the recording."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream_analysis import AUTHOR_DURATION_LIMITS, review_windows


class StreamReviewTests(unittest.TestCase):
    def test_twitch_preserves_strong_platform_cues_and_covers_end(self):
        duration = AUTHOR_DURATION_LIMITS['twitch']
        source = {'provider': 'twitch', 'windows': [
            {'start': 40, 'end': 100, 'heat': 95},
            {'start': 800, 'end': 860, 'heat': 25},
            {'start': duration - 120, 'end': duration - 40, 'heat': 87},
        ]}
        windows = review_windows(source, duration, [])
        self.assertEqual(len(windows), 14)
        self.assertEqual(windows[0], (0, 180))
        self.assertEqual(windows[-1], (duration - 180, duration))
        self.assertTrue(all(a[1] <= b[0] for a, b in zip(windows, windows[1:])))

    def test_youtube_with_no_cues_uses_recent_dialogue_at_every_block(self):
        duration = AUTHOR_DURATION_LIMITS['youtube']
        final = duration - 270
        source = {'provider': 'youtube', 'windows': [{'start': 0, 'end': 100, 'kind': 'sample'}]}
        windows = review_windows(source, duration, [{'start': final, 'end': final + 10, 'text': 'Проверка речи'}])
        self.assertEqual(len(windows), 10)
        self.assertGreater(windows[0][0], 100)
        self.assertTrue(windows[-1][0] <= final < windows[-1][1])
        self.assertTrue(all(0 <= start < end <= duration and end - start == 180 for start, end in windows))
