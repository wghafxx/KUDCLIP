"""Offline, deterministic editorial tests. Run with python -m unittest discover."""

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from editorial import EditorialInputTooLarge, _estimate, select_moments
from groq_pool import estimate_tokens


def candidate(first="S000001", last="S000003", hook="Вопрос понятен.", payoff="Ответ получен.", **kwargs):
    return {"start_id": first, "end_id": last, "topic": "Причина неожиданного результата",
            "genre": "story", "requires_visual": False,
            "hook": {"sentence_id": first, "quote": hook},
            "payoff": {"sentence_id": last, "quote": payoff}, **kwargs}


def accept(**kwargs):
    return {"verdict": "accept", "reason": "S000001 задаёт вопрос; S000003 даёт ответ.",
            "ratings": {key: 4 for key in ("standalone", "hook", "payoff", "genre_fit")},
            "gates": {key: True for key in ("standalone", "complete", "faithful", "boundaries", "visual_sufficient")},
            "title": "Понятный вопрос и ответ", "hashtags": ["#shorts"], **kwargs}


def visual_candidate(first="V000001", last="V000002", hook="Персонаж окружён противниками.", payoff="Игрок завершил задачу."):
    return {"start_id": first, "end_id": last, "topic": "Побег из окружения противников",
            "genre": "game", "requires_visual": True,
            "hook": {"observation_id": first, "description": hook},
            "payoff": {"observation_id": last, "description": payoff}}


SEGMENTS = [{"start": 0, "end": 5, "text": "Вопрос понятен."},
            {"start": 5, "end": 12, "text": "Теперь необходимое объяснение."},
            {"start": 12, "end": 18, "text": "Ответ получен."},
            {"start": 18, "end": 24, "text": "Следующая отдельная тема."}]


