"""Render with real FFmpeg and generated local assets."""
import asyncio
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import face_layout
import pipeline


class WebcamSelection(unittest.TestCase):
    def test_recurring_camera_beats_larger_center_face(self):
        center = (0.5, 0.5, 0.35, 0.5, 0.99)
        corner = (0.83, 0.78, 0.08, 0.16, 0.9)
        frames = [[center, corner]] * 6 + [[center]] * 3
        self.assertEqual(face_layout._dominant_face(frames), corner[:4])
        self.assertIsNone(face_layout._dominant_face([[corner]] + [[]] * 8))

    def test_gameplay_stays_centered_unless_it_contains_camera(self):
        aspect = 1080 / (1920 - face_layout.FACE_HEIGHT)
        centered = face_layout._screen_crop(1280, 720, (0, 480, 300, 720), aspect)
        self.assertEqual(centered.x + centered.width / 2, 640)
        self.assertEqual(centered.height, 720)
        shifted = face_layout._screen_crop(1280, 720, (800, 240, 1060, 520), aspect)
        self.assertLessEqual(shifted.x + shifted.width, 800)
        self.assertEqual(shifted.height, 720)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class RetentionRender(unittest.TestCase):
    def test_compact_webcam_above_centered_gameplay_is_not_repeated(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as name:
            root = Path(name)
            image, source, output = (root / item for item in ("screen.png", "raw.mp4", "rendered.mp4"))
            screen = Image.new("RGB", (320, 180), "red")
            draw = ImageDraw.Draw(screen)
            draw.rectangle((0, 40, 30, 140), fill="blue")
            draw.rectangle((145, 40, 175, 140), fill="cyan")
            draw.rectangle((246, 46, 303, 117), fill="lime")
            screen.save(image)
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(image),
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000",
                "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
            ], check=True, capture_output=True)
            portrait = face_layout._portrait((0.86, 0.45, 0.055, 0.12), 320, 180, 0)
            asyncio.run(pipeline.render_clip(source, root / "none.ass", output, 1, portrait=portrait))
            video, audio = asyncio.run(pipeline._probe(output))["streams"]
            self.assertEqual((video["width"], video["height"], audio["codec_type"]), (1080, 1920, "audio"))
            frame = root / "frame.png"
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1", str(frame)],
                           check=True, capture_output=True)
            with Image.open(frame) as actual:
                face = actual.getpixel((540, 310))
                screen_left = actual.getpixel((20, 1500))
                screen_center = actual.getpixel((540, 1500))
                screen_right = actual.getpixel((1050, 950))
            self.assertGreater(face[1], 220)
            self.assertGreater(screen_left[0], 220)
            self.assertGreater(screen_center[1], 220)
            self.assertGreater(screen_center[2], 220)
            self.assertGreater(screen_right[0], 220)

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
