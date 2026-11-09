"""Render with real FFmpeg and generated local assets."""
import asyncio
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class RetentionRender(unittest.TestCase):
    def test_banner_and_muted_split_keep_voice_and_duration(self):
        def make(path, color, frequency):
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:s=320x180:r=30",
                 "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
                 "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                check=True, capture_output=True,
            )

        async def check(root):
            raw, loop, output = (root / name for name in ("raw.mp4", "loop.mp4", "render.mp4"))
            ass, artwork = root / "text.ass", root / "ad.png"
            make(raw, "red", 223)
            make(loop, "blue", 2000)
            Image.new("RGBA", (260, 50), (0, 255, 0, 255)).save(artwork)
            words = [{"word": "Вот", "start": 0.5, "end": 0.9}, {"word": "развязка", "start": 0.9, "end": 1.3}]
            ass.write_text(pipeline.build_ass(words, "montserrat_italic", "#06B6D4", split=True), encoding="utf-8")
            await pipeline.render_clip(raw, ass, output, 1, banner=artwork, loop=loop, hook=True, duration=3)
            probe = await pipeline._probe(output)
            self.assertAlmostEqual(float(probe["format"]["duration"]), 3, delta=0.12)
            video, audio = probe["streams"]
            self.assertEqual((video["width"], video["height"], audio["codec_type"]), (1080, 1920, "audio"))
            result = subprocess.check_output([
                "ffmpeg", "-v", "error", "-ss", "1.8", "-i", str(output),
                "-frames:v", "1", "-vf", "crop=2:2:540:210", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
            ])
            self.assertGreater(result[1], 220)
            self.assertLess(result[0], 30)
            levels = subprocess.check_output([
                "ffmpeg", "-v", "error", "-ss", "1", "-i", str(output), "-t", "0.5",
                "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"
            ])
            # A 223Hz source crosses zero about 223 times per half second.
            pcm = [int.from_bytes(levels[i:i+2], "little", signed=True) for i in range(0, len(levels), 2)]
            zero_crossings = sum((x >= 0) != (y >= 0) for x, y in zip(pcm, pcm[1:]))
            self.assertTrue(185 < zero_crossings < 270, zero_crossings)

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as name:
            asyncio.run(check(Path(name)))
