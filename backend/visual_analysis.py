"""Sparse visual evidence and local sound peaks; no additional paid services."""
import asyncio
import array
import base64
import hashlib
import math
import statistics
import json
from pathlib import Path

from groq_pool import estimate_tokens

VERSION = 'frames-v2'
VERDICT_POLICY = 'multimodal-motion-v2'
DENSE_POLICY = 'multimodal-tail-v2'
MOTION_POLICY = 'motion-triplets-v1'
VISION_TOKEN_BUDGET = 6400


def sheet_times(start, end):
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError('Некорректный интервал для проверки кадров')
    # Include the final source frames: a payoff near the end must not fall into
    # the old unsampled last ninth of every contact sheet.
    last = end - min(0.1, (end - start) / 10)
    step = (last - start) / 8
    return [min(last, max(start, round(start + step * i, 3))) for i in range(9)]


async def make_sheet(video, output, start, end, run, width=320, *, times=None, video_offset=0, zooms=None):
    video, output = Path(video), Path(output)
    default_times = sheet_times(start, end)
    times = default_times if times is None else list(times)
    if len(times) != 9 or any(not math.isfinite(t) or not start <= t < end for t in times):
        raise ValueError('Для сетки нужны девять таймкодов внутри эпизода')
    if not math.isfinite(video_offset) or video_offset < 0 or any(t < video_offset for t in times):
        raise ValueError('Временное смещение HD-фрагмента не соответствует эпизоду')
    zooms = [1.0] * 9 if zooms is None else list(zooms)
    if len(zooms) != 9 or any(not math.isfinite(zoom) or not 1 <= zoom <= 3 for zoom in zooms):
        raise ValueError('Недопустимое увеличение кадров')
    if width < 32 or width > 1920:
        raise ValueError('Некорректный размер кадра')
    height = int(width * 9 / 16)
    command = ['ffmpeg', '-y']
    chains = []
    for index, second in enumerate(times):
        command += ['-threads', '1', '-ss', str(max(0, second - video_offset)), '-i', str(video)]
        crop = f'crop=iw/{zooms[index]}:ih/{zooms[index]}:(iw-ow)/2:(ih-oh)/2,' if zooms[index] > 1 else ''
        chains.append(f'[{index}:v]trim=end_frame=1,setpts=PTS-STARTPTS,{crop}'
                      f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                      f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[f{index}]')
    layout = '|'.join(f'{(i % 3) * (width + 2)}_{(i // 3) * (height + 2)}' for i in range(9))
    chains.append(''.join(f'[f{i}]' for i in range(9)) + f'xstack=inputs=9:layout={layout}:fill=black[sheet]')
    command += ['-filter_complex', ';'.join(chains), '-map', '[sheet]', '-frames:v', '1', '-q:v', '3', str(output)]
    if output.resolve() == video.resolve():
        raise ValueError('Сетка кадров должна сохраняться отдельно от исходника')
    output.unlink(missing_ok=True)
    await run(command, timeout=120)
    if not output.exists():
        raise RuntimeError('Не удалось извлечь кадры для проверки момента')
    return times


async def sound_peaks(audio):
    proc = await asyncio.create_subprocess_exec(
        'ffmpeg', '-v', 'error', '-i', str(audio), '-f', 's16le', '-ac', '1', '-ar', '8000', 'pipe:1',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), 120)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode or not raw:
        return []
    samples = array.array('h')
    samples.frombytes(raw[:len(raw) // 2 * 2])
    levels = []
    for index in range(0, len(samples), 8000):
        chunk = samples[index:index + 8000]
        levels.append(math.sqrt(sum(x * x for x in chunk) / max(1, len(chunk))))
    baseline = max(1, statistics.median(levels)) if levels else 1
    peaks = []
    for second in sorted(range(len(levels)), key=lambda i: levels[i], reverse=True):
        if levels[second] < baseline * 1.8 or any(abs(second - x['time']) < 20 for x in peaks):
            continue
        peaks.append({'time': second, 'relative_loudness': round(levels[second] / baseline, 2)})
        if len(peaks) == 6:
            break
    return sorted(peaks, key=lambda item: item['time'])


def _sheet_messages(times, purpose, encoded):
    prompt = (
        'На изображении сетка 3x3 кадров одного видео, читать слева направо и сверху вниз. '
        f'Приблизительные секунды кадров: {times}. {purpose}\n'
        'Описывай только то, что действительно видно. Не выдумывай убийства, победу, смех, '
        'счёт или эмоции по факту наличия игры. Разреженные кадры могут пропустить быстрое действие. '
        'Не считай громкость или плотность речи доказательством интереса. '
        'Укажи наблюдаемое действие, изменение и исход, если он виден; иначе явно скажи, что исход неизвестен. '
        'Неразличимый маленький предмет/движение между stills — insufficient, не contradiction. '
        'Reject только при явном противоположном факте: укажи contradiction_evidence с таймкодом. '
        'Если дана точная речь из этой сцены, она может совместно с совместимым видимым действием подтвердить смысл; '
        'не выдавай речь за видимую форму предмета. Для joint цитируй speech_evidence дословно из предоставленного ASR. '
        'Пиши кратко: не более 3 observations, description до 120 символов, reason до 60 символов; общий reason до 120 символов. '
        'JSON: {"observations":[{"start":секунда,"end":секунда,"description":"факты",'
        '"interesting":true/false,"reason":"что конкретно необычно или почему неясно"}],'
        '"complete_event":true/false,"verdict":"accept/reject/uncertain","reason":"...",'
        '"evidence_status":"support/contradiction/insufficient","support_mode":"visual/joint/speech_only",'
        '"speech_evidence":[{"quote":"дословно","start":секунда,"end":секунда}],'
        '"contradiction_evidence":[{"start":секунда,"end":секунда,"description":"противоположный факт"}]}'
    )
    return [
        {'role': 'system', 'content': 'Ты проверяешь визуальные доказательства в видео. Только JSON и наблюдаемые факты.'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + encoded}},
        ]},
    ]


