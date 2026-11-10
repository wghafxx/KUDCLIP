"""Serialize GPU work and latch a shutdown on a thermal safety trip."""

import asyncio
import logging
import os
from pathlib import Path
import shutil
import subprocess


logger = logging.getLogger(__name__)
slot = asyncio.Lock()
TEMPERATURE_LIMIT = 70
_WINDOW_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_PORTABLE_FFMPEG = Path(__file__).resolve().parent / 'tools' / 'ffmpeg' / 'ffmpeg.exe'
_trip_reason = None
_temperature_seen = False
_encoders = {}


class ThermalStop(asyncio.CancelledError):
    """Abort the current operation without retrying it on CPU."""


def _temperature():
    executable = shutil.which('nvidia-smi')
    if not executable:
        return None
    try:
        result = subprocess.run([executable, '--id=0', '--query-gpu=temperature.gpu', '--format=csv,noheader,nounits'],
                                capture_output=True, text=True, check=True, timeout=3, creationflags=_WINDOW_FLAGS)
        return int(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


async def require_safe_temperature():
    global _trip_reason, _temperature_seen
    if _trip_reason is not None:
        raise ThermalStop(_trip_reason)
    temperature = await asyncio.to_thread(_temperature)
    if _trip_reason is None and temperature is None and _temperature_seen:
        _trip_reason = 'Потеряна связь с датчиком температуры видеокарты'
    if temperature is not None:
        _temperature_seen = True
        if _trip_reason is None and temperature >= TEMPERATURE_LIMIT:
            _trip_reason = f'Видеокарта достигла {temperature} °C (порог {TEMPERATURE_LIMIT} °C)'
    if _trip_reason is not None:
        raise ThermalStop(_trip_reason)
    return temperature is not None


async def monitor(on_stop, *, interval=1.0):
    while True:
        try:
            await require_safe_temperature()
        except ThermalStop as exc:
            await on_stop(str(exc))
            return
        await asyncio.sleep(interval)


async def _terminate_child(proc):
    if proc.returncode is None:
        if os.name == 'nt':
            # The Windows venv Python launcher can own a separate Python process.
            killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(proc.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=_WINDOW_FLAGS)
            await killer.wait()
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.communicate()


async def capture_child(command, *, timeout=None):
    launch = asyncio.create_task(asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                               stderr=asyncio.subprocess.PIPE, creationflags=_WINDOW_FLAGS))
    try:
        proc = await asyncio.shield(launch)
    except asyncio.CancelledError:
        await _terminate_child(await launch)
        raise
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        await _terminate_child(proc)
        raise
    return proc.returncode, stdout, stderr


async def _nvenc_works(executable):
    if executable in _encoders:
        return _encoders[executable]
    try:
        status, _, _ = await capture_child([executable, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'color=s=320x180:r=30', '-frames:v', '2', '-c:v', 'h264_nvenc', '-f', 'null', '-'], timeout=8)
        _encoders[executable] = status == 0
    except (OSError, asyncio.TimeoutError):
        _encoders[executable] = False
    return _encoders[executable]


def _encoder_candidates():
    return tuple(dict.fromkeys(filter(None, (os.environ.get('KUDCLIP_NVENC_FFMPEG'),
        str(_PORTABLE_FFMPEG) if _PORTABLE_FFMPEG.is_file() else None, shutil.which('ffmpeg')))))


def _nvenc_command(command, executable):
    args = [executable, *command[1:]]
    codec = args.index('-c:v') + 1
    args[codec] = 'h264_nvenc'
    args[args.index('-preset') + 1] = 'p4'
    if '-crf' in args:
        args[args.index('-crf')] = '-cq'
    rate_control = ['-rc', 'vbr']
    if '-b:v' not in args:
        rate_control += ['-b:v', '0']
    args[codec + 1:codec + 1] = rate_control
    return args


async def encode(run, command, timeout):
    async with slot:
        if await require_safe_temperature():
            for executable in _encoder_candidates():
                if not await _nvenc_works(executable):
                    continue
                if not await require_safe_temperature():
                    break
                try:
                    return await run(_nvenc_command(command, executable), timeout=timeout)
                except RuntimeError as exc:
                    logger.warning('NVENC failed; retrying with libx264: %s', exc)
                    break
        await require_safe_temperature()
        return await run([*command[:-1], '-threads:v', '4', command[-1]], timeout=timeout)
