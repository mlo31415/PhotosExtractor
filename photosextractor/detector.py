"""
Photo region detection for scanned document pages.

Strategy: Gaussian blur to dissolve halftone dots, Otsu binarisation,
morphological opening to erase thin text strokes, morphological closing
to fill photo interiors, then contour filtering by area and aspect ratio.

If pytesseract is available the strip of text immediately below each
detected photo is OCR'd and returned as an initial caption.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    from PIL import Image as _PILImage
    _PIL = True
except ImportError:
    _PIL = False

try:
    import pytesseract as _tess
    _TESS = True
except ImportError:
    _TESS = False

ProgressFn = Callable[[int, str], None]


@dataclass
class PhotoRegion:
    x1: int
    y1: int
    x2: int
    y2: int
    caption: str = ""

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1


# ── image loading ─────────────────────────────────────────────────────────────

def _load_gray(path: str) -> np.ndarray:
    """Load any image format as a grayscale uint8 array."""
    if _CV2:
        raw = np.fromfile(path, dtype=np.uint8)
        arr = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
        if arr is not None:
            return arr
    if _PIL:
        return np.array(_PILImage.open(path).convert("L"))
    raise RuntimeError("Cannot load image — install opencv-python or Pillow.")


# ── caption detection ─────────────────────────────────────────────────────────

def _detect_caption(gray: np.ndarray, region: PhotoRegion) -> str:
    """
    Look for caption text in the strip immediately below a photo.
    Returns the OCR'd text, or "" if nothing plausible is found.
    """
    if not (_TESS and _PIL):
        return ""

    h, w = gray.shape
    x1 = max(0, region.x1)
    x2 = min(w, region.x2)
    y_start = region.y2
    y_end   = min(h, region.y2 + 160)

    if y_end - y_start < 8 or x2 - x1 < 20:
        return ""

    strip = gray[y_start:y_end, x1:x2]

    # Fraction of dark pixels per row (>1 % = "has content")
    row_density = np.mean(strip < 235, axis=1)
    content_rows = np.where(row_density > 0.01)[0]

    if len(content_rows) == 0:
        return ""
    if int(content_rows[0]) > 25:   # caption too far below the photo
        return ""

    # Collect the first unbroken run of content rows
    run_start = int(content_rows[0])
    run_end   = run_start
    for r in content_rows:
        r = int(r)
        if r - run_end > 8:     # gap of more than 8 white rows → new section
            break
        run_end = r

    caption_img = strip[run_start : run_end + 1, :]
    if caption_img.shape[0] < 4:
        return ""

    # Pad slightly so Tesseract does not clip edge characters
    caption_img = np.pad(caption_img, ((4, 4), (6, 6)),
                         mode="constant", constant_values=255)

    try:
        pil_img = _PILImage.fromarray(caption_img)
        text = _tess.image_to_string(pil_img, config="--psm 6")
        return text.strip()
    except Exception:
        return ""


# ── main detection ────────────────────────────────────────────────────────────

def _detect_from_gray(
    gray: np.ndarray,
    min_area_fraction: float = 0.005,
    progress: Optional[ProgressFn] = None,
) -> List[PhotoRegion]:
    """Core detection on a pre-loaded grayscale uint8 array."""
    def prog(pct: int, msg: str) -> None:
        if progress:
            progress(pct, msg)

    h, w = gray.shape
    min_area = max(1_000, int(min_area_fraction * h * w))

    prog(20, "Smoothing…")
    blurred = cv2.GaussianBlur(gray, (7, 7), 2)

    prog(32, "Binarising…")
    _, binary = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )

    prog(46, "Removing text strokes…")
    stroke_px = max(5, h // 200)
    open_k = np.ones((stroke_px, stroke_px), np.uint8)
    no_text = cv2.morphologyEx(binary, cv2.MORPH_OPEN, open_k)

    prog(60, "Filling photo regions…")
    fill_px = stroke_px * 4
    close_k = np.ones((fill_px, fill_px), np.uint8)
    filled = cv2.morphologyEx(no_text, cv2.MORPH_CLOSE, close_k, iterations=2)

    prog(74, "Finding contours…")
    contours, _ = cv2.findContours(
        filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    regions: List[PhotoRegion] = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw * ch < min_area:
            continue
        if cw < 20 or ch < 20:
            continue
        if max(cw, ch) > 8 * min(cw, ch):
            continue
        if cw > 0.92 * w and ch > 0.92 * h:
            continue
        regions.append(PhotoRegion(x, y, x + cw, y + ch))

    regions = sorted(regions, key=lambda r: (r.y1, r.x1))

    if _TESS and regions:
        prog(84, "Detecting captions…")
        for i, region in enumerate(regions):
            region.caption = _detect_caption(gray, region)
            pct = 84 + round(12 * (i + 1) / len(regions))
            prog(pct, f"Detecting captions… ({i + 1}/{len(regions)})")

    prog(100, "Done")
    return regions


def detect_photos(
    image_path: str,
    min_area_fraction: float = 0.005,
    progress: Optional[ProgressFn] = None,
) -> List[PhotoRegion]:
    """
    Return photo bounding boxes for the image at *image_path*, sorted
    top-to-bottom then left-to-right.
    """
    if not _CV2:
        raise RuntimeError(
            "opencv-python is required for detection.\n"
            "Run:  pip install opencv-python"
        )
    if progress:
        progress(5, "Loading image…")
    gray = _load_gray(image_path)
    return _detect_from_gray(gray, min_area_fraction, progress)


def detect_photos_pil(
    pil_image: "_PILImage.Image",
    min_area_fraction: float = 0.005,
    progress: Optional[ProgressFn] = None,
) -> List[PhotoRegion]:
    """
    Return photo bounding boxes for a pre-loaded PIL image.
    Used for PDF pages that have no on-disk path.
    """
    if not _CV2:
        raise RuntimeError(
            "opencv-python is required for detection.\n"
            "Run:  pip install opencv-python"
        )
    gray = np.array(pil_image.convert("L"))
    return _detect_from_gray(gray, min_area_fraction, progress)
