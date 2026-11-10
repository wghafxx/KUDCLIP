"""Unlimited local speech recognition with faster-whisper (GTX 10xx: int8 on CUDA)."""

import json
import logging
import os
from pathlib import Path
import sys
from functools import lru_cache

import gpu_runtime


logger = logging.getLogger(__name__)
_CUDA_DLL_HANDLES = []
_CUDA_FAILED = False
_MODEL_DIR = Path(__file__).resolve().parent / 'models' / 'whisper'

def _cuda_dlls():
    # pip wheels nvidia-cublas-cu12 / nvidia-cudnn-cu12 keep DLLs outside PATH on Windows.
    if os.name != "nt":
        return
    for folder in Path(sys.prefix, 'Lib', 'site-packages', 'nvidia').glob('*/bin'):
        _CUDA_DLL_HANDLES.append(os.add_dll_directory(str(folder)))
        os.environ['PATH'] = str(folder) + os.pathsep + os.environ.get('PATH', '')


_cuda_dlls()


@lru_cache(maxsize=1)
def _cuda_supported():
    try:
        import ctranslate2
        import faster_whisper  # noqa: F401
        return ctranslate2.get_cuda_device_count() > 0 and 'int8' in ctranslate2.get_supported_compute_types('cuda')
    except (ImportError, OSError, RuntimeError, ValueError):
        return False


def available():
    return not _CUDA_FAILED and _cuda_supported()


def _model(name, device):
    from faster_whisper import WhisperModel

    packaged = _MODEL_DIR / name
    checkpoint = str(packaged) if (packaged / 'model.bin').is_file() else name
    return WhisperModel(checkpoint, device=device, compute_type='int8', cpu_threads=4, num_workers=1)


def _transcribe(path, name, device):
    segments, _ = _model(name, device).transcribe(str(path), language="ru", word_timestamps=True, vad_filter=True,
                                                  beam_size=3, condition_on_previous_text=False)
    words, result = [], []
    for segment in segments:
        result.append({"start": float(segment.start), "end": float(segment.end), "text": segment.text})
        words.extend({"word": w.word, "start": float(w.start), "end": float(w.end)} for w in segment.words or ())
    return words, result, " ".join(s["text"] for s in result)


async def _transcribe_child(path, name, device):
    status, stdout, stderr = await gpu_runtime.capture_child(
        [sys.executable, str(Path(__file__).resolve()), str(path), name, device])
    if status:
        detail = stderr.decode('utf-8', errors='replace').strip().splitlines()
        raise RuntimeError(detail[-1][:450] if detail else f'Whisper завершился с кодом {status}')
    return tuple(json.loads(stdout))


async def transcribe(path, name="large-v3-turbo"):
    global _CUDA_FAILED
    async with gpu_runtime.slot:
        if await gpu_runtime.require_safe_temperature() and available():
            try:
                return await _transcribe_child(path, name, 'cuda')
            except (RuntimeError, OSError) as exc:
                await gpu_runtime.require_safe_temperature()
                logger.warning('CUDA speech recognition failed; switching to CPU: %s', exc)
                _CUDA_FAILED = True
        return await _transcribe_child(path, name, 'cpu')


if __name__ == '__main__':
    print(json.dumps(_transcribe(*sys.argv[1:])))
