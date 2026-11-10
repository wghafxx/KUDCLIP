"""Local webcam detection and portrait framing for vertical clips."""
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median

MODEL = Path(__file__).parent / 'models' / 'face_detection_yunet_2023mar.onnx'
FACE_HEIGHT = 624
TITLE_SPACE = 466
BANNER_Y = {"top": "1104", "middle": "1104", "bottom": "H-h-72", "above_subs": "max(1104,H-h-515)"}
SAMPLES = 9
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Crop:
    x: int
    y: int
    width: int
    height: int

    def filter(self):
        return f'crop={self.width}:{self.height}:{self.x}:{self.y}'


@dataclass(frozen=True)
class Portrait:
    face: Crop
    screen: Crop
    title_space: int


def _dominant_face(frames):
    """Choose one face recurring at the same screen position, preferring inset webcams."""
    best, best_score = None, None
    minimum = max(2, math.ceil(len(frames) * 0.45))
    for candidates in frames:
        for x, y, width, height, _ in candidates:
            matches = []
            for frame in frames:
                near = [face for face in frame if abs(face[0] - x) <= max(0.025, width * 0.8)
                        and abs(face[1] - y) <= max(0.025, height * 0.8)
                        and width * 0.5 <= face[2] <= width * 2]
                if near:
                    matches.append(min(near, key=lambda face: abs(face[0] - x) + abs(face[1] - y)))
            if len(matches) < minimum:
                continue
            mx, my, mw, mh, confidence = (median(values) for values in zip(*matches))
            inset = mw < 0.24 and mh < 0.40 and (mx < 0.28 or mx > 0.72 or my < 0.22 or my > 0.78)
            score = (inset, len(matches), confidence)
            if best_score is None or score > best_score:
                best, best_score = (mx, my, mw, mh), score
    return best


def _crop_in(bounds, aspect, center, max_height=math.inf):
    x1, y1, x2, y2 = bounds
    left, top = math.ceil(x1 / 2) * 2, math.ceil(y1 / 2) * 2
    right, bottom = math.floor(x2 / 2) * 2, math.floor(y2 / 2) * 2
    height = int(min(bottom - top, (right - left) / aspect, max_height)) // 2 * 2
    width = int(height * aspect) // 2 * 2
    if min(width, height) < 2:
        return None
    x = min(right - width, max(left, round((center[0] - width / 2) / 2) * 2))
    y = min(bottom - height, max(top, round((center[1] - height / 2) / 2) * 2))
    return Crop(x, y, width, height)


def _screen_crop(frame_width, frame_height, camera, aspect):
    center = (frame_width / 2, frame_height / 2)
    centered = _crop_in((0, 0, frame_width, frame_height), aspect, center)
    if centered is None:
        return None
    left, top, right, bottom = camera
    if (centered.x >= right or centered.x + centered.width <= left
            or centered.y >= bottom or centered.y + centered.height <= top):
        return centered
    strips = ((0, 0, left, frame_height), (right, 0, frame_width, frame_height),
              (0, 0, frame_width, top), (0, bottom, frame_width, frame_height))
    choices = [_crop_in(bounds, aspect, center) for bounds in strips]
    # Keep a usable area of gameplay when a camera overlaps the center.
    choices = [crop for crop in choices if crop and crop.height >= centered.height * 0.55]
    return min(choices, key=lambda crop: ((crop.x + crop.width / 2 - center[0]) ** 2
                                          + (crop.y + crop.height / 2 - center[1]) ** 2,
                                          -crop.height), default=None)


def _portrait(face, frame_width, frame_height, title_space):
    x, y, width, height = face
    cx, cy = x * frame_width, y * frame_height
    fw, fh = width * frame_width, height * frame_height
    aspect = 1080 / FACE_HEIGHT
    zoom = max(fh * 1.6, fw * 1.6 / aspect)
    closeup = _crop_in((0, 0, frame_width, frame_height), aspect, (cx, cy + fh * 0.1), zoom)
    # Include the area around the face to keep the original webcam inset out of gameplay.
    camera = (max(0, cx - fw * 1.8), max(0, cy - fh * 1.35),
              min(frame_width, cx + fw * 1.8), min(frame_height, cy + fh * 1.35))
    game = _screen_crop(frame_width, frame_height, camera, 1080 / (1920 - FACE_HEIGHT - title_space))
    return Portrait(closeup, game, title_space) if closeup and game else None


def detect(path, duration, *, title_banner=False):
    try:
        import cv2
    except (ImportError, OSError) as exc:
        logger.warning('Детектор лица недоступен: %s', exc)
        return None
    capture = None
    try:
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            return None
        detector = cv2.FaceDetectorYN.create('onnx', MODEL.read_bytes(), b'', (320, 320), 0.82, 0.3, 64)
        samples = []
        frame_width, frame_height = 0, 0
        for index in range(SAMPLES):
            capture.set(cv2.CAP_PROP_POS_MSEC, duration * (index + 0.5) / SAMPLES * 1000)
            available, frame = capture.read()
            if not available:
                continue
            frame_height, frame_width = frame.shape[:2]
            scale = min(1, 960 / max(frame_width, frame_height))
            if scale < 1:
                frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            height, width = frame.shape[:2]
            detector.setInputSize((width, height))
            _, faces = detector.detect(frame)
            samples.append([] if faces is None else [
                ((float(x) + float(w) / 2) / width, (float(y) + float(h) / 2) / height,
                 float(w) / width, float(h) / height, float(score))
                for x, y, w, h, *_, score in faces if w >= 12 and h >= 12
            ])
        face = _dominant_face(samples)
        if face is None or (face[2] >= 0.28 and face[3] >= 0.58):
            # A full-screen close-up is already the main picture.
            return None
        return _portrait(face, frame_width, frame_height, TITLE_SPACE if title_banner else 0)
    except (OSError, cv2.error) as exc:
        logger.warning('Не удалось определить положение лица: %s', exc)
        return None
    finally:
        if capture is not None:
            capture.release()


def filter_graph(portrait, saturation):
    top = FACE_HEIGHT + portrait.title_space
    bottom = 1920 - top
    pad = f',pad=1080:{top}:0:{portrait.title_space}:color=0x09101D' if portrait.title_space else ''
    return (
        '[0:v]split=2[face][game];'
        f'[face]{portrait.face.filter()},scale=1080:{FACE_HEIGHT}:flags=lanczos,'
        f'eq=saturation={saturation},setsar=1{pad}[portrait];'
        f'[game]{portrait.screen.filter()},scale=1080:{bottom}:flags=lanczos,'
        f'eq=saturation={saturation},setsar=1[screen];'
        '[portrait][screen]vstack=inputs=2,fps=30,setsar=1,format=yuv420p'
    )