def _sheet_cost(times, purpose):
    return estimate_tokens({'model': 'qwen/qwen3.8-27b',
                            'messages': _sheet_messages(times, purpose, ''),
                            'max_tokens': 700, 'response_format': {'type': 'json_object'}})


async def describe_sheet(path, times, vision, purpose):
    if _sheet_cost(times, purpose) > VISION_TOKEN_BUDGET:
        raise RuntimeError('Точное визуальное доказательство не помещается в бесплатный бюджет запроса')
    encoded = base64.b64encode(path.read_bytes()).decode('ascii')
    return await vision(_sheet_messages(times, purpose, encoded), temperature=0.1, max_tokens=700)


async def overview(video, duration, folder, stages, save, vision, run, peaks=None):
    observations = []
    # 90-second sheets cover the entire source, rather than only the beginning.
    intervals = [(float(s), min(float(s + 90), duration)) for s in range(0, math.ceil(duration), 90)]
    # Louder audio is a search hint only, never an acceptance criterion.
    for peak in peaks or []:
        t = float(peak['time'])
        intervals.append((max(0, t - 8), min(duration, t + 10)))
    for index, (start, end) in enumerate(intervals):
        key = f'{VERSION}:overview:{start:.2f}:{end:.2f}'
        if key not in stages:
            sheet = folder / f'overview_{index}.jpg'
            times = await make_sheet(video, sheet, start, end, run)
            result = await describe_sheet(sheet, times, vision, 'Найди возможные необычные события и их контекст для дальнейшего просмотра.')
            if not isinstance(result, dict) or not isinstance(result.get('observations'), list):
                raise RuntimeError('Модель вернула некорректное описание кадров')
            stages[key] = result
            await save(key, result)
            sheet.unlink(missing_ok=True)
        for item in stages[key].get('observations', []):
            try:
                s, e = float(item['start']), float(item['end'])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(s) and math.isfinite(e) and start <= s <= e <= end + 0.1:
                observations.append({'start': s, 'end': e, 'description': str(item.get('description', ''))[:600],
                                     'interesting': item.get('interesting') is True, 'reason': str(item.get('reason', ''))[:400]})
    return observations


def _normal_text(text):
    return ' '.join(str(text or '').split())


def _claim(moment):
    claim = {key: moment.get(key) for key in ('title', 'hook', 'payoff', 'genre', 'context_line') if moment.get(key) is not None}
    claim['selection_reason'] = str(moment.get('selection_reason') or '')[:400]
    return claim