class Chat:
    def __init__(self, discovery=None, critic=None):
        self.discovery = discovery or (lambda payload: [candidate()])
        self.critic = critic or (lambda payload: accept())
        self.calls = []

    async def __call__(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        payload = json.loads(messages[1]["content"])
        if "DISCOVER" in messages[0]["content"]:
            return {"candidates": self.discovery(payload)}
        if "CRITIC" in messages[0]["content"]:
            return self.critic(payload)
        return {"candidate_ids": [item["id"] for item in payload["candidates"]][:payload["maximum"]]}


class EditorialTests(unittest.IsolatedAsyncioTestCase):
    async def run_selector(self, chat, **kwargs):
        settings = dict(segments=SEGMENTS, words=[], duration=24, video_title="Источник",
                        max_clips=2, min_duration=5, max_duration=20, genre="story",
                        audience="Русскоязычная аудитория", preferences="Законченные истории",
                        banned=[], highlights=[], chat=chat)
        settings.update(kwargs)
        return await select_moments(**settings)

    async def test_semantic_end_not_random_duration(self):
        result = await self.run_selector(Chat())
        self.assertEqual([(c["start"], c["end"]) for c in result], [(0, 18)])
        self.assertEqual(result[0]["payoff"]["quote"], "Ответ получен.")

    async def test_word_boundaries_remove_segment_silence(self):
        words = [{"word": "Вопрос", "start": 1, "end": 2}, {"word": "понятен.", "start": 2, "end": 3},
                 {"word": "Ответ", "start": 13, "end": 14}, {"word": "получен.", "start": 14, "end": 16}]
        result = await self.run_selector(Chat(), words=words)
        self.assertEqual((result[0]["start"], result[0]["end"]), (1, 16))

    async def test_fabricated_quote_unknown_id_and_wrong_order_rejected(self):
        for raw in [candidate(payoff="Этого нет в источнике."), candidate(first="S999999"),
                    candidate(first="S000003", last="S000001"), candidate(first=[]),
                    candidate(hook={"sentence_id": [], "quote": "Вопрос понятен."})]:
            chat = Chat(discovery=lambda _, raw=raw: [raw])
            self.assertEqual(await self.run_selector(chat), [])
            self.assertEqual(len(chat.calls), 1)

    async def test_banned_candidate_never_shifted_or_restored(self):
        chat = Chat()
        self.assertEqual(await self.run_selector(chat, banned=[(10, 11)]), [])
        self.assertEqual(len(chat.calls), 1)

    async def test_critic_veto_and_low_rating(self):
        for response in [accept(verdict="reject"), accept(gates={}),
                         accept(ratings={"standalone": 2, "hook": 4, "payoff": 4, "genre_fit": 4})]:
            self.assertEqual(await self.run_selector(Chat(critic=lambda _, response=response: response)), [])

    async def test_empty_discovery_returns_zero_without_padding(self):
        chat = Chat(discovery=lambda _: [])
        self.assertEqual(await self.run_selector(chat, max_clips=10), [])
        self.assertEqual(len(chat.calls), 1)

    async def test_complete_coverage_including_end_of_long_transcript(self):
        segments = [{"start": i * 5, "end": i * 5 + 5,
                     "text": f"Эпизод {i}: " + "объяснение " * 28 + "завершён."} for i in range(45)]
        final_text = segments[-1]["text"]
        def discovery(payload):
            if "S000045 " in payload["source"]:
                return [candidate("S000045", "S000045", final_text[:100], "завершён.")]
            return []
        chat = Chat(discovery=discovery)
        result = await self.run_selector(chat, segments=segments, duration=225, max_duration=15, min_duration=1)
        source_prompts = [json.loads(messages[1]["content"])["source"] for messages, _ in chat.calls
                          if "DISCOVER" in messages[0]["content"]]
        self.assertGreater(len(source_prompts), 1)
        for index in range(1, 46):
            self.assertTrue(any(f"S{index:06d} " in source for source in source_prompts), index)
        self.assertEqual((result[0]["start"], result[0]["end"]), (220, 225))
        self.assertTrue(all(_estimate(messages, kwargs["max_tokens"]) <= 6400 for messages, kwargs in chat.calls))

    async def test_overlap_and_same_topic_removed_after_critic(self):
        first = candidate()
        second = candidate("S000002", "S000004", "Теперь необходимое объяснение.", "Следующая отдельная тема.", topic="Совсем другая интересная тема")
        result = await self.run_selector(Chat(discovery=lambda _: [first, second]), max_duration=24)
        self.assertEqual(len(result), 1)

    async def test_cached_stages_resume_without_chat_or_progress(self):
        cache, saved = {}, []
        async def progress(key, value):
            saved.append((key, value))
        first = await self.run_selector(Chat(), cache=cache, progress=progress)
        self.assertEqual(len(saved), 2)
        async def forbidden_chat(*_, **__):
            raise AssertionError("Cached stages must not call chat")
        second = await self.run_selector(forbidden_chat, cache=cache, progress=progress)
        self.assertEqual(first, second)
        self.assertEqual(len(saved), 2)

    async def test_changed_preferences_invalidate_cache(self):
        cache = {}
        await self.run_selector(Chat(), cache=cache)
        chat = Chat()
        await self.run_selector(chat, cache=cache, preferences="Другой критерий")
        self.assertEqual(len(chat.calls), 2)

    async def test_one_revision_gets_new_independent_check(self):
        responses = iter([accept(verdict="revise", revision=candidate(last="S000004", payoff="Следующая отдельная тема.")), accept()])
        result = await self.run_selector(Chat(critic=lambda _: next(responses)), max_duration=24)
        self.assertEqual(result[0]["end"], 24)

    async def test_second_revision_is_rejected(self):
        chat = Chat(critic=lambda _: accept(verdict="revise", revision=candidate()))
        self.assertEqual(await self.run_selector(chat), [])
        self.assertEqual(len([1 for messages, _ in chat.calls if "CRITIC" in messages[0]["content"]]), 2)

    async def test_visual_dependency_returns_explicit_provisional_for_final_video_check(self):
        chat = Chat(discovery=lambda _: [candidate(requires_visual=True, genre="game")])
        result = await self.run_selector(chat, genre="game")
        self.assertTrue(result[0]["requires_visual"])
        self.assertTrue(result[0]["visual_pending"])
        self.assertFalse(result[0]["visual_verified"])
        self.assertEqual(len(chat.calls), 2)
        observations = [{"start": 0, "end": 18, "description": "Игрок завершил задачу", "interesting": True, "reason": "Неожиданный успех"}]
        result = await self.run_selector(Chat(discovery=chat.discovery, critic=lambda _: accept(visual_verified=True)),
                                         genre="game", visual_context=observations)
        self.assertFalse(result[0]["visual_verified"])
        self.assertTrue(result[0]["visual_pending"])

    async def test_visual_only_event_does_not_require_or_invent_speech(self):
        observations = [
            {"start": 0, "end": 8, "description": "Персонаж окружён противниками.", "interesting": True},
            {"start": 8, "end": 18, "description": "Игрок завершил задачу.", "interesting": True},
        ]
        chat = Chat(discovery=lambda _: [visual_candidate()], critic=lambda _: accept(visual_verified=True))
        result = await self.run_selector(chat, segments=[], words=[], genre="game", visual_context=observations)
        self.assertEqual((result[0]["start"], result[0]["end"]), (0, 18))
        self.assertTrue(result[0]["visual_only"])
        self.assertEqual(result[0]["source_kind"], "visual")
        self.assertEqual(result[0]["hook"]["description"], "Персонаж окружён противниками.")
        self.assertNotIn("quote", result[0]["hook"])
        critic_payload = json.loads(chat.calls[-1][0][1]["content"])
        self.assertIn("V000001", critic_payload["candidate_source"])
        self.assertNotIn("S000001", critic_payload["candidate_source"])

    async def test_visual_evidence_must_be_real_and_observation_range_long_enough(self):
        observations = [{"start": 0, "end": 18, "description": "Персонаж окружён противниками.", "interesting": True}]
        chat = Chat(discovery=lambda _: [visual_candidate(last="V000001", payoff="Несуществующая победа.")])
        self.assertEqual(await self.run_selector(chat, segments=[], visual_context=observations, genre="game"), [])
        self.assertEqual(len(chat.calls), 1)
        observations[0]["end"] = 1
        chat = Chat(discovery=lambda _: [visual_candidate(last="V000001", payoff="Персонаж окружён противниками.")])
        self.assertEqual(await self.run_selector(chat, segments=[], visual_context=observations, genre="game"), [])
        self.assertEqual(len(chat.calls), 1)

    async def test_visual_boundaries_are_not_replaced_by_word_boundaries(self):
        observations = [{"start": 0, "end": 18, "description": "Персонаж окружён противниками.", "interesting": True}]
        chat = Chat(discovery=lambda _: [visual_candidate(last="V000001", payoff="Персонаж окружён противниками.")],
                    critic=lambda _: accept(visual_verified=True))
        result = await self.run_selector(chat, segments=[], genre="game", visual_context=observations,
                                         words=[{"word": "случайная реплика", "start": 5, "end": 7}])
        self.assertEqual((result[0]["start"], result[0]["end"]), (0, 18))

    async def test_final_comparison_enforces_cap_after_independent_critics(self):
        segments = []
        proposals = []
        for index in range(4):
            first = f"Начало отдельной истории номер {index}."
            last = f"Итог отдельной истории номер {index}."
            segments.extend([{"start": index * 20, "end": index * 20 + 5, "text": first},
                             {"start": index * 20 + 5, "end": index * 20 + 15, "text": last}])
            proposals.append(candidate(f"S{index * 2 + 1:06d}", f"S{index * 2 + 2:06d}", first, last,
                                       topic=f"Уникальное событие {index}"))
        chat = Chat(discovery=lambda _: proposals)
        result = await self.run_selector(chat, segments=segments, duration=80, max_clips=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(len([1 for messages, _ in chat.calls if "CRITIC" in messages[0]["content"]]), 3)
        self.assertGreaterEqual(len([1 for messages, _ in chat.calls if "COMPARE" in messages[0]["content"]]), 2)

    async def test_completed_windows_resume_after_quota_failure(self):
        segments = [{"start": i * 5, "end": i * 5 + 5,
                     "text": f"Событие {i}: " + "подробность " * 30 + "конец."} for i in range(40)]
        baseline = Chat(discovery=lambda _: [])
        await self.run_selector(baseline, segments=segments, duration=200)
        cache, count = {}, 0
        async def quota_chat(messages, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("quota exhausted")
            return {"candidates": []}
        with self.assertRaisesRegex(RuntimeError, "quota exhausted"):
            await self.run_selector(quota_chat, segments=segments, duration=200, cache=cache)
        self.assertEqual(len(cache), 1)
        resumed = Chat(discovery=lambda _: [])
        await self.run_selector(resumed, segments=segments, duration=200, cache=cache)
        self.assertEqual(len(resumed.calls), len(baseline.calls) - 1)

    async def test_provider_errors_propagate_without_random_fallback(self):
        async def failing_chat(*_, **__):
            raise RuntimeError("daily quota")
        with self.assertRaisesRegex(RuntimeError, "daily quota"):
            await self.run_selector(failing_chat)

    async def test_oversized_exact_candidate_fails_explicitly(self):
        segments = [{"start": i * 5, "end": i * 5 + 5, "text": chr(1072 + i) * 800 + "."} for i in range(4)]
        chat = Chat(discovery=lambda _: [candidate("S000001", "S000004", "а" * 350, "г" * 350)])
        with self.assertRaises(EditorialInputTooLarge):
            await self.run_selector(chat, segments=segments, duration=20, max_duration=30, preferences="")

    async def test_valid_long_preferences_compact_without_losing_source_coverage(self):
        segments = [{"start": i * 10, "end": i * 10 + 10, "text": "я" * 150 + "."} for i in range(22)]
        chat = Chat(discovery=lambda _: [])
        self.assertEqual(await self.run_selector(chat, segments=segments, duration=220,
                                                preferences="начало " + "я" * 2000 + " конец"), [])
        sources = []
        for messages, kwargs in chat.calls:
            payload = json.loads(messages[1]["content"])
            sources.append(payload["source"])
            self.assertLessEqual(len(payload["settings"]["preferences"]), 800)
            self.assertTrue(payload["settings"]["preferences"].endswith("конец"))
            actual = estimate_tokens({"model": "qwen/qwen3.8-27b", "messages": messages,
                                      "max_tokens": kwargs["max_tokens"], "response_format": {"type": "json_object"}})
            self.assertEqual(_estimate(messages, kwargs["max_tokens"]), actual)
            self.assertLessEqual(actual, 6400)
        for index in range(1, 23):
            self.assertTrue(any(f"S{index:06d} " in source for source in sources))

    async def test_multibyte_windows_adapt_to_actual_free_budget(self):
        segments = [{"start": i * 5, "end": i * 5 + 5, "text": "🎮" * 600 + "."} for i in range(12)]
        chat = Chat(discovery=lambda _: [])
        await self.run_selector(chat, segments=segments, duration=60, preferences="я" * 2000)
        sources = [json.loads(messages[1]["content"])["source"] for messages, _ in chat.calls]
        for index in range(1, 13):
            self.assertTrue(any(f"S{index:06d} " in source for source in sources))
        self.assertTrue(all(_estimate(messages, kwargs["max_tokens"]) <= 6400 for messages, kwargs in chat.calls))

    async def test_optional_visual_context_is_bounded_not_candidate_source(self):
        observations = [{"start": 0, "end": 18, "description": "Наблюдаемое игровое действие " * 25,
                         "reason": "Видимые факты " * 20, "interesting": True} for _ in range(5)]
        chat = Chat(discovery=lambda payload: [candidate()] if "S000001" in payload["source"] else [])
        result = await self.run_selector(chat, visual_context=observations)
        self.assertEqual(len(result), 1)
        for messages, _ in chat.calls:
            if "CRITIC" in messages[0]["content"]:
                payload = json.loads(messages[1]["content"])
                self.assertLessEqual(len(json.dumps(payload["observations"], ensure_ascii=False, separators=(",", ":"))), 1000)
                self.assertIn("Вопрос понятен.", payload["candidate_source"])
                self.assertIn("Ответ получен.", payload["candidate_source"])

    async def test_time_overlap_keeps_maximum_length_story_across_window_boundaries(self):
        segments = [{"start": i * 3, "end": i * 3 + 3, "text": f"Фрагмент {i}: " + "история " * 12 + "конец."}
                    for i in range(100)]
        chat = Chat(discovery=lambda _: [])
        await self.run_selector(chat, segments=segments, duration=300, max_duration=62, min_duration=15)
        windows = []
        for messages, _ in chat.calls:
            if "DISCOVER" in messages[0]["content"]:
                payload = json.loads(messages[1]["content"])
                stamps = __import__("re").findall(r"\[([\d.]+)–([\d.]+)\]", payload["source"])
                windows.append((min(float(start) for start, _ in stamps), max(float(end) for _, end in stamps)))
        for start in range(0, 239, 3):
            self.assertTrue(any(first <= start and last >= start + 62 for first, last in windows), (start, windows))

    async def test_speech_discovery_receives_local_visual_facts_and_screen_context(self):
        seen = []
        chat = Chat(discovery=lambda payload: seen.append(payload) or [])
        context = "Прятки в CS2: люди стали предметами"
        await self.run_selector(chat, genre="game", context_line=context,
                                visual_context=[{"start": 0, "end": 18, "description": "Кружка убегает от игрока",
                                                 "interesting": True, "reason": "Погоня за замаскированным участником"}])
        speech = next(payload for payload in seen if "S000001" in payload["source"])
        self.assertEqual(speech["settings"]["context_line"], context)
        self.assertIn("Кружка", json.dumps(speech["local_observations"], ensure_ascii=False))
        self.assertLessEqual(len(json.dumps(speech["local_observations"], ensure_ascii=False, separators=(",", ":"))), 700)

    async def test_visual_pending_does_not_relax_semantic_gates_or_scores(self):
        gates = {key: True for key in ("standalone", "complete", "faithful", "boundaries")}
        gates["visual_sufficient"] = False
        result = await self.run_selector(Chat(critic=lambda _: accept(gates=gates)), genre="game")
        self.assertTrue(result[0]["visual_pending"])
        self.assertFalse(result[0]["visual_verified"])
        for failed in ("standalone", "complete", "faithful", "boundaries"):
            broken = {**gates, failed: False}
            self.assertEqual(await self.run_selector(Chat(critic=lambda _, broken=broken: accept(gates=broken))), [])
        scores = {"standalone": 4, "hook": 4, "payoff": 2, "genre_fit": 4}
        self.assertEqual(await self.run_selector(Chat(critic=lambda _: accept(gates=gates, ratings=scores))), [])

    async def test_screen_context_is_given_to_critic_and_returned_for_actual_render(self):
        chat = Chat()
        line = "Прятки в CS2: люди стали предметами"
        result = await self.run_selector(chat, context_line=line)
        self.assertEqual(result[0]["context_line"], line)
        critic = next(json.loads(messages[1]["content"]) for messages, _ in chat.calls if "CRITIC" in messages[0]["content"])
        self.assertEqual(critic["settings"]["context_line"], line)

    @unittest.skipUnless(Path(r"C:\Users\lolar\Documents\Codex\2026-11-06\c-users-lolar-desktop-v2-0\work\sample_run_2026_11_07\data\analyses.json").exists(),
                         "Real CS2 sample transcript is not present in this workspace")
    async def test_real_cs2_audited_stories_have_complete_budgeted_windows(self):
        path = Path(r"C:\Users\lolar\Documents\Codex\2026-11-06\c-users-lolar-desktop-v2-0\work\sample_run_2026_11_07\data\analyses.json")
        records = json.loads(path.read_text(encoding="utf-8"))
        analysis = next(item for item in records if item.get("id") == "8603288fdd95b00fb130feb0")
        observations = []
        for key, value in analysis.get("stages", {}).items():
            if key.startswith("frames-v2:overview:") and isinstance(value, dict):
                observations.extend(value.get("observations", []))
        chat = Chat(discovery=lambda _: [])
        await self.run_selector(chat, segments=analysis["segments"], words=analysis["words"],
                                duration=max(segment["end"] for segment in analysis["segments"]),
                                min_duration=15, max_duration=62, genre="game", visual_context=observations,
                                context_line="Прятки в CS2: люди стали предметами")
        windows = []
        for messages, kwargs in chat.calls:
            self.assertLessEqual(_estimate(messages, kwargs["max_tokens"]), 6400)
            if "[EDITORIAL:DISCOVER]" not in messages[0]["content"]:
                continue
            payload = json.loads(messages[1]["content"])
            stamps = __import__("re").findall(r"\[([\d.]+)–([\d.]+)\]", payload["source"])
            windows.append((min(float(start) for start, _ in stamps), max(float(end) for _, end in stamps)))
        for start, end in [(546, 584.1), (1787.7, 1819), (1843, 1904),
                           (546, 608), (1787.7, 1849.7), (1843, 1905)]:
            self.assertTrue(any(first <= start and last >= end for first, last in windows), (start, end, windows))


if __name__ == "__main__":
    unittest.main()
