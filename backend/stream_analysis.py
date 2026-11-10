"""Bounded editorial review windows over the complete local stream transcript."""

import math

AUTHOR_DURATION_LIMITS = {"youtube": 3 * 3600, "twitch": 7 * 3600}
_MAX_WINDOWS = {"youtube": 10, "twitch": 14}
_WINDOW_SECONDS = 180


def review_windows(source, duration, segments):
    """Cover the timeline in blocks and pick a candidate area in every block.

    Viewer clips, chat bursts and replay peaks take precedence in each block.
    Elsewhere, prefer dialogue over dead air. These signals choose what to
    inspect closely. They never qualify an episode for a finished clip.
    """
    count = min(_MAX_WINDOWS[source['provider']], math.ceil(duration / 720))
    step = duration / count
    cues = [[] for _ in range(count)]
    for window in source.get('windows') or []:
        if window.get('kind') == 'sample':
            continue
        try:
            start, end = float(window['start']), float(window['end'])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start < end <= duration:
            center = (start + end) / 2
            heat = window.get('heat')
            priority = heat if isinstance(heat, (int, float)) and math.isfinite(heat) else 0
            cues[min(count - 1, int(center / step))].append((center, priority))

    # With half-window strides an episode at the boundary of two windows has
    # another opportunity to be considered in full.
    stride = _WINDOW_SECONDS // 2
    speech = [0.0] * math.ceil(duration / stride)
    for segment in segments:
        if not str(segment.get('text') or '').strip():
            continue
        start, end = max(0.0, segment['start']), min(duration, segment['end'])
        if start >= end:
            continue
        for index in range(int(start // stride), min(len(speech), math.ceil(end / stride))):
            speech[index] += min(end, (index + 1) * stride) - max(start, index * stride)

    result = []
    for index, markers in enumerate(cues):
        first, last = index * step, (index + 1) * step if index + 1 < count else duration
        width = min(_WINDOW_SECONDS, last - first)
        midpoint = (first + last) / 2
        if markers:
            center, _ = max(markers, key=lambda marker: (marker[1], -abs(marker[0] - midpoint)))
        else:
            blocks = range(math.ceil(first / stride), math.floor(last / stride) - 1)
            peak = max(blocks, key=lambda block: (speech[block] + speech[block + 1],
                       -abs((block + 1) * stride - midpoint)), default=None)
            center = (peak + 1) * stride if peak is not None else midpoint
        start = max(first, min(last - width, center - width / 2))
        result.append((round(start, 3), min(duration, round(start + width, 3))))
    return result
