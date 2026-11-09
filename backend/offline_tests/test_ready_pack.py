"""Local package integration tests with synthetic FFmpeg media, no API calls."""

import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ready_pack


WORK = Path(r"C:\Users\lolar\Documents\Codex\2026-11-06\c-users-lolar-desktop-v2-0\work\ready_pack_tests")
FONTS = Path(__file__).resolve().parents[1] / "fonts"


async def real_run(command, timeout=120):
    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    if proc.returncode:
        raise RuntimeError(stderr.decode(errors="replace")[-1500:])
    return stdout.decode(errors="replace")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe unavailable")
class ReadyPackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="package_", dir=WORK)
        self.folder = Path(self.temporary.name)
        self.source = self.folder / "clean.mp4"
        self.calls = []

    def tearDown(self):
        if not self.folder.resolve().is_relative_to(WORK.resolve()):
            raise AssertionError("Scratch cleanup escaped the task workspace")
        self.temporary.cleanup()

    async def source_video(self, audio=True):
        command = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=270x480:rate=10:duration=0.8"]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.8"]
        command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac", "-shortest"]
        await real_run(command + [str(self.source)])

    async def recorded_run(self, command, **kwargs):
        self.calls.append(command)
        return await real_run(command, **kwargs)

    async def package(self, **kwargs):
        settings = dict(title="Неожиданный исход сложного боя", banner_text="Неожиданный исход сложного боя",
                        caption="Неожиданный исход сложного боя\n\n#игры #shorts", profile_name="ИГРОВОЙ ДЕНЬ",
                        out_dir=self.folder, clip_id="test_0", run=self.recorded_run, fonts_dir=FONTS)
        settings.update(kwargs)
        return await ready_pack.build_package(self.source, **settings)

    async def test_complete_package_has_valid_video_transparency_caption_and_zip(self):
        await self.source_video()
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = await self.package()
        self.assertTrue(result["ready_for_publish"])
        metadata = await ready_pack._probe(self.folder / result["video_filename"], real_run)
        self.assertEqual((metadata["width"], metadata["height"]), (1080, 1920))
        self.assertTrue(metadata["has_audio"])
        self.assertLessEqual((self.folder / result["video_filename"]).stat().st_size, 45 * 1024 * 1024)
        with Image.open(self.folder / result["banner_filename"]) as image:
            self.assertEqual((image.size, image.mode), ((1080, 1920), "RGBA"))
            self.assertEqual(image.getpixel((540, 1000))[3], 0)
            self.assertEqual(image.getpixel((540, 1600))[3], 0)
            self.assertGreater(image.getpixel((70, 150))[3], 200)
        with zipfile.ZipFile(self.folder / result["package_filename"]) as archive:
            self.assertEqual(set(archive.namelist()), {"video.mp4", "cover.jpg", "banner.png", "caption.txt", "README.txt"})
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("caption.txt").decode("utf-8").strip(), result["publication_caption"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)

    async def test_rerun_is_idempotent_and_does_not_reencode_or_double_overlay(self):
        await self.source_video(audio=False)
        first = await self.package()
        video = self.folder / first["video_filename"]
        before = (video.stat().st_mtime_ns, hashlib.sha256(video.read_bytes()).hexdigest())
        self.calls.clear()
        second = await self.package()
        self.assertEqual(first, second)
        self.assertEqual((video.stat().st_mtime_ns, hashlib.sha256(video.read_bytes()).hexdigest()), before)
        self.assertTrue(all(Path(command[0]).stem == "ffprobe" for command in self.calls))
        # Passing a returned ready path still resolves the saved clean input.
        self.source = video
        self.calls.clear()
        self.assertEqual(await self.package(), first)
        self.assertTrue(all(Path(command[0]).stem == "ffprobe" for command in self.calls))

    async def test_plain_publication_package_keeps_audio_and_has_no_banner(self):
        await self.source_video()
        result = await self.package(banner_text=None)
        self.assertNotIn("banner_filename", result)
        self.assertTrue(result["ready_for_publish"])
        video = self.folder / result["video_filename"]
        self.assertTrue((await ready_pack._probe(video, real_run))["has_audio"])
        with zipfile.ZipFile(self.folder / result["package_filename"]) as archive:
            self.assertEqual(set(archive.namelist()), {"video.mp4", "cover.jpg", "caption.txt", "README.txt"})
            self.assertIsNone(archive.testzip())

    async def test_corrupt_archive_is_rebuilt_from_clean_video(self):
        await self.source_video(audio=False)
        result = await self.package()
        archive = self.folder / result["package_filename"]
        archive.write_bytes(b"broken")
        self.calls.clear()
        rebuilt = await self.package()
        with zipfile.ZipFile(archive) as valid:
            self.assertIsNone(valid.testzip())
        self.assertEqual(result, rebuilt)
        encode = next(command for command in self.calls if "-filter_complex" in command)
        self.assertEqual(Path(encode[encode.index("-i") + 1]), self.source)

    async def test_banner_limits_words_wraps_russian_and_keeps_center_free(self):
        await self.source_video(audio=False)
        result = await self.package(banner_text="Этот невероятно неожиданный поворот полностью изменил исход последней решающей битвы")
        self.assertLessEqual(len(result["banner_text"].split()), 7)
        manifest = json.loads((self.folder / "test_0_ready.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(manifest["layout"]["lines"]), 3)
        self.assertLess(manifest["layout"]["panel_bottom"], 445)

    async def test_overlong_clip_rejected_before_visual_render(self):
        self.source.write_bytes(b"source-placeholder")
        async def probe_only(command, **_):
            self.assertEqual(Path(command[0]).stem, "ffprobe")
            return json.dumps({"streams": [{"codec_type": "video", "width": 1080, "height": 1920}],
                               "format": {"duration": "66"}})
        with self.assertRaisesRegex(ValueError, "65"):
            await self.package(run=probe_only)
        self.assertFalse((self.folder / "test_0_ready.mp4").exists())

    async def test_missing_fonts_or_source_and_invalid_id_fail_clearly(self):
        with self.assertRaises(FileNotFoundError):
            await self.package()
        self.source.write_bytes(b"not-a-video")
        with self.assertRaisesRegex(ValueError, "идентификатор"):
            await self.package(clip_id="../escape")
        with self.assertRaisesRegex(RuntimeError, "шрифт"):
            await self.package(fonts_dir=self.folder)

    async def test_size_fallback_renders_clean_source_again(self):
        await self.source_video(audio=False)
        encodes = []
        async def inflated_first_encode(command, **kwargs):
            result = await real_run(command, **kwargs)
            if "-filter_complex" in command:
                encodes.append(command)
                if len(encodes) == 1:
                    with Path(command[-1]).open("ab") as handle:
                        handle.write(b"\x00" * 400001)
            return result
        with patch.object(ready_pack, "MAX_FILE_BYTES", 400000):
            result = await self.package(run=inflated_first_encode)
        self.assertEqual(len(encodes), 2)
        self.assertIn("-b:v", encodes[1])
        self.assertTrue(all(Path(command[command.index("-i") + 1]) == self.source for command in encodes))
        self.assertLessEqual((self.folder / result["video_filename"]).stat().st_size, 400000)

    async def test_failed_rebuild_preserves_previous_committed_package(self):
        await self.source_video(audio=False)
        result = await self.package()
        output = self.folder / result["video_filename"]
        before = hashlib.sha256(output.read_bytes()).hexdigest()
        package_before = hashlib.sha256((self.folder / result["package_filename"]).read_bytes()).hexdigest()
        async def fail_encode(command, **kwargs):
            if "-filter_complex" in command:
                raise RuntimeError("local encode interrupted")
            return await real_run(command, **kwargs)
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            await self.package(title="Другой заголовок", banner_text="Другой заголовок", run=fail_encode)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), before)
        self.assertEqual(hashlib.sha256((self.folder / result["package_filename"]).read_bytes()).hexdigest(), package_before)
        self.assertFalse(list(self.folder.glob(".test_0_pack_*")))


if __name__ == "__main__":
    unittest.main()