def _speech_context(transcript, start, end):
    result = []
    for item in transcript or []:
        if not isinstance(item, dict):
            continue
        text = _normal_text(item.get('text', item.get('word', '')))
        try:
            first, last = float(item['start']), float(item['end'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not text or not math.isfinite(first) or not math.isfinite(last) or last <= first:
            continue
        # Never present words from a mostly outside ASR segment as on-scene proof.
        if first >= start - 0.5 and last <= end + 0.5 and last > start and first < end:
            result.append({'start': max(start, first), 'end': min(end, last), 'text': text})
    return sorted(result, key=lambda item: (item['start'], item['end']))


def _anchor(moment, field, speech, fallback):
    evidence = moment.get(field) or {}
    quote = _normal_text(evidence.get('quote', '')) if isinstance(evidence, dict) else ''
    if quote:
        for line in speech:
            if quote.casefold() in line['text'].casefold():
                return (line['start'] + line['end']) / 2
    return fallback


def _prompt_speech(moment, speech, maximum=1100):
    if not speech:
        return []
    anchors = [_anchor(moment, field, speech, -1) for field in ('hook', 'payoff')]
    priority = sorted(range(len(speech)), key=lambda index: (
        0 if any(speech[index]['start'] <= anchor <= speech[index]['end'] for anchor in anchors) else 1,
        0 if index in (0, len(speech) - 1) else 1, index))
    chosen = []
    for index in priority:
        candidate = chosen + [speech[index]]
        if len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))) <= maximum:
            chosen.append(speech[index])
    return sorted(chosen, key=lambda item: item['start'])


def _joint_purpose(moment, speech, extra):
    return ('Утверждение: ' + json.dumps(_claim(moment), ensure_ascii=False, sort_keys=True) +
            '. Точная речь ASR ИМЕННО из выбранной сцены (не пересказ после неё): ' +
            json.dumps(speech, ensure_ascii=False, sort_keys=True) + '. ' + extra +
            ' context_line будет на экране и задаёт правила игры, но не доказывает исход. '
            'В прятках исход — раскрытие/побег/узнанная маскировка; убийство не обязательно. '
            'Нельзя отвергать прямую реплику об исходе только потому, что маленький prop неразличим на stills: '
            'нужны совместимые кадры действия И точная речь; один ASR без видимого действия недостаточен.')


async def verify(video, moment, folder, stages, save, vision, run, *, transcript=None, video_offset=0):
    start, end = float(moment['start']), float(moment['end'])
    sheet_times(start, end)  # Validate before touching the cache or a subprocess.
    if transcript is None:
        transcript = moment.get('source_transcript', moment.get('transcript', []))
    speech = _prompt_speech(moment, _speech_context(transcript, start, end))
    signature = {'version': VERSION, 'evidence_policy': VERDICT_POLICY,
                 'start': start, 'end': end, 'source': str(video),
                 'video_offset': video_offset, 'speech': speech, 'claim': _claim(moment)}
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    key = 'visual-verdict:' + digest
    if key not in stages:
        sheet = folder / f'detail_{digest}.jpg'
        times = await make_sheet(video, sheet, start, end, run, width=640, video_offset=video_offset)
        result = await describe_sheet(sheet, times, vision,
            _joint_purpose(moment, speech, 'Проверь законченный эпизод по всей сетке. Отсутствие детали в редких кадрах — insufficient; '
                           'contradiction требует конкретного противоположного факта. Все таймкоды абсолютные.'))
        if not isinstance(result, dict) or result.get('verdict') not in {'accept', 'reject', 'uncertain'}:
            raise RuntimeError('Модель не смогла проверить визуальную развязку')
        result = _validated_verdict(result, start, end, speech)
        stages[key] = result
        await save(key, result)
        sheet.unlink(missing_ok=True)
    initial = _validated_verdict(stages[key], start, end, speech)
    requires_visual = moment.get('genre') == 'game' or moment.get('visual_only') is True or moment.get('requires_visual') is True
    if initial.get('verdict') != 'uncertain' or not requires_visual:
        return initial
    if speech:
        motion = await _motion_verify(video, moment, initial, speech, start, end, folder, stages, save, vision, run, video_offset)
        if motion.get('verdict') in {'accept', 'reject'}:
            return motion
        initial = motion
    return await _dense_tail(video, moment, initial, start, end, folder, stages, save, vision, run, speech, video_offset)


