"""Local publication assets. Pillow + FFmpeg only; no model/provider calls."""

import asyncio
import hashlib
import json
import math
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


VERSION = "ready-pack-2"
WIDTH, HEIGHT = 1080, 1920
MAX_DURATION = 65.05
MAX_FILE_BYTES = 45 * 1024 * 1024
_LOCKS = {}


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text(value):
    return " ".join(str(value or "").replace("\x00", "").split())


def _banner_text(title, supplied):
    original = _text(supplied) or _text(title) or "Видео"
    words = original.split()
    # Keep source wording rather than inventing an unsupported promotional claim.
    return " ".join(words[:7]) + ("…" if len(words) > 7 else "")


def _font_file(folder):
    folder = Path(folder)
    for name in ("RussoOne.ttf", "MontserratBlack.ttf", "RubikBlack.ttf"):
        if (folder / name).is_file():
            return folder / name
    fonts = sorted(folder.glob("*.ttf"))
    if not fonts:
        raise RuntimeError("Для готовой обложки нужен локальный TTF-шрифт с кириллицей")
    return fonts[0]


def _wrap(draw, text, font, width):
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textbbox((0, 0), trial, font=font)[2] <= width:
            current = trial
            continue
        if current:
            lines.append(current)
            current = ""
        if draw.textbbox((0, 0), word, font=font)[2] <= width:
            current = word
            continue
        # Very long identifiers still wrap, without clipping or losing letters.
        for character in word:
            if current and draw.textbbox((0, 0), current + character, font=font)[2] > width:
                lines.append(current)
                current = ""
            current += character
    if current:
        lines.append(current)
    return lines


def _title_layout(text, font_path, width=880):
    draw = ImageDraw.Draw(Image.new("RGBA", (WIDTH, HEIGHT)))
    for size in range(78, 15, -2):
        font = ImageFont.truetype(str(font_path), size)
        lines = _wrap(draw, text.upper(), font, width)
        if len(lines) <= 3:
            return font, lines
    raise ValueError("Заголовок слишком длинный для трёх читаемых строк")


def _rgb(value, fallback):
    value = str(value or "")
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    return fallback


def _render_banner(text, profile_name, font_path, output, style=None):
    style = style or {}
    accent = _rgb(style.get("accent"), (34, 221, 235))
    highlight = _rgb(style.get("highlight"), (191, 248, 79))
    panel = _rgb(style.get("panel"), (9, 16, 29))
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    font, lines = _title_layout(text, font_path)
    line_height = int(font.size * 1.14)
    bottom = max(280, 145 + len(lines) * line_height + 28)
    shadow = Image.new("RGBA", image.size)
    ImageDraw.Draw(shadow).rounded_rectangle((42, 80, 1038, bottom + 12), radius=30, fill=(0, 0, 0, 150))
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((48, 72, 1032, bottom), radius=26, fill=(*panel, 241))
    draw.rounded_rectangle((48, 94, 56, bottom - 22), radius=4, fill=(*accent, 255))
    label = _text(profile_name).upper() or "МОМЕНТ ДНЯ"
    label = label[:48]
    label_font = ImageFont.truetype(str(font_path), 25)
    while draw.textlength(label, font=label_font) > 830 and label_font.size > 16:
        label_font = ImageFont.truetype(str(font_path), label_font.size - 1)
    draw.text((94, 100), label, font=label_font, fill=(*accent, 255), anchor="lt")
    draw.rounded_rectangle((952, 99, 984, 107), radius=4, fill=(*highlight, 255))
    for index, line in enumerate(lines):
        draw.text((94, 145 + index * line_height), line, font=font,
                  fill=(*highlight, 255) if index == len(lines) - 1 else (250, 252, 255, 255),
                  stroke_width=1, stroke_fill=(0, 0, 0, 180), anchor="lt")
    image.save(output, "PNG", optimize=True)
    return {"lines": lines, "font_size": font.size, "panel_bottom": bottom}


