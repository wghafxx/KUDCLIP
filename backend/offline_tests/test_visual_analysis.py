"""Offline visual pipeline checks: synthetic media and fake vision, no APIs."""

import asyncio
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import visual_analysis
from groq_pool import estimate_tokens


WORK = Path(r"C:\Users\lolar\Documents\Codex\2026-11-06\c-users-lolar-desktop-v2-0\work\visual_analysis_tests")


async def real_run(command, timeout=120):
    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    if proc.returncode:
        raise RuntimeError(stderr.decode(errors="replace")[-1500:])
    return stdout.decode(errors="replace")


async def fake_run(command, **_):
    Path(command[-1]).write_bytes(b"offline-image-placeholder")
    return ""


def image_times(messages):
    content = messages[-1]["content"]
    text = next(item["text"] for item in content if item.get("type") == "text")
    return json.loads(re.search(r"\[[\d.,\s]+\]", text).group())


class VisualAnalysisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="visual_", dir=WORK)
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        # Only the securely created task scratch directory can be removed.
        if not self.folder.resolve().is_relative_to(WORK.resolve()):
            raise AssertionError("Scratch cleanup escaped the task workspace")
        self.temporary.cleanup()

    async def test_overview_covers_tail_and_reuses_saved_responses(self):
        stages, saved, seen = {}, [], []
        async def save(key, value):
            saved.append(key)
        async def vision(messages, **_):
            times = image_times(messages)
            seen.append(times)
            return {"observations": [{"start": times[0], "end": times[-1],
                                      "description": "Наблюдаемый эпизод", "interesting": True, "reason": "Видимое изменение"}]}
        first = await visual_analysis.overview(self.folder / "source.mp4", 181, self.folder,
                                               stages, save, vision, fake_run)
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[-1][0], 180)
        self.assertTrue(first)
        self.assertEqual(len(saved), 3)
        async def forbidden(*_, **__):
            raise AssertionError("Cached visual stage must not call a model or FFmpeg")
        second = await visual_analysis.overview(self.folder / "source.mp4", 181, self.folder,
                                                stages, save, forbidden, forbidden)
        self.assertEqual(first, second)

    async def test_overview_discards_nonfinite_reversed_and_outside_observations(self):
        async def save(*_):
            pass
        async def vision(*_, **__):
            return {"observations": [
                {"start": 0, "end": 5, "description": "Факт"},
                {"start": -1, "end": 5, "description": "За границей"},
                {"start": 5, "end": 1, "description": "Инверсия"},
                {"start": math.nan, "end": 5, "description": "NaN"},
                {"start": 0, "end": math.inf, "description": "Infinity"},
                {"start": 0, "end": 100, "description": "После конца"},
            ]}
        result = await visual_analysis.overview(self.folder / "source.mp4", 9, self.folder, {}, save, vision, fake_run)
        self.assertEqual([item["description"] for item in result], ["Факт"])

    async def test_verification_cache_includes_claim_not_just_timestamps(self):
        calls, stages = [], {}
        async def save(*_):
            pass
        async def vision(messages, **_):
            calls.append(messages)
            return {"verdict": "accept" if len(calls) == 1 else "reject", "complete_event": True,
                    "observations": [], "reason": "Проверен именно заявленный смысл",
                    "evidence_status": "contradiction",
                    "contradiction_evidence": [{"start": 0, "end": 8, "description": "Видимый исход противоположен новому утверждению"}]}
        first = {"start": 0, "end": 9, "selection_reason": "Игрок проигрывает схватку"}
        second = {"start": 0, "end": 9, "selection_reason": "Игрок выигрывает схватку"}
        await visual_analysis.verify(self.folder / "source.mp4", first, self.folder, stages, save, vision, fake_run)
        result = await visual_analysis.verify(self.folder / "source.mp4", second, self.folder, stages, save, vision, fake_run)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["verdict"], "reject")

    async def test_invalid_model_verdict_does_not_become_a_cached_accept(self):
        stages = {}
        async def save(*_):
            pass
        async def vision(*_, **__):
            return {"observations": []}
        with self.assertRaises(RuntimeError):
            await visual_analysis.verify(self.folder / "source.mp4", {"start": 0, "end": 9},
                                         self.folder, stages, save, vision, fake_run)
        self.assertEqual(stages, {})

    async def test_accept_requires_nonempty_in_range_facts_and_complete_event(self):
        invalid_facts = [[], [{"start": 0, "end": 5, "description": "Сцена вне эпизода"}],
                         [{"start": 10, "end": 15, "description": ""}],
                         [{"start": math.nan, "end": 15, "description": "Не конечный таймкод"}]]
        async def save(*_):
            pass
        for facts in invalid_facts:
            async def vision(*_, **__):
                return {"verdict": "accept", "complete_event": True, "observations": facts, "reason": "Общая оценка"}
            with self.subTest(facts=facts):
                result = await visual_analysis.verify(self.folder / "source.mp4", {"start": 10, "end": 19},
                                                       self.folder, {}, save, vision, fake_run)
                self.assertEqual(result["verdict"], "uncertain")
                self.assertFalse(result["complete_event"])
        async def valid_vision(*_, **__):
            return {"verdict": "accept", "complete_event": True,
                    "observations": [{"start": 10, "end": 18, "description": "Персонаж достигает выхода"}]}
        result = await visual_analysis.verify(self.folder / "source.mp4", {"start": 10, "end": 19},
                                               self.folder, {}, save, valid_vision, fake_run)
        self.assertEqual(result["verdict"], "accept")
        self.assertTrue(result["complete_event"])

    async def test_visual_prompt_checks_title_hook_and_payoff_not_only_generic_reason(self):
        seen = []
        async def save(*_):
            pass
        async def vision(messages, **_):
            seen.extend(item["text"] for item in messages[-1]["content"] if item.get("type") == "text")
            return {"verdict": "uncertain", "complete_event": False, "observations": []}
        moment = {"start": 0, "end": 9, "title": "Выход из окружения", "selection_reason": "Есть завершённый сюжет",
                  "hook": {"sentence_id": "S1", "quote": "Герой окружён"},
                  "payoff": {"sentence_id": "S2", "quote": "Герой достиг выхода"}}
        await visual_analysis.verify(self.folder / "source.mp4", moment, self.folder, {}, save, vision, fake_run)
        self.assertIn("Выход из окружения", seen[0])
        self.assertIn("Герой окружён", seen[0])
        self.assertIn("Герой достиг выхода", seen[0])

    async def test_dense_tail_confirms_whole_claim_and_reuses_both_stages(self):
        stages, saved, seen = {}, [], []
        async def save(key, result):
            saved.append(key)
        async def vision(messages, **kwargs):
            times = image_times(messages)
            seen.append((times, messages))
            cost = estimate_tokens({"model": "qwen/qwen3.8-27b", "messages": messages,
                                    "max_tokens": kwargs["max_tokens"], "response_format": {"type": "json_object"}})
            self.assertLessEqual(cost, 6400)
            if len(seen) == 1:
                return {"verdict": "uncertain", "complete_event": False,
                        "observations": [{"start": 50, "end": 55, "description": "Персонаж оказался в окружении"}]}
            return {"verdict": "accept", "complete_event": True, "reason": "Контекст первой сетки и выход в хвосте подтверждают эпизод",
                    "observations": [{"start": 78, "end": 79, "description": "Персонаж достигает выхода"}]}
        moment = {"start": 50, "end": 80, "genre": "game", "title": "Выход из окружения",
                  "hook": {"sentence_id": "S1", "quote": "Нужно выбраться"},
                  "payoff": {"sentence_id": "S2", "quote": "Получилось выйти"}}
        result = await visual_analysis.verify(self.folder / "source.mp4", moment, self.folder, stages, save, vision, fake_run)
        self.assertEqual(result["verdict"], "accept")
        self.assertTrue(result["complete_event"])
        self.assertEqual(result["dense_verification"]["start"], 70)
        self.assertEqual(seen[0][0][0], 50)
        self.assertEqual(seen[1][0][0], 70)
        self.assertLess(seen[1][0][-1], 80)
        tail_prompt = next(item["text"] for item in seen[1][1][-1]["content"] if item.get("type") == "text")
        self.assertIn("Персонаж оказался в окружении", tail_prompt)
        self.assertIn("Нужно выбраться", tail_prompt)
        self.assertIn("Получилось выйти", tail_prompt)
        self.assertEqual(len(saved), 2)
        self.assertTrue(any(key.startswith("visual-tail:") for key in saved))
        async def forbidden(*_, **__):
            raise AssertionError("A completed dense check must reuse both cached stages")
        cached = await visual_analysis.verify(self.folder / "source.mp4", moment, self.folder, stages, save, forbidden, forbidden)
        self.assertEqual(cached, result)

    async def test_dense_quota_failure_preserves_initial_and_retries_only_tail(self):
        stages, saved, calls = {}, [], []
        async def save(key, result):
            saved.append(key)
        async def initial_then_quota(messages, **_):
            calls.append(image_times(messages))
            if len(calls) == 1:
                return {"verdict": "uncertain", "complete_event": False,
                        "observations": [{"start": 50, "end": 55, "description": "Контекст виден, исход неясен"}]}
            raise RuntimeError("free quota exhausted")
        moment = {"start": 50, "end": 80, "genre": "game", "title": "Проверяемый исход"}
        with self.assertRaisesRegex(RuntimeError, "quota exhausted"):
            await visual_analysis.verify(self.folder / "source.mp4", moment, self.folder, stages, save, initial_then_quota, fake_run)
        self.assertEqual(len(stages), 1)
        self.assertEqual(len(saved), 1)
        retried = []
        async def only_tail(messages, **_):
            retried.append(image_times(messages))
            return {"verdict": "accept", "complete_event": True,
                    "observations": [{"start": 78, "end": 79, "description": "Подтверждённый исход"}]}
        result = await visual_analysis.verify(self.folder / "source.mp4", moment, self.folder, stages, save, only_tail, fake_run)
        self.assertEqual(result["verdict"], "accept")
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0][0], 70)
        self.assertEqual(len(stages), 2)

    async def test_dense_uncertainty_or_outside_facts_remain_final_rejection(self):
        moment = {"start": 50, "end": 80, "genre": "game"}
        tails = [{"verdict": "uncertain", "complete_event": False, "observations": []},
                 {"verdict": "accept", "complete_event": True,
                  "observations": [{"start": 0, "end": 5, "description": "Относительные/чужие таймкоды"}]}]
        async def save(*_):
            pass
        for tail in tails:
            responses = iter([{"verdict": "uncertain", "complete_event": False,
                               "observations": [{"start": 50, "end": 55, "description": "Начальная ситуация"}]}, tail])
            async def vision(*_, **__):
                return next(responses)
            with self.subTest(tail=tail):
                result = await visual_analysis.verify(self.folder / "source.mp4", moment, self.folder, {}, save, vision, fake_run)
                self.assertEqual(result["verdict"], "reject")
                self.assertFalse(result["complete_event"])

    async def test_invalid_initial_accept_triggers_dense_but_initial_reject_does_not(self):
        moment = {"start": 50, "end": 80, "genre": "game"}
        calls = []
        async def save(*_):
            pass
        async def vision(messages, **_):
            calls.append(image_times(messages))
            if len(calls) == 1:
                return {"verdict": "accept", "complete_event": True, "observations": []}
            return {"verdict": "accept", "complete_event": True,
                    "observations": [{"start": 78, "end": 79, "description": "В хвосте виден целый заявленный эпизод"}]}
        result = await visual_analysis.verify(self.folder / "source.mp4", moment, self.folder, {}, save, vision, fake_run)
        self.assertEqual(result["verdict"], "accept")
        self.assertEqual(len(calls), 2)
        rejected = []
        async def reject(messages, **_):
            rejected.append(messages)
            return {"verdict": "reject", "complete_event": False, "observations": [],
                    "evidence_status": "contradiction",
                    "contradiction_evidence": [{"start": 51, "end": 79, "description": "Наблюдаемый исход прямо противоположен заявленному"}]}
        result = await visual_analysis.verify(self.folder / "source.mp4", moment, self.folder, {}, save, reject, fake_run)
        self.assertEqual(result["verdict"], "reject")
        self.assertEqual(len(rejected), 1)

    async def test_missing_identity_goes_to_motion_and_exact_speech_can_jointly_prove_event(self):
        calls, stages = [], {}
        speech = [{"start": 50, "end": 52, "text": "Кружка убегает"},
                  {"start": 78, "end": 79, "text": "Еле догнал"}]
        moment = {"start": 50, "end": 80, "genre": "game", "title": "Погоня за кружкой",
                  "hook": {"quote": "Кружка убегает"}, "payoff": {"quote": "Еле догнал"},
                  "context_line": "Прятки в CS2: люди стали предметами"}
        async def save(*_):
            pass
        async def vision(messages, **kwargs):
            calls.append(messages)
            cost = estimate_tokens({"model": "qwen/qwen3.8-27b", "messages": messages,
                                    "max_tokens": kwargs["max_tokens"], "response_format": {"type": "json_object"}})
            self.assertLessEqual(cost, 6400)
            if len(calls) == 1:
                return {"verdict": "reject", "complete_event": False, "evidence_status": "insufficient",
                        "reason": "Маленькая кружка неразличима на редких кадрах", "observations": []}
            return {"verdict": "accept", "complete_event": True, "support_mode": "joint", "evidence_status": "support",
                    "speech_evidence": [{"quote": "Кружка убегает"}, {"quote": "Еле догнал"}],
                    "observations": [{"start": 64, "end": 66, "description": "Видимое действие совместимо с погоней"}]}
        result = await visual_analysis.verify(self.folder / "source.mp4", moment, self.folder, stages, save, vision, fake_run, transcript=speech)
        self.assertEqual(result["verdict"], "accept")
        self.assertEqual(result["support_mode"], "joint")
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(key.startswith("visual-motion:") for key in stages))
        times = image_times(calls[1])
        self.assertLessEqual(times[2] - times[0], 0.61)
        prompt = next(item["text"] for item in calls[1][-1]["content"] if item.get("type") == "text")
        self.assertIn(moment["context_line"], prompt)
        self.assertEqual(result["speech_evidence"][0]["start"], 50)

    async def test_fabricated_speech_and_speech_only_never_become_accept(self):
        calls = []
        speech = [{"start": 50, "end": 52, "text": "Кружка убегает"}, {"start": 78, "end": 79, "text": "Не могу её догнать"}]
        async def save(*_):
            pass
        async def vision(messages, **_):
            calls.append(messages)
            if len(calls) == 1:
                return {"verdict": "uncertain", "complete_event": False, "observations": []}
            return {"verdict": "accept", "complete_event": True,
                    "support_mode": "joint" if len(calls) == 2 else "speech_only",
                    "speech_evidence": [{"quote": "Еле догнал"}],
                    "observations": [{"start": 78, "end": 79, "description": "Камера перемещается"}]}
        result = await visual_analysis.verify(self.folder / "source.mp4", {"start": 50, "end": 80, "genre": "game"},
                                              self.folder, {}, save, vision, fake_run, transcript=speech)
        self.assertEqual(result["verdict"], "reject")
        self.assertFalse(result["complete_event"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["speech_evidence"], [])

    async def test_hd_offset_uses_local_seek_and_keeps_absolute_evidence_times(self):
        commands = []
        async def run(command, **kwargs):
            commands.append(command)
            return await fake_run(command, **kwargs)
        times = await visual_analysis.make_sheet(self.folder / "hd.mp4", self.folder / "hd.jpg", 100, 109,
                                                 run, video_offset=100,
                                                 zooms=[1, 1, 1, 1.8, 1.8, 1.8, 1, 1, 1])
        self.assertEqual(times[0], 100)
        seeks = [float(commands[0][index + 1]) for index, value in enumerate(commands[0]) if value == "-ss"]
        self.assertEqual(seeks[0], 0)
        self.assertLess(seeks[-1], 9)
        self.assertIn("crop=iw/1.8", commands[0][commands[0].index("-filter_complex") + 1])

    async def test_context_line_changes_visual_cache_and_is_actually_passed(self):
        stages, seen = {}, []
        async def save(*_):
            pass
        async def vision(messages, **_):
            seen.append(messages)
            return {"verdict": "uncertain", "complete_event": False, "observations": []}
        for line in ("Участники стали предметами", "Игрок управляет предметом"):
            await visual_analysis.verify(self.folder / "source.mp4", {"start": 0, "end": 9, "context_line": line},
                                         self.folder, stages, save, vision, fake_run)
        self.assertEqual(len(seen), 2)
        self.assertIn("Участники стали предметами", next(item["text"] for item in seen[0][-1]["content"] if item.get("type") == "text"))
        self.assertIn("Игрок управляет предметом", next(item["text"] for item in seen[1][-1]["content"] if item.get("type") == "text"))

    async def test_sheet_rejects_invalid_ranges_before_starting_ffmpeg(self):
        async def forbidden_run(*_, **__):
            raise AssertionError("Invalid timestamps must be rejected before FFmpeg")
        for start, end in [(-1, 9), (9, 9), (10, 9), (math.nan, 9), (0, math.inf)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                await visual_analysis.make_sheet(self.folder / "source.mp4", self.folder / "sheet.jpg",
                                                 start, end, forbidden_run)

    def test_sheet_labels_include_final_context(self):
        for start, end in [(100, 109), (1788.75476, 1818.9548)]:
            with self.subTest(start=start, end=end):
                times = visual_analysis.sheet_times(start, end)
                self.assertEqual(len(times), 9)
                self.assertEqual(times, sorted(times))
                self.assertGreaterEqual(times[0], start)
                self.assertLess(times[-1], end)
                self.assertGreaterEqual(times[-1], end - 0.25)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    async def test_actual_sheet_contains_payoff_in_final_half_second(self):
        source, output = self.folder / "payoff.mp4", self.folder / "sheet.jpg"
        await real_run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=160x90:r=10:d=9",
                        "-vf", "drawbox=color=lime:t=fill:enable='gte(t,8.5)'",
                        "-c:v", "libx264", "-preset", "ultrafast", str(source)])
        await visual_analysis.make_sheet(source, output, 100, 109, real_run, width=160, video_offset=100,
                                         zooms=[1, 1, 1, 1.8, 1.8, 1.8, 1, 1, 1])
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-i", str(output), "-vf", "crop=2:2:406:230,scale=1:1",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        rgb, stderr = await proc.communicate()
        self.assertEqual(proc.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(len(rgb), 3)
        self.assertGreater(rgb[1], rgb[0] + 50, f"Last frame misses the green payoff: RGB={list(rgb)}")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    async def test_sound_peak_is_search_evidence_only_and_detected_locally(self):
        audio = self.folder / "peak.wav"
        await real_run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=6",
                        "-af", "volume='if(between(t,2,3),1,0.01)':eval=frame", str(audio)])
        peaks = await visual_analysis.sound_peaks(audio)
        self.assertTrue(any(1 <= peak["time"] <= 3 and peak["relative_loudness"] >= 1.8 for peak in peaks))
        self.assertTrue(all("verdict" not in peak and "interesting" not in peak for peak in peaks))


if __name__ == "__main__":
    unittest.main()