def _motion_times(moment, speech, start, end):
    first = _anchor(moment, 'hook', speech, start + min(2, (end - start) / 5))
    last = _anchor(moment, 'payoff', speech, end - min(2, (end - start) / 5))
    first, last = sorted((max(start, first), min(end - 0.1, last)))
    anchors = (first, (first + last) / 2, last)
    return [max(start, min(end - 0.1, round(anchor + delta, 3)))
            for anchor in anchors for delta in (-0.3, 0, 0.3)]


async def _motion_verify(video, moment, initial, speech, start, end, folder, stages, save, vision, run, video_offset):
    times = _motion_times(moment, speech, start, end)
    facts = (initial.get('observations') or [])[:1]
    purpose = _joint_purpose(moment, speech,
        'Это три короткие серии движения: верхний ряд — хук, средний — действие (центр увеличен 1.8x), нижний — итог. '
        'Каждый ряд — 3 последовательных кадра через 0.3s. Сравни движение предмета относительно окружения, '
        'а не только движение камеры. Общие кадры плюс точная прямая речь могут совместно подтвердить погоню/раскрытие. '
        'Не требуй различить мелкую деталь предмета, если его название прямо звучит в сцене; не выдумывай его форму. '
        'Речь сама по себе без совместимого действия не достаточна. Первые визуальные факты: ' +
        json.dumps(facts, ensure_ascii=False, sort_keys=True))
    if _sheet_cost(times, purpose) > VISION_TOKEN_BUDGET:
        facts = []
        purpose = _joint_purpose(moment, speech,
            'Три серии движения по 3 кадра через0.3s: хук/действие(центр1.8x)/итог. '
            'Нужно совместимое реальное действие плюс точная речь; маленькая форма может быть неразличима. '
            'Одного ASR или движения камеры недостаточно.')
    signature = {'policy': MOTION_POLICY, 'source': str(video), 'offset': video_offset, 'times': times, 'purpose': purpose}
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    key = 'visual-motion:' + digest
    if key not in stages:
        sheet = folder / f'motion_{digest}.jpg'
        await make_sheet(video, sheet, start, end, run, width=640, times=times, video_offset=video_offset,
                         zooms=[1, 1, 1, 1.8, 1.8, 1.8, 1, 1, 1])
        result = await describe_sheet(sheet, times, vision, purpose)
        if not isinstance(result, dict) or result.get('verdict') not in {'accept', 'reject', 'uncertain'}:
            raise RuntimeError('Модель не смогла проверить движение в эпизоде')
        stages[key] = _validated_verdict(result, start, end, speech)
        await save(key, stages[key])
        sheet.unlink(missing_ok=True)
    motion = _validated_verdict(stages[key], start, end, speech)
    return {**motion, 'observations': (initial.get('observations') or []) + motion.get('observations', []),
            'motion_verification': {'times': times, 'verdict': motion.get('verdict')}}


def _dense_purpose(moment, initial, start, end, tail_start, facts, speech=None):
    return _joint_purpose(moment, speech or [],
        f'Плотная проверка хвоста [{tail_start:.2f},{end:.2f}] полного эпизода [{start:.2f},{end:.2f}]. '
        'Начальные факты: ' + json.dumps(facts, ensure_ascii=False, sort_keys=True) +
        '. Контекст первой сетки, движение, точная речь и хвост могут совместно подтвердить весь заявленный эпизод; '
        'complete_event относится к их совместному доказательству. Нужен конкретный payoff, не любое движение. '
        'Новые observations только из хвоста с абсолютными секундами. Нет подтверждения — uncertain, '
        'видимое противоречие — contradiction; не приравнивай эти случаи.')