def _render_cover(frame, banner, output):
    with Image.open(frame) as raw:
        image = raw.convert("RGB")
    image = ImageEnhance.Color(image).enhance(1.12)
    image = ImageEnhance.Contrast(image).enhance(1.06).convert("RGBA")
    # Subtle edge shading keeps the actual game frame visible and colourful.
    shade = Image.new("RGBA", (1, HEIGHT))
    shade.putdata([(4, 10, 22, int(80 * (abs(y / HEIGHT - 0.48) * 1.7) ** 2)) for y in range(HEIGHT)])
    image.alpha_composite(shade.resize(image.size))
    with Image.open(banner) as overlay:
        image.alpha_composite(overlay.convert("RGBA"))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((25, 25, WIDTH - 26, HEIGHT - 26), radius=30,
                           outline=(42, 219, 230, 220), width=3)
    draw.rounded_rectangle((72, HEIGHT - 92, 250, HEIGHT - 82), radius=5, fill=(191, 248, 79, 255))
    image.convert("RGB").save(output, "JPEG", quality=94, subsampling=0, optimize=True)


async def _probe(path, run):
    result = await run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout=60)
    try:
        metadata = json.loads(result)
        streams = metadata["streams"]
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        duration = float(metadata.get("format", {}).get("duration") or video.get("duration") or 0)
        width, height = int(video["width"]), int(video["height"])
    except (ValueError, TypeError, KeyError, StopIteration, OverflowError) as exc:
        raise RuntimeError("Не удалось проверить готовое видео") from exc
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise RuntimeError("Готовое видео имеет недопустимую длительность или размеры")
    return {"duration": duration, "width": width, "height": height,
            "has_audio": any(stream.get("codec_type") == "audio" for stream in streams)}


def _validate_images(cover, banner=None):
    with Image.open(cover) as image:
        if image.format != "JPEG" or image.size != (WIDTH, HEIGHT):
            raise RuntimeError("Неверный формат готовой обложки")
        image.verify()
    if banner is not None:
        with Image.open(banner) as image:
            if image.format != "PNG" or image.size != (WIDTH, HEIGHT) or image.mode != "RGBA":
                raise RuntimeError("Неверный формат прозрачного баннера")
            image.verify()


def _validate_zip(path, include_banner):
    expected = {"video.mp4", "cover.jpg", "caption.txt", "README.txt"}
    if include_banner:
        expected.add("banner.png")
    try:
        with zipfile.ZipFile(path) as archive:
            if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected):
                raise RuntimeError("В комплекте отсутствуют необходимые файлы")
            if any(info.file_size <= 0 for info in archive.infolist()) or archive.testzip() is not None:
                raise RuntimeError("Комплект публикации повреждён")
    except (zipfile.BadZipFile, OSError) as exc:
        raise RuntimeError("Комплект публикации повреждён") from exc


async def _encode(source, banner, output, run, bitrate=None):
    resize = "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,setsar=1,fps=30"
    command = ["ffmpeg", "-y", "-i", str(source)]
    if banner is None:
        command += ["-vf", resize + ",format=yuv420p", "-map", "0:v:0"]
    else:
        filters = (f"[0:v]{resize}[base];[1:v]format=rgba[bar];"
                   "[base][bar]overlay=0:0:format=auto,format=yuv420p[ready]")
        command += ["-i", str(banner), "-filter_complex", filters, "-map", "[ready]"]
    codec = ["-crf", "19", "-maxrate", "5M", "-bufsize", "10M"]
    if bitrate is not None:
        codec = ["-b:v", f"{bitrate}k", "-maxrate", f"{bitrate}k", "-bufsize", f"{bitrate * 2}k"]
    await run([*command, "-map", "0:a:0?",
               "-c:v", "libx264", "-preset", "medium", *codec, "-profile:v", "high", "-level:v", "4.2",
               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)], timeout=900)


