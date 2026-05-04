"""
Photo region detection for scanned document pages.

Primary pass
────────────
Gaussian blur → Otsu binarisation (inverted) → morphological opening to
erase thin text strokes → morphological closing to fill photo interiors →
contour filtering by area, aspect ratio, and a text-vs-photo discriminator.

Supplementary pass (light halftone)
────────────────────────────────────
Photos printed with a light halftone (high-key images) average to a
medium-gray local mean even when most of their pixels exceed Otsu's
threshold.  A large box-filter captures this mean; pixels in a medium-gray
band (darker than blank paper but lighter than ink) are closed into blobs
and merged with the primary result.

Text discriminator
──────────────────
Text columns produce strongly oscillating row-mean brightness (alternating
dark text lines and white inter-line gaps).  Candidate regions whose
row-mean standard deviation is disproportionately larger than their
column-mean standard deviation are rejected as text.

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


# ── text-vs-photo discriminator ───────────────────────────────────────────────

def _looks_like_text(
    gray: np.ndarray, rx: int, ry: int, rw: int, rh: int
) -> bool:
    """
    Return True when the region looks like a text column rather than a photo.

    Text columns have strongly oscillating row-mean brightness (alternating
    dark text lines and white inter-line gaps); halftone photos produce a
    smoother, more isotropic intensity pattern where row and column variation
    are similar in magnitude.
    """
    y1 = max(0, ry);  y2 = min(gray.shape[0], ry + rh)
    x1 = max(0, rx);  x2 = min(gray.shape[1], rx + rw)
    patch = gray[y1:y2, x1:x2]
    if patch.size < 400:
        return False

    # Dark/medium regions are almost certainly halftone — never reject them.
    if float(patch.mean()) < 168:
        return False

    row_std = float(np.std(patch.mean(axis=1)))
    col_std = float(np.std(patch.mean(axis=0))) + 0.5   # +0.5 avoids /0

    # Reject when horizontal oscillation far exceeds vertical oscillation
    # (the signature of regularly-spaced text lines) AND the oscillation is
    # large enough to rule out a uniform/lightly-printed region.
    return row_std / col_std > 2.5 and row_std > 10


def _region_overlaps(r1: "PhotoRegion", r2: "PhotoRegion") -> bool:
    """True if the two regions overlap by more than half the smaller one's area."""
    ix1 = max(r1.x1, r2.x1);  iy1 = max(r1.y1, r2.y1)
    ix2 = min(r1.x2, r2.x2);  iy2 = min(r1.y2, r2.y2)
    if ix2 <= ix1 or iy2 <= iy1:
        return False
    inter = (ix2 - ix1) * (iy2 - iy1)
    smaller = min(r1.width * r1.height, r2.width * r2.height)
    return inter > 0.5 * smaller


def _find_white_corridor(coverage: np.ndarray, min_width: int) -> int:
    """
    Find the centre of the widest run in *coverage* where every element ≥ 0.90.
    *coverage* is a 1-D array of per-column (or per-row) white-pixel fractions.
    Returns the 0-based index of the corridor centre, or -1 if none qualifies.
    """
    best_center = -1
    best_width  = 0
    run_start   = -1
    n = len(coverage)
    for i in range(n + 1):
        in_run = i < n and coverage[i] >= 0.90
        if in_run:
            if run_start < 0:
                run_start = i
        else:
            if run_start >= 0:
                run_len = i - run_start
                if run_len >= min_width and run_len > best_width:
                    best_width  = run_len
                    best_center = run_start + run_len // 2
                run_start = -1
    return best_center