async def _dense_tail(video, moment, initial, start, end, folder, stages, save, vision, run, speech=None, video_offset=0):
    tail_start = max(start, end - 10.0)
    times = sheet_times(tail_start, end)
    source_facts = initial.get('observations') or []
    # Preserve complete source facts rather than cutting a sentence in half.
    facts = source_facts[:1] + source_facts[-1:] if len(source_facts) > 1 else list(source_facts)
    purpose = _dense_purpose(moment, initial, start, end, tail_start, facts, speech)
    while facts and _sheet_cost(times, purpose) > VISION_TOKEN_BUDGET:
        facts.pop()
        purpose = _dense_purpose(moment, initial, start, end, tail_start, facts, speech)
    if _sheet_cost(times, purpose) > VISION_TOKEN_BUDGET:
        raise RuntimeError('Полный заявленный смысл не помещается в бесплатную проверку развязки')
    signature = {'policy': DENSE_POLICY, 'source': str(video), 'offset': video_offset, 'start': start, 'end': end,
                 'tail_start': tail_start, 'purpose': purpose}
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    key = 'visual-tail:' + digest
    if key not in stages:
        sheet = folder / f'tail_{digest}.jpg'
        await make_sheet(video, sheet, tail_start, end, run, width=640, video_offset=video_offset)
        # The initial response was already durably saved. A quota exception here
        # leaves it reusable; only the unfinished tail request runs on retry.
        result = await describe_sheet(sheet, times, vision, purpose)
        if not isinstance(result, dict) or result.get('verdict') not in {'accept', 'reject', 'uncertain'}:
            raise RuntimeError('Модель не смогла уточнить визуальную развязку')
        # New visual facts must be in the tail; speech can legitimately carry
        # setup/payoff from elsewhere inside the original selected episode.
        result = _validated_verdict(result, tail_start, end, speech)
        stages[key] = result
        await save(key, result)
        sheet.unlink(missing_ok=True)
    tail = _validated_verdict(stages[key], tail_start, end, speech)
    combined = dict(tail)
    combined['observations'] = source_facts + tail.get('observations', [])
    combined['dense_verification'] = {'start': tail_start, 'end': end,
                                       'initial_verdict': initial.get('verdict'), 'verdict': tail.get('verdict')}
    if tail.get('verdict') != 'accept' or tail.get('complete_event') is not True:
        combined.update(verdict='reject', complete_event=False,
                        reason='Развязка не подтверждена плотной проверкой; эпизод отклонён. ' + str(tail.get('reason') or '')[:350])
    return combined


def _validated_facts(observations, start, end):
    facts = []
    for observation in observations if isinstance(observations, list) else []:
        if not isinstance(observation, dict):
            continue
        description = observation.get('description')
        if not isinstance(description, str) or not description.strip():
            continue
        try:
            first, last = float(observation['start']), float(observation['end'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(first) and math.isfinite(last) and start <= first <= end and first <= last <= end + 0.1:
            facts.append({**observation, 'start': first, 'end': min(end, last), 'description': description.strip()})
    return facts


def _validated_speech(evidence, speech):
    result = []
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict):
            continue
        quote = _normal_text(item.get('quote'))
        if not quote:
            continue
        for line in speech or []:
            if quote in _normal_text(line['text']):
                result.append({'start': line['start'], 'end': line['end'], 'quote': quote})
                break
    return result


def _validated_verdict(result, start, end, speech=None):
    """A claimed accept without actual in-range source facts is uncertainty."""
    result = dict(result)
    facts = _validated_facts(result.get('observations'), start, end)
    result['observations'] = facts
    result['speech_evidence'] = _validated_speech(result.get('speech_evidence'), speech)
    contradictions = _validated_facts(result.get('contradiction_evidence'), start, end)
    result['contradiction_evidence'] = contradictions
    if result.get('evidence_status') == 'contradiction' and contradictions:
        result.update(verdict='reject', complete_event=False)
    if result.get('verdict') == 'reject' and (result.get('evidence_status') != 'contradiction' or not contradictions):
        result.update(verdict='uncertain', complete_event=False, evidence_status='insufficient')
    joint_invalid = result.get('support_mode') in {'joint', 'speech_only'} and (
        result.get('support_mode') == 'speech_only' or not result['speech_evidence'])
    mode_invalid = result.get('support_mode', 'visual') not in {'visual', 'joint', 'speech_only'}
    status_invalid = result.get('evidence_status', 'support') != 'support'
    if result.get('verdict') == 'accept' and (result.get('complete_event') is not True or not facts or joint_invalid or mode_invalid or status_invalid):
        result.update(verdict='uncertain', complete_event=False,
                      reason='Заявленный завершённый эпизод не подтверждён фактическими наблюдениями внутри выбранных границ. ' +
                             str(result.get('reason') or '')[:350])
    return result