async def build_package(video_path, *, title, banner_text, caption, profile_name,
                        out_dir, clip_id, run, fonts_dir, cover_at=None, style=None):
    """Build validated, idempotent local assets from the clean, unmodified clip.

    Outputs are separate ``*_ready.mp4`` / JPG / transparent PNG / text / ZIP.
    A private manifest commits the package only after every artifact validates.
    ``run`` is an awaited subprocess callback; this module makes no API calls.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(clip_id)):
        raise ValueError("Недопустимый идентификатор готового ролика")
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = _LOCKS.setdefault((str(out_dir), str(clip_id)), asyncio.Lock())
    async with lock:
        return await _build(Path(video_path), title, banner_text, caption, profile_name,
                            out_dir, str(clip_id), run, Path(fonts_dir), cover_at, style)


async def _build(source, title, supplied_banner, caption, profile_name, out_dir, clip_id, run, fonts_dir, cover_at=None, style=None):
    names = {"video_filename": f"{clip_id}_ready.mp4", "cover_filename": f"{clip_id}_cover.jpg",
             "package_filename": f"{clip_id}_publication.zip",
             "caption_filename": f"{clip_id}_caption.txt", "readme_filename": f"{clip_id}_README.txt"}
    include_banner = supplied_banner is not None
    if include_banner:
        names["banner_filename"] = f"{clip_id}_banner.png"
    manifest_path = out_dir / f"{clip_id}_ready.json"
    manifest = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    # A caller may pass the returned ready path again. Always go back to the
    # manifest's clean input, never stack a second header over the first.
    if source.resolve() == (out_dir / names["video_filename"]).resolve():
        if not isinstance(manifest, dict) or not manifest.get("source_path"):
            raise ValueError("Для сборки нужен чистый исходный ролик, а не готовая версия с баннером")
        source = Path(manifest["source_path"])
    if not source.is_file():
        raise FileNotFoundError("Чистый исходный ролик для готового комплекта не найден")
    font_path = _font_file(fonts_dir) if include_banner else None
    visible_banner = _banner_text(title, supplied_banner) if include_banner else ""
    publication_caption = str(caption or title or "Видео").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    signature_data = {"version": VERSION, "source_hash": _hash(source), "font_hash": _hash(font_path) if font_path else None,
                      "title": _text(title), "banner_text": visible_banner,
                      "caption": publication_caption, "profile_name": _text(profile_name), 'cover_at': cover_at,
                      **({"style": style} if style else {})}
    signature = hashlib.sha256(json.dumps(signature_data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    result = {**names, "publication_caption": publication_caption, "banner_text": visible_banner,
              "ready_for_publish": True}
    if isinstance(manifest, dict) and manifest.get("signature") == signature:
        try:
            hashes = manifest["files"]
            for name in names.values():
                if _hash(out_dir / name) != hashes[name]:
                    raise RuntimeError("Готовый файл изменился")
            _validate_images(out_dir / names["cover_filename"], out_dir / names["banner_filename"] if include_banner else None)
            _validate_zip(out_dir / names["package_filename"], include_banner)
            metadata = await _probe(out_dir / names["video_filename"], run)
            if metadata["width"] != WIDTH or metadata["height"] != HEIGHT or metadata["duration"] > MAX_DURATION:
                raise RuntimeError("Неверные параметры готового видео")
            if (out_dir / names["video_filename"]).stat().st_size > MAX_FILE_BYTES:
                raise RuntimeError("Готовое видео больше лимита публикации")
            return result
        except (OSError, KeyError, RuntimeError, ValueError, TypeError):
            # Rebuild invalid local artifacts from the preserved clean source.
            pass
    source_info = await _probe(source, run)
    if source_info["duration"] > MAX_DURATION:
        raise ValueError("Готовый ролик должен быть не длиннее 65 секунд вместе с рекламой")
    staging = Path(tempfile.mkdtemp(prefix=f".{clip_id}_pack_", dir=out_dir))
    try:
        banner = staging / names["banner_filename"] if include_banner else None
        cover = staging / names["cover_filename"]
        ready, frame = staging / names["video_filename"], staging / "source_frame.jpg"
        layout = await asyncio.to_thread(_render_banner, visible_banner, profile_name, font_path, banner, style) if banner else None
        if cover_at is not None and (not math.isfinite(float(cover_at)) or float(cover_at) < 0):
            raise ValueError('Некорректный момент для обложки')
        at = max(0.0, min(float(cover_at) if cover_at is not None else source_info["duration"] * 0.4, source_info["duration"] - 0.05))
        await run(["ffmpeg", "-y", "-ss", f"{at:.3f}", "-i", str(source),
                   "-vf", "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,setsar=1",
                   "-frames:v", "1", "-q:v", "2", str(frame)], timeout=120)
        if banner:
            await asyncio.to_thread(_render_cover, frame, banner, cover)
        else:
            frame.replace(cover)
        await _encode(source, banner, ready, run)
        if ready.stat().st_size > MAX_FILE_BYTES:
            # Re-encode the clean source, not the already overlaid result.
            target = min(4500, int((MAX_FILE_BYTES * 0.88 * 8 / source_info["duration"] - 128000) / 1000))
            await _encode(source, banner, ready, run, bitrate=max(400, target))
        ready_info = await _probe(ready, run)
        if (ready_info["width"], ready_info["height"]) != (WIDTH, HEIGHT):
            raise RuntimeError("Готовое видео не соответствует формату 1080×1920")
        if abs(ready_info["duration"] - source_info["duration"]) > 0.35 or ready_info["duration"] > MAX_DURATION:
            raise RuntimeError("Длительность готового видео не совпадает с исходным эпизодом")
        if source_info["has_audio"] and not ready_info["has_audio"]:
            raise RuntimeError("При сборке готового ролика потерялась аудиодорожка")
        if ready.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError("Не удалось уложить готовый ролик в лимит 45 МиБ")
        _validate_images(cover, banner)
        caption_path, readme = staging / names["caption_filename"], staging / names["readme_filename"]
        caption_path.write_text(publication_caption + "\n", encoding="utf-8", newline="\n")
        readme.write_text(
            f"ГОТОВЫЙ КОМПЛЕКТ ДЛЯ ПУБЛИКАЦИИ\n\n{_text(title)}\n\n"
            "video.mp4 — вертикальный ролик, готов к загрузке.\n"
            "cover.jpg — обложка.\n"
            + ("banner.png — баннер, добавленный в видео.\n" if banner else "")
            + "caption.txt — готовая подпись.\n", encoding="utf-8", newline="\n")
        package = staging / names["package_filename"]
        with zipfile.ZipFile(package, "w") as archive:
            media = [(ready, "video.mp4"), (cover, "cover.jpg")]
            if banner:
                media.append((banner, "banner.png"))
            for path, archive_name in media:
                archive.write(path, archive_name, compress_type=zipfile.ZIP_STORED)
            archive.write(caption_path, "caption.txt", compress_type=zipfile.ZIP_DEFLATED)
            archive.write(readme, "README.txt", compress_type=zipfile.ZIP_DEFLATED)
        _validate_zip(package, include_banner)
        manifest_data = {"version": VERSION, "signature": signature, "source_path": str(source.resolve()),
                         "files": {name: _hash(staging / name) for name in names.values()},
                         "layout": layout, "duration": ready_info["duration"], "result": result}
        committed_manifest = staging / manifest_path.name
        committed_manifest.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in names.values():
            (staging / name).replace(out_dir / name)
        committed_manifest.replace(manifest_path)
        return result
    finally:
        # This generated staging directory is the only recursive cleanup target.
        if staging.exists() and staging.resolve().is_relative_to(out_dir) and staging.name.startswith(f".{clip_id}_pack_"):
            shutil.rmtree(staging)