def _try_corridor_split(
    gray: np.ndarray,
    region: "PhotoRegion",
    white_thr: float,
) -> List["PhotoRegion"]:
    """
    If *region* contains an internal near-white corridor that spans most of its
    height (vertical split → left/right) or width (horizontal split → top/bottom),
    return the two halves.  Otherwise return [region].

    The search is restricted to the middle 60 % of each axis so that white photo
    borders do not trigger spurious splits.
    """
    x1, y1 = region.x1, region.y1
    x2, y2 = region.x2, region.y2
    patch = gray[max(0, y1):min(gray.shape[0], y2),
                 max(0, x1):min(gray.shape[1], x2)]
    rh, rw = patch.shape
    if rh < 40 or rw < 40:
        return [region]

    margin    = 0.20                         # ignore outer 20 % on each side
    min_width = max(5, min(rw, rh) // 25)   # corridor must be at least this wide

    # ── vertical corridor → left / right ─────────────────────────────────────
    c_lo, c_hi = int(rw * margin), int(rw * (1 - margin))
    if c_hi > c_lo + min_width:
        # fraction of pixels ≥ white_thr in each column
        col_cov = (patch[:, c_lo:c_hi] >= white_thr).mean(axis=0)
        cx = _find_white_corridor(col_cov, min_width)
        if cx >= 0:
            split_x = x1 + c_lo + cx
            left  = PhotoRegion(x1, y1, split_x, y2)
            right = PhotoRegion(split_x, y1, x2, y2)
            if left.width >= 20 and right.width >= 20:
                return [left, right]

    # ── horizontal corridor → top / bottom ───────────────────────────────────
    r_lo, r_hi = int(rh * margin), int(rh * (1 - margin))
    if r_hi > r_lo + min_width:
        row_cov = (patch[r_lo:r_hi, :] >= white_thr).mean(axis=1)
        ry = _find_white_corridor(row_cov, min_width)
        if ry >= 0:
            split_y = y1 + r_lo + ry
            top    = PhotoRegion(x1, y1, x2, split_y)
            bottom = PhotoRegion(x1, split_y, x2, y2)
            if top.height >= 20 and bottom.height >= 20:
                return [top, bottom]

    return [region]


def _nms_regions(regions: List["PhotoRegion"]) -> List["PhotoRegion"]:
    """
    Non-maximum suppression: remove any region whose area is more than 60 %
    covered by a larger already-kept region.  This cleans up small sub-blobs
    that the closing step left inside a single larger photo.
    """
    ordered = sorted(regions, key=lambda r: r.width * r.height, reverse=True)
    keep: List[PhotoRegion] = []
    for cand in ordered:
        c_area = cand.width * cand.height
        dominated = False
        for larger in keep:
            ix1 = max(cand.x1, larger.x1);  iy1 = max(cand.y1, larger.y1)
            ix2 = min(cand.x2, larger.x2);  iy2 = min(cand.y2, larger.y2)
            if ix2 > ix1 and iy2 > iy1:
                if (ix2 - ix1) * (iy2 - iy1) > 0.60 * c_area:
                    dominated = True
                    break
        if not dominated:
            keep.append(cand)
    return keep


def _contours_to_regions(
    gray: np.ndarray,
    contours,
    min_area: int,
    img_w: int,
    img_h: int,
    max_mean_gray: float = 255.0,
) -> List["PhotoRegion"]:
    """Apply all geometric and texture filters to a contour list."""
    out: List[PhotoRegion] = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw * ch < min_area:
            continue
        if cw < 20 or ch < 20:
            continue
        if max(cw, ch) > 6 * min(cw, ch):      # 6:1 max aspect ratio
            continue
        if cw > 0.92 * img_w and ch > 0.92 * img_h:
            continue
        if _looks_like_text(gray, x, y, cw, ch):
            continue
        if max_mean_gray < 255:
            y2 = min(gray.shape[0], y + ch)
            x2 = min(gray.shape[1], x + cw)
            patch = gray[max(0, y):y2, max(0, x):x2]
            if patch.size > 0 and float(patch.mean()) > max_mean_gray:
                continue
        out.append(PhotoRegion(x, y, x + cw, y + ch))
    return out


# ── text-masking post-process (called from app after detection) ───────────────

def mask_text_in_regions(
    pil_image: "_PILImage.Image",
    regions: List["PhotoRegion"],
    conf_threshold: int = 10,
    debug: bool = False,
) -> "tuple":
    """
    Build a copy of *pil_image* with all recognised text painted over with the
    local background colour, then return (masked_image, surviving_regions).

    Regions whose entire content is text are dropped from the returned list.
    Regions with surviving photo content are kept at their original coordinates;
    shrinking is handled separately by canvas.shrink_all_to_content() so the
    same algorithm is used for both auto-detection and manual shrink.

    If pytesseract is unavailable the function returns (pil_image, regions, None)
    unchanged so the pipeline degrades gracefully.

    When *debug* is True a third element is returned: a PIL Image with a
    light-green overlay on areas inside each box where no text was found, and
    a red overlay on areas where text was detected.  Otherwise the third
    element is None.
    """
    if not (_TESS and _PIL):
        return pil_image, regions, None

    # Ensure Tesseract is findable on Windows even when not on PATH.
    try:
        import os as _os, shutil as _sh
        if not _sh.which("tesseract"):
            default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
            if _os.path.isfile(default):
                _tess.pytesseract.tesseract_cmd = default
    except Exception:
        pass

    iw, ih = pil_image.width, pil_image.height
    masked_arr = np.array(pil_image.convert("RGB"))
    surviving: List[PhotoRegion] = []
    # debug_data: per-region list of (region, [word_bboxes_in_image_coords])
    debug_data: list = []
    print(f"[PX text-mask] {len(regions)} region(s) to scan  "
          f"(conf_threshold={conf_threshold})")

    for region in regions:
        x1 = max(0, region.x1);  y1 = max(0, region.y1)
        x2 = min(iw, region.x2); y2 = min(ih, region.y2)
        cw, ch = x2 - x1, y2 - y1
        if cw < 10 or ch < 10:
            surviving.append(region)
            continue

        # Grayscale crop for background estimation and Tesseract input.
        crop_gray = np.array(
            _PILImage.fromarray(masked_arr[y1:y2, x1:x2]).convert("L"),
            dtype=np.float32,
        )

        # Background: 90th-percentile of the outer border ring.
        ring = min(10, max(2, min(cw, ch) // 8))
        border = np.concatenate([
            crop_gray[:ring, :].ravel(),  crop_gray[-ring:, :].ravel(),
            crop_gray[:, :ring].ravel(),  crop_gray[:, -ring:].ravel(),
        ])
        bg_level = float(np.percentile(border, 90))
        bg_fill  = int(bg_level)

        try:
            data = _tess.image_to_data(
                _PILImage.fromarray(crop_gray.astype(np.uint8)),
                config="--psm 11",
                output_type=_tess.Output.DICT,
            )
        except Exception:
            surviving.append(region)
            continue

        has_text = False
        words_found = []
        region_word_bboxes: list = []

        def _apply_tess_data(d, ty_offset: int = 0) -> None:
            nonlocal has_text
            pad = 5
            for j in range(len(d["text"])):
                word = str(d["text"][j]).strip()
                if not word:
                    continue
                try:
                    conf = int(d["conf"][j])
                except (ValueError, TypeError):
                    continue
                words_found.append((word, conf))
                if conf < conf_threshold:
                    continue
                raw_tx  = int(d["left"][j])
                raw_ty  = int(d["top"][j]) + ty_offset
                raw_tx2 = raw_tx + int(d["width"][j])
                raw_ty2 = raw_ty + int(d["height"][j])
                tx  = max(0,  raw_tx  - pad)
                ty  = max(0,  raw_ty  - pad)
                tx2 = min(cw, raw_tx2 + pad)
                ty2 = min(ch, raw_ty2 + pad)
                if tx2 > tx and ty2 > ty:
                    masked_arr[y1 + ty : y1 + ty2, x1 + tx : x1 + tx2] = bg_fill
                    region_word_bboxes.append((x1 + tx, y1 + ty, x1 + tx2, y1 + ty2))
                    has_text = True

        _apply_tess_data(data)

        # PSM 11 is conservative on small pure-text crops (e.g. a single year
        # annotation "1940").  If it found no candidates at all, retry with PSM 6
        # which treats the crop as a uniform text block and is more reliable there.
        # We only do this when words_found is completely empty to avoid running
        # PSM 6 on large mixed photo+text regions where it false-detects halftone
        # dots as text and causes over-masking.
        if not words_found:
            try:
                d_fb = _tess.image_to_data(
                    _PILImage.fromarray(crop_gray.astype(np.uint8)),
                    config="--psm 6",
                    output_type=_tess.Output.DICT,
                )
                _apply_tess_data(d_fb)
            except Exception:
                pass

        # Secondary pass: bottom 40 % with block-text mode catches caption lines
        # that score poorly when the full region (photo + text) is analysed together.
        bot_h = ch * 2 // 5
        if bot_h >= 20:
            try:
                d2 = _tess.image_to_data(
                    _PILImage.fromarray(crop_gray[ch - bot_h:].astype(np.uint8)),
                    config="--psm 6",
                    output_type=_tess.Output.DICT,
                )
                _apply_tess_data(d2, ty_offset=ch - bot_h)
            except Exception:
                pass

        print(f"  box ({x1},{y1})-({x2},{y2})  bg={bg_level:.0f}  "
              f"words={len(words_found)}  masked={has_text}  "
              + (f"sample={words_found[:5]}" if words_found else ""))

        debug_data.append(((x1, y1, x2, y2), region_word_bboxes))

        if not has_text:
            surviving.append(region)
            continue

        # Check whether any photo content survives in the now-masked crop.
        after = masked_arr[y1:y2, x1:x2].mean(axis=2).astype(np.float32)
        has_content = (after < bg_level - 20.0).any()
        print(f"    → has_content={has_content}  "
              f"({'kept' if has_content else 'DROPPED — pure text'})")
        if has_content:
            surviving.append(region)

    print(f"[PX text-mask] {len(surviving)}/{len(regions)} region(s) survived")

    debug_image = None
    if debug and _PIL and debug_data:
        from PIL import ImageDraw as _ID
        overlay = _PILImage.new("RGBA", (iw, ih), (0, 0, 0, 0))
        draw = _ID.Draw(overlay)
        for (rx1, ry1, rx2, ry2), word_bboxes in debug_data:
            draw.rectangle([rx1, ry1, rx2, ry2], fill=(0, 200, 0, 70))
            for (wx1, wy1, wx2, wy2) in word_bboxes:
                draw.rectangle([wx1, wy1, wx2, wy2], fill=(220, 0, 0, 120))
        base = _PILImage.fromarray(masked_arr).convert("RGBA")
        debug_image = _PILImage.alpha_composite(base, overlay).convert("RGB")

    return _PILImage.fromarray(masked_arr), surviving, debug_image


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
    bg_level = float(np.percentile(gray.ravel(), 95))   # ≈ blank-paper brightness

    # ── primary pass ──────────────────────────────────────────────────────────

    prog(15, "Smoothing…")
    blurred = cv2.GaussianBlur(gray, (7, 7), 2)

    prog(26, "Binarising…")
    _, binary = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )

    prog(37, "Removing text strokes…")
    # Keep the opening kernel small so sparse halftone dots (1-4 px at 150 dpi)
    # survive.  The _looks_like_text discriminator handles text blobs that
    # also survive.
    stroke_px = max(2, h // 500)
    open_k = np.ones((stroke_px, stroke_px), np.uint8)
    no_text = cv2.morphologyEx(binary, cv2.MORPH_OPEN, open_k)

    prog(48, "Filling photo regions…")
    # Closing kernel sized independently of stroke_px so it stays large enough
    # to bridge the gaps between halftone dots regardless of stroke_px.
    fill_px = max(20, h // 100)
    close_k = np.ones((fill_px, fill_px), np.uint8)
    filled = cv2.morphologyEx(no_text, cv2.MORPH_CLOSE, close_k, iterations=2)

    prog(59, "Finding primary contours…")
    contours1, _ = cv2.findContours(
        filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    regions = _contours_to_regions(gray, contours1, min_area, w, h)

    # ── supplementary pass: light / white-bordered halftone ───────────────────
    # Run as a completely separate pipeline and merge at the region level so
    # that the large blobs it may produce cannot swallow the primary detections.
    #
    # A lightly-printed halftone averages to medium gray over a wide window
    # even when most individual pixels exceed Otsu's threshold.  The band
    # bg−80 … bg−15 catches photos down to ~15 % halftone density; text is
    # screened by _looks_like_text and the max_mean_gray cap.

    prog(65, "Searching for light halftone…")
    blur_w = max(31, h // 50)
    if blur_w % 2 == 0:
        blur_w += 1
    local_mean = cv2.blur(gray, (blur_w, blur_w))
    med_lo     = max(0.0, bg_level - 80)
    # Widen the upper bound: photos with ~15 % halftone density average to
    # roughly bg-20 over a large window; the previous bg-40 cut-off missed them.
    # _looks_like_text and max_mean_gray still screen out text that bleeds in.
    med_hi     = bg_level - 15
    if med_hi > med_lo:
        medium_mask = (
            (local_mean.astype(np.float32) >= med_lo) &
            (local_mean.astype(np.float32) <  med_hi)
        ).astype(np.uint8) * 255
        mg_fill = max(fill_px, h // 40)
        mg_k    = np.ones((mg_fill, mg_fill), np.uint8)
        medium_filled = cv2.morphologyEx(
            medium_mask, cv2.MORPH_CLOSE, mg_k, iterations=2
        )
        contours2, _ = cv2.findContours(
            medium_filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        # Cap raised to match wider band; _looks_like_text handles any text
        # columns that slip through.
        for r in _contours_to_regions(
            gray, contours2, min_area, w, h, max_mean_gray=225.0
        ):
            if not any(_region_overlaps(r, existing) for existing in regions):
                regions.append(r)

    # ── post-processing ───────────────────────────────────────────────────────

    prog(74, "Refining regions…")

    # Pass 1 — split any region that has an internal white corridor:
    # handles two photos joined by the closing step when the gap between them
    # was narrower than the closing radius.
    white_thr = max(228.0, bg_level - 18.0)
    split_out: List[PhotoRegion] = []
    for r in regions:
        split_out.extend(_try_corridor_split(gray, r, white_thr))

    # Pass 2 — non-maximum suppression: remove small sub-blobs that are mostly
    # inside a larger detected region (halftone fragments left by the pipeline).
    regions = _nms_regions(split_out)

    regions = sorted(regions, key=lambda r: (r.y1, r.x1))

    if _TESS and regions:
        prog(78, "Detecting captions…")
        for i, region in enumerate(regions):
            region.caption = _detect_caption(gray, region)
            pct = 78 + round(18 * (i + 1) / len(regions))
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
