"""
Scrollable image canvas with draggable rectangular photo boxes.

Normal interaction
──────────────────
Click box            → select (amber highlight)
Delete key           → remove selected box
Double-click box     → edit metadata (Caption / Date / Source)
Hover over box       → tooltip after 700 ms
Right-click box      → Delete Box / Edit Metadata…
Right-click canvas   → Add Box Here

Split-line workflow
───────────────────
Shift + drag LMB across a box from one side to the *opposite* side →
    releases a blue line showing the proposed split position.
Right-click the blue line → popup with "Split" or "Delete".
  Split  → replaces the original box with two boxes; the blue line
           disappears and is replaced by the two adjacent green borders.
  Delete → removes the blue line; original box unchanged.

The optimal split position is the whitest column (vertical split) or row
(horizontal split) within the crossing range, chosen from the original image.
"""
from __future__ import annotations

import math
import os
import shutil
from copy import deepcopy
from tkinter import messagebox
from typing import Dict, List, Optional, Tuple

import tkinter as tk

from PIL import Image, ImageTk

from .detector import PhotoRegion
from .metadata import PhotoMeta

# ── visual constants ──────────────────────────────────────────────────────────
BORDER_COLOR        = "#00C800"   # green  — normal box
ACTIVE_BORDER_COLOR = "#FFB800"   # amber  — selected box
BORDER_WIDTH        = 2
ACTIVE_BORDER_WIDTH = 3

HANDLE_FILL        = "#00FF00"
ACTIVE_HANDLE_FILL = "#FFFF00"
HANDLE_BORDER = "#007800"
HANDLE_HALF   = 5
HIT_RADIUS    = 8
MIN_BOX_PX    = 10

# Split-line visuals — same blue for both the live trail and the pending line
SPLIT_COLOR  = "#1E90FF"   # dodger-blue
SPLIT_WIDTH  = 2

# Caption-box visuals
CAPTION_COLOR      = "#1E90FF"   # dashed blue selection box
CAPTION_WIDTH      = 2
MIN_RUBBER_BAND_PX = 20          # minimum canvas pixels each axis to keep a drawn selection

_SHIFT = 0x0001   # Shift modifier bit in event.state

# Curvature / orientation thresholds for split-line validation
_MAX_CURVE     = 0.20   # max perpendicular deviation as fraction of line length
_MIN_AXIS_RATIO = 1.5   # dominant axis must be ≥ 1.5 × cross axis (~34° tolerance)

HANDLE_CURSORS: Dict[str, str] = {
    "TL": "top_left_corner",    "TC": "sb_v_double_arrow",
    "TR": "top_right_corner",
    "ML": "sb_h_double_arrow",                               "MR": "sb_h_double_arrow",
    "BL": "bottom_left_corner", "BC": "sb_v_double_arrow",
    "BR": "bottom_right_corner",
    "move": "fleur",
}


# ── tooltip ───────────────────────────────────────────────────────────────────
class _Tooltip:
    _DELAY_MS = 700

    def __init__(self, canvas: tk.Canvas) -> None:
        self._canvas   = canvas
        self._win:      Optional[tk.Toplevel] = None
        self._after_id: Optional[str]         = None

    def schedule(self, text: str, rx: int, ry: int) -> None:
        self._cancel()
        self._after_id = self._canvas.after(
            self._DELAY_MS, lambda: self._show(text, rx, ry)
        )

    def hide(self) -> None:
        self._cancel()
        self._close()

    def show_now(self, text: str, rx: int, ry: int) -> None:
        """Show immediately without delay; reposition if already visible."""
        self._cancel()
        if self._win is not None and self._win.winfo_exists():
            self._win.wm_geometry(f"+{rx + 14}+{ry + 14}")
        else:
            self._show(text, rx, ry)

    def _show(self, text: str, rx: int, ry: int) -> None:
        self._close()
        self._win = win = tk.Toplevel(self._canvas)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{rx + 14}+{ry + 14}")
        tk.Label(
            win, text=text,
            background="#ffffc0", foreground="#000000",
            relief="solid", borderwidth=1,
            justify=tk.LEFT, padx=6, pady=3,
            font=("TkDefaultFont", 9),
        ).pack()

    def _cancel(self) -> None:
        if self._after_id:
            self._canvas.after_cancel(self._after_id)
            self._after_id = None

    def _close(self) -> None:
        if self._win:
            self._win.destroy()
            self._win = None


# ── data model ────────────────────────────────────────────────────────────────
class PhotoBox:
    """Bounding box in floating-point image-pixel coordinates plus metadata."""

    def __init__(
        self,
        x1: float, y1: float, x2: float, y2: float,
        meta: Optional[PhotoMeta] = None,
    ) -> None:
        self.x1   = float(min(x1, x2))
        self.y1   = float(min(y1, y2))
        self.x2   = float(max(x1, x2))
        self.y2   = float(max(y1, y2))
        self.meta = meta if meta is not None else PhotoMeta()

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    def canvas_rect(self, scale: float) -> Tuple[int, int, int, int]:
        return (
            round(self.x1 * scale), round(self.y1 * scale),
            round(self.x2 * scale), round(self.y2 * scale),
        )

    def handle_positions(self, scale: float) -> Dict[str, Tuple[int, int]]:
        cx1, cy1, cx2, cy2 = self.canvas_rect(scale)
        mx, my = (cx1 + cx2) // 2, (cy1 + cy2) // 2
        return {
            "TL": (cx1, cy1), "TC": (mx, cy1), "TR": (cx2, cy1),
            "ML": (cx1, my),                    "MR": (cx2, my),
            "BL": (cx1, cy2), "BC": (mx, cy2), "BR": (cx2, cy2),
        }

    def apply_drag(
        self, handle: str, dx: float, dy: float,
        img_w: float, img_h: float,
    ) -> None:
        M = MIN_BOX_PX
        if handle == "TL":
            self.x1 = min(self.x1 + dx, self.x2 - M)
            self.y1 = min(self.y1 + dy, self.y2 - M)
        elif handle == "TC":
            self.y1 = min(self.y1 + dy, self.y2 - M)
        elif handle == "TR":
            self.x2 = max(self.x2 + dx, self.x1 + M)
            self.y1 = min(self.y1 + dy, self.y2 - M)
        elif handle == "ML":
            self.x1 = min(self.x1 + dx, self.x2 - M)
        elif handle == "MR":
            self.x2 = max(self.x2 + dx, self.x1 + M)
        elif handle == "BL":
            self.x1 = min(self.x1 + dx, self.x2 - M)
            self.y2 = max(self.y2 + dy, self.y1 + M)
        elif handle == "BC":
            self.y2 = max(self.y2 + dy, self.y1 + M)
        elif handle == "BR":
            self.x2 = max(self.x2 + dx, self.x1 + M)
            self.y2 = max(self.y2 + dy, self.y1 + M)
        elif handle == "move":
            bw = self.x2 - self.x1
            bh = self.y2 - self.y1
            nx1 = max(0.0, min(self.x1 + dx, img_w - bw))
            ny1 = max(0.0, min(self.y1 + dy, img_h - bh))
            self.x1, self.y1 = nx1, ny1
            self.x2, self.y2 = nx1 + bw, ny1 + bh
            return
        self.x1 = max(0.0, min(self.x1, img_w))
        self.y1 = max(0.0, min(self.y1, img_h))
        self.x2 = max(0.0, min(self.x2, img_w))
        self.y2 = max(0.0, min(self.y2, img_h))


class SplitLine:
    """
    A pending split: a blue line drawn across a PhotoBox awaiting user
    confirmation via right-click → Split.
    """
    def __init__(self, box: PhotoBox, orient: str, pos: float) -> None:
        self.box    = box       # the box this line will split
        self.orient = orient    # "vertical" (divides left/right) or "horizontal"
        self.pos    = pos       # split coordinate in image pixels


class CaptionBox:
    """
    A user-drawn text-selection region outside all photo boxes.
    Drag it onto the Caption field in the Photo Information panel to OCR
    that image region and fill the active photo's caption.
    """
    def __init__(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.x1 = float(min(x1, x2))
        self.y1 = float(min(y1, y2))
        self.x2 = float(max(x1, x2))
        self.y2 = float(max(y1, y2))

    def canvas_rect(self, scale: float) -> Tuple[int, int, int, int]:
        return (
            round(self.x1 * scale), round(self.y1 * scale),
            round(self.x2 * scale), round(self.y2 * scale),
        )


_tesseract_warned = False   # show the install dialog at most once per session


def _warn_tesseract_missing() -> None:
    global _tesseract_warned
    if _tesseract_warned:
        return
    _tesseract_warned = True
    messagebox.showwarning(
        "Tesseract OCR Not Installed",
        "The OCR feature requires the Tesseract engine, which was not found.\n\n"
        "To install it on Windows:\n\n"
        "  Option A — Windows Package Manager (winget):\n"
        "    1. Open a Command Prompt or PowerShell window\n"
        "    2. Run:  winget install UB-Mannheim.TesseractOCR\n\n"
        "  Option B — Manual download:\n"
        "    1. Open your browser and go to:\n"
        "       https://github.com/UB-Mannheim/tesseract/wiki\n"
        "    2. Download the installer:\n"
        "       tesseract-ocr-w64-setup-*.exe\n"
        "    3. Run the installer — accept the default options\n\n"
        "After installing, restart PhotosExtractor.",
    )


# ── canvas widget ─────────────────────────────────────────────────────────────
class ImageCanvas(tk.Frame):
    """Scrollable canvas that displays a PIL image with draggable photo boxes."""

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, **kwargs)

        self._pil_image: Optional[Image.Image]        = None
        self._tk_image:  Optional[ImageTk.PhotoImage]  = None
        self._scale:     float                          = 1.0
        self._boxes:     List[PhotoBox]                 = []
        self.__active:   Optional[PhotoBox]             = None   # backing for _active property
        self._on_select_callback                        = None   # callable(box|None)
        self._drag:      Optional[dict]                 = None
        self._splits:       List[SplitLine]                   = []
        # Points accumulated while the user Shift-drags (canvas coords)
        self._split_pts:    Optional[List[Tuple[float, float]]] = None
        self._split_drag:   Optional[dict]                      = None
        self._last_deleted: Optional[PhotoBox]                  = None
        # Caption-box state
        self._caption_boxes: List[CaptionBox]                  = []
        self._rubber_band:   Optional[dict]                    = None   # live during LMB drag on empty canvas
        self._caption_drag:  Optional[dict]                    = None   # dragging a caption box
        self._caption_drop_widget: Optional[tk.Widget]         = None   # drop target for designated caption boxes
        self._on_new_box_callback                               = None   # callable(PhotoBox) fired when a box is added

        self._build_widgets()
        self._bind_events()

    # ── construction ──────────────────────────────────────────────────────────

    def _build_widgets(self) -> None:
        self.canvas = tk.Canvas(self, bg="#404040", highlightthickness=0)
        h_bar = tk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
        v_bar = tk.Scrollbar(self, orient=tk.VERTICAL,   command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=h_bar.set, yscrollcommand=v_bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        v_bar.grid(row=0, column=1, sticky="ns")
        h_bar.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

    def _bind_events(self) -> None:
        c = self.canvas
        c.bind("<ButtonPress-1>",        self._on_press)
        c.bind("<B1-Motion>",            self._on_drag)
        c.bind("<ButtonRelease-1>",      self._on_release)
        c.bind("<Double-ButtonPress-1>", self._on_double_click)
        c.bind("<Motion>",               self._on_hover)
        c.bind("<Leave>",                lambda _e: self._tooltip.hide())
        c.bind("<ButtonPress-3>",        self._on_right_click)
        c.bind("<Delete>",               self._on_delete_key)
        c.bind("<Control-MouseWheel>",   self._on_ctrl_scroll)
        self._tooltip = _Tooltip(c)
        # Safety-net: catch releases that land on other widgets (e.g. info panel)
        self.after(0, self._bind_root_release)

    def _bind_root_release(self) -> None:
        root = self.winfo_toplevel()
        root.bind("<ButtonRelease-1>", self._on_root_release, add=True)

    def _on_root_release(self, event: tk.Event) -> None:
        """Safety-net: clean up caption drag if the canvas missed the release."""
        if self._caption_drag is not None:
            cb       = self._caption_drag["cb"]
            ocr_text = self._caption_drag.get("ocr_text")
            self._caption_drag = None
            self._tooltip.hide()
            self._try_drop_caption(cb, event.x_root, event.y_root, ocr_text)

    # ── selection property (fires callback on change) ────────────────────────

    @property
    def _active(self) -> Optional[PhotoBox]:
        return self.__active

    @_active.setter
    def _active(self, box: Optional[PhotoBox]) -> None:
        if box is self.__active:
            return
        self.__active = box
        if self._on_select_callback is not None:
            self._on_select_callback(box)

    def set_on_select(self, callback) -> None:
        """Register a callable(box_or_None) fired whenever the active box changes."""
        self._on_select_callback = callback

    def set_caption_drop_widget(self, widget: tk.Widget) -> None:
        """Set the widget that designated caption boxes are dropped onto for OCR."""
        self._caption_drop_widget = widget

    def set_on_new_box(self, callback) -> None:
        """Register a callable(PhotoBox) fired whenever a box is manually added."""
        self._on_new_box_callback = callback

    # ── public API ────────────────────────────────────────────────────────────

    def set_image(self, pil_image: Image.Image) -> None:
        self._pil_image = pil_image
        self._boxes.clear()
        self._splits.clear()
        self._caption_boxes.clear()
        self._rubber_band = None
        self._caption_drag = None
        self._active = None
        self._last_deleted = None
        self.update_idletasks()
        self._fit()

    def set_boxes(self, regions: List[PhotoRegion]) -> None:
        self._boxes = [
            PhotoBox(r.x1, r.y1, r.x2, r.y2, PhotoMeta(caption=r.caption))
            for r in regions
        ]
        self._splits.clear()
        self._active = None
        self._last_deleted = None
        self._redraw()

    def clear_boxes(self) -> None:
        self._boxes.clear()
        self._splits.clear()
        self._active = None
        self._last_deleted = None
        self._redraw()

    def get_boxes(self) -> List[PhotoBox]:
        return list(self._boxes)

    def zoom(self, factor: float) -> None:
        self._set_scale(self._scale * factor)

    # ── image display ─────────────────────────────────────────────────────────

    def _fit(self) -> None:
        if self._pil_image is None:
            return
        cw = self.canvas.winfo_width()  or 800
        ch = self.canvas.winfo_height() or 600
        iw, ih = self._pil_image.size
        self._set_scale(min(cw / iw, ch / ih, 1.0))

    def _set_scale(self, scale: float) -> None:
        self._scale = max(0.05, min(scale, 8.0))
        self._refresh()

    def _refresh(self) -> None:
        if self._pil_image is None:
            return
        iw, ih = self._pil_image.size
        nw = max(1, round(iw * self._scale))
        nh = max(1, round(ih * self._scale))
        resized = self._pil_image.resize((nw, nh), Image.LANCZOS)
        self._tk_image = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk_image)
        self.canvas.configure(scrollregion=(0, 0, nw, nh))
        self._redraw()

    # ── drawing ───────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        """Redraw all boxes, caption boxes, split lines, and rubber band."""
        self.canvas.delete("box")
        self.canvas.delete("split")
        self.canvas.delete("caption")
        self.canvas.delete("rubber_band")
        for idx, box in enumerate(self._boxes):
            self._draw_box(box, idx, active=(box is self._active))
        for idx, cb in enumerate(self._caption_boxes):
            self._draw_caption_box(cb, idx)
        for idx, split in enumerate(self._splits):
            self._draw_split_line(split, idx)
        if self._rubber_band is not None:
            self._draw_rubber_band()

    def _draw_box(self, box: PhotoBox, idx: int, active: bool) -> None:
        tag   = ("box", f"b{idx}")
        color = ACTIVE_BORDER_COLOR if active else BORDER_COLOR
        width = ACTIVE_BORDER_WIDTH  if active else BORDER_WIDTH
        hfill = ACTIVE_HANDLE_FILL   if active else HANDLE_FILL
        cx1, cy1, cx2, cy2 = box.canvas_rect(self._scale)
        self.canvas.create_rectangle(
            cx1, cy1, cx2, cy2,
            outline=color, width=width, tags=tag,
        )
        for _hname, (hx, hy) in box.handle_positions(self._scale).items():
            self.canvas.create_rectangle(
                hx - HANDLE_HALF, hy - HANDLE_HALF,
                hx + HANDLE_HALF, hy + HANDLE_HALF,
                fill=hfill, outline=HANDLE_BORDER, width=1,
                tags=tag,
            )

    def _draw_split_line(self, split: SplitLine, idx: int) -> None:
        """Draw a pending split as a solid blue line spanning the target box."""
        tag = ("split", f"sp{idx}")
        s   = self._scale
        if split.orient == "vertical":
            cx  = round(split.pos * s)
            cy1 = round(split.box.y1 * s)
            cy2 = round(split.box.y2 * s)
            self.canvas.create_line(
                cx, cy1, cx, cy2,
                fill=SPLIT_COLOR, width=SPLIT_WIDTH, tags=tag,
            )
        else:
            cy  = round(split.pos * s)
            cx1 = round(split.box.x1 * s)
            cx2 = round(split.box.x2 * s)
            self.canvas.create_line(
                cx1, cy, cx2, cy,
                fill=SPLIT_COLOR, width=SPLIT_WIDTH, tags=tag,
            )

    def _draw_split_preview(self) -> None:
        """Draw the in-progress blue trail following the exact mouse path."""
        self.canvas.delete("split_preview")
        pts = self._split_pts
        if pts is None or len(pts) < 2:
            return
        flat = [coord for pt in pts for coord in pt]
        self.canvas.create_line(
            flat,
            fill=SPLIT_COLOR, width=SPLIT_WIDTH,
            tags="split_preview",
        )

    def _draw_caption_box(self, cb: CaptionBox, idx: int) -> None:
        tag = ("caption", f"cap{idx}")
        cx1, cy1, cx2, cy2 = cb.canvas_rect(self._scale)
        self.canvas.create_rectangle(
            cx1, cy1, cx2, cy2,
            outline=CAPTION_COLOR, dash=(4, 4), width=CAPTION_WIDTH, tags=tag,
        )

    def _draw_rubber_band(self) -> None:
        self.canvas.delete("rubber_band")
        rb = self._rubber_band
        if rb is None:
            return
        self.canvas.create_rectangle(
            rb["cx0"], rb["cy0"], rb["cx1"], rb["cy1"],
            outline=CAPTION_COLOR, dash=(4, 4), width=1,
            tags="rubber_band",
        )

    # ── hit testing ───────────────────────────────────────────────────────────

    def _cxy(self, event: tk.Event) -> Tuple[float, float]:
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _hit_split(self, cx: float, cy: float) -> Optional[SplitLine]:
        """Return the first pending split line within HIT_RADIUS of (cx, cy)."""
        s = self._scale
        for split in reversed(self._splits):
            if split.orient == "vertical":
                lx  = split.pos * s
                ly1 = split.box.y1 * s
                ly2 = split.box.y2 * s
                if abs(cx - lx) <= HIT_RADIUS and ly1 - HIT_RADIUS <= cy <= ly2 + HIT_RADIUS:
                    return split
            else:
                ly  = split.pos * s
                lx1 = split.box.x1 * s
                lx2 = split.box.x2 * s
                if abs(cy - ly) <= HIT_RADIUS and lx1 - HIT_RADIUS <= cx <= lx2 + HIT_RADIUS:
                    return split
        return None

    def _hit_box(
        self, cx: float, cy: float
    ) -> Tuple[Optional[PhotoBox], Optional[str]]:
        for box in reversed(self._boxes):
            for hname, (hx, hy) in box.handle_positions(self._scale).items():
                if abs(cx - hx) <= HIT_RADIUS and abs(cy - hy) <= HIT_RADIUS:
                    return box, hname
            bx1, by1, bx2, by2 = box.canvas_rect(self._scale)
            if bx1 <= cx <= bx2 and by1 <= cy <= by2:
                return box, "move"
        return None, None

    def _hit_caption_box(self, cx: float, cy: float) -> Optional[CaptionBox]:
        s = self._scale
        for cb in reversed(self._caption_boxes):
            bx1, by1, bx2, by2 = cb.canvas_rect(s)
            if bx1 <= cx <= bx2 and by1 <= cy <= by2:
                return cb
        return None

    # ── mouse events ──────────────────────────────────────────────────────────

    def _on_press(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        self._last_deleted = None   # any LMB action cancels the undo ghost
        if event.state & _SHIFT:
            self._drag = None
            cx, cy = self._cxy(event)
            self._split_pts = [(cx, cy)]
            self.canvas.config(cursor="crosshair")
            return

        cx, cy = self._cxy(event)

        # LMB on a pending split line → drag it to reposition
        split = self._hit_split(cx, cy)
        if split is not None:
            self._split_drag = {"split": split, "last_cx": cx, "last_cy": cy}
            return

        # LMB on a caption box → start drag (box stays put; drop on Caption field to OCR)
        cb = self._hit_caption_box(cx, cy)
        if cb is not None:
            self._caption_drag = {"cb": cb}
            return

        box, handle = self._hit_box(cx, cy)
        if box is not None:
            # Clicked a photo box — select it
            changed = box is not self._active
            self._active = box
            self._drag = {"box": box, "handle": handle,
                          "last_cx": cx, "last_cy": cy}
            if changed:
                self._redraw()
        else:
            # Empty space: start rubber-band WITHOUT clearing the active photo box.
            # The user may be drawing a text selection to drop on the Caption field,
            # and we need _active to know which photo receives the OCR result.
            self._drag = None
            self._rubber_band = {"cx0": cx, "cy0": cy, "cx1": cx, "cy1": cy}

    def _on_drag(self, event: tk.Event) -> None:
        if self._split_pts is not None:
            cx, cy = self._cxy(event)
            self._split_pts.append((cx, cy))
            self._draw_split_preview()
            return
        if self._caption_drag is not None:
            # OCR exactly once, on first motion
            if "ocr_text" not in self._caption_drag:
                self._caption_drag["ocr_text"] = self._ocr_region(self._caption_drag["cb"])
            self._show_caption_tooltip(event.x_root, event.y_root)
            return
        if self._rubber_band is not None:
            cx, cy = self._cxy(event)
            self._rubber_band["cx1"] = cx
            self._rubber_band["cy1"] = cy
            self._draw_rubber_band()
            return
        if self._split_drag is not None:
            cx, cy = self._cxy(event)
            split = self._split_drag["split"]
            if split.orient == "vertical":
                delta = (cx - self._split_drag["last_cx"]) / self._scale
                lo = split.box.x1 + MIN_BOX_PX
                hi = split.box.x2 - MIN_BOX_PX
                split.pos = max(lo, min(hi, split.pos + delta))
                self._split_drag["last_cx"] = cx
            else:
                delta = (cy - self._split_drag["last_cy"]) / self._scale
                lo = split.box.y1 + MIN_BOX_PX
                hi = split.box.y2 - MIN_BOX_PX
                split.pos = max(lo, min(hi, split.pos + delta))
                self._split_drag["last_cy"] = cy
            self._redraw()
            return
        if not self._drag or self._pil_image is None:
            return
        self._tooltip.hide()
        cx, cy = self._cxy(event)
        dx = (cx - self._drag["last_cx"]) / self._scale
        dy = (cy - self._drag["last_cy"]) / self._scale
        iw, ih = self._pil_image.size
        self._drag["box"].apply_drag(self._drag["handle"], dx, dy, iw, ih)
        self._drag["last_cx"] = cx
        self._drag["last_cy"] = cy
        self._redraw()

    def _on_release(self, event: tk.Event) -> None:
        if self._split_pts is not None:
            cx, cy = self._cxy(event)
            self._split_pts.append((cx, cy))
            self._finish_split()
            return
        if self._caption_drag is not None:
            cb       = self._caption_drag["cb"]
            ocr_text = self._caption_drag.get("ocr_text")
            self._caption_drag = None
            self._tooltip.hide()
            self._try_drop_caption(cb, event.x_root, event.y_root, ocr_text)
            return
        if self._rubber_band is not None:
            rb = self._rubber_band
            self._rubber_band = None
            self.canvas.delete("rubber_band")
            if (abs(rb["cx1"] - rb["cx0"]) >= MIN_RUBBER_BAND_PX and
                    abs(rb["cy1"] - rb["cy0"]) >= MIN_RUBBER_BAND_PX):
                s = self._scale
                self._caption_boxes.append(CaptionBox(
                    rb["cx0"] / s, rb["cy0"] / s,
                    rb["cx1"] / s, rb["cy1"] / s,
                ))
                self._redraw()
            return
        if self._split_drag is not None:
            self._split_drag = None
            return
        self._drag = None

    def _on_double_click(self, event: tk.Event) -> None:
        self._drag = None   # cancel any drag-intent from the second press

    def _on_hover(self, event: tk.Event) -> None:
        if self._drag or self._split_pts is not None:
            self._tooltip.hide()
            return
        if self._split_drag is not None:
            split = self._split_drag["split"]
            cur = "sb_h_double_arrow" if split.orient == "vertical" else "sb_v_double_arrow"
            self.canvas.config(cursor=cur)
            self._tooltip.hide()
            return
        if self._caption_drag is not None:
            self.canvas.config(cursor="fleur")
            self._show_caption_tooltip(event.x_root, event.y_root)
            return
        cx, cy = self._cxy(event)

        # Blue split line — show a directional drag cursor
        split = self._hit_split(cx, cy)
        if split is not None:
            cur = "sb_h_double_arrow" if split.orient == "vertical" else "sb_v_double_arrow"
            self.canvas.config(cursor=cur)
            self._tooltip.hide()
            return

        # Caption box — move cursor
        cb = self._hit_caption_box(cx, cy)
        if cb is not None:
            self.canvas.config(cursor="fleur")
            self._tooltip.hide()
            return

        box, handle = self._hit_box(cx, cy)
        self.canvas.config(
            cursor=HANDLE_CURSORS.get(handle, "") if handle else ""
        )
        if box is None:
            self._tooltip.hide()
            return
        m = box.meta
        if m.caption or m.date or m.source:
            lines: List[str] = []
            if m.caption: lines.append(f"Caption: {m.caption}")
            if m.date:    lines.append(f"Date:    {m.date}")
            if m.source:  lines.append(f"Source:  {m.source}")
            self._tooltip.schedule("\n".join(lines), event.x_root, event.y_root)
        else:
            self._tooltip.schedule(
                "(no metadata — click to select, then edit in the panel)",
                event.x_root, event.y_root,
            )

    def _on_right_click(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        cx, cy = self._cxy(event)

        # ── right-click on a pending split line ──
        split = self._hit_split(cx, cy)
        if split is not None:
            menu = tk.Menu(self, tearoff=0)
            menu.add_command(
                label="Split",
                command=lambda s=split: self._apply_split(s),
            )
            menu.add_command(
                label="Delete",
                command=lambda s=split: self._remove_split(s),
            )
            menu.tk_popup(event.x_root, event.y_root)
            return

        # ── right-click on a caption box ──
        cb = self._hit_caption_box(cx, cy)
        if cb is not None:
            menu = tk.Menu(self, tearoff=0)
            menu.add_command(
                label="Remove Caption Box",
                command=lambda b=cb: self._remove_caption_box(b),
            )
            menu.tk_popup(event.x_root, event.y_root)
            return

        # ── right-click on a box or empty canvas ──
        box, _ = self._hit_box(cx, cy)
        if box is not None and box is not self._active:
            self._active = box
            self._redraw()
        menu = tk.Menu(self, tearoff=0)
        if box is not None:
            menu.add_command(
                label="Delete Box",
                command=lambda b=box: self._delete_box(b),
            )
            menu.add_separator()
        else:
            # Offer undo if click is within the bounds of the last deleted box
            if self._last_deleted is not None:
                ghost = self._last_deleted
                gx1, gy1, gx2, gy2 = ghost.canvas_rect(self._scale)
                if gx1 <= cx <= gx2 and gy1 <= cy <= gy2:
                    menu.add_command(
                        label="Undo Delete Box",
                        command=self._restore_deleted,
                    )
                    menu.add_separator()
        if self._pil_image is not None:
            menu.add_command(
                label="Add Box Here",
                command=lambda x=cx, y=cy: self._add_box_at(x, y),
            )
        if menu.index("end") is not None:
            menu.tk_popup(event.x_root, event.y_root)

    def _on_delete_key(self, event: tk.Event) -> None:
        if self._active is not None:
            self._delete_box(self._active)

    def _on_ctrl_scroll(self, event: tk.Event) -> None:
        self.zoom(1.1 if event.delta > 0 else 0.9)

    # ── box helpers ───────────────────────────────────────────────────────────

    def _delete_box(self, box: PhotoBox) -> None:
        if box in self._boxes:
            self._boxes.remove(box)
            self._last_deleted = box   # available for undo via right-click
        if self._active is box:
            self._active = None
        self._splits = [s for s in self._splits if s.box is not box]
        self._redraw()

    def _restore_deleted(self) -> None:
        """Undo the most recent box deletion (one level only)."""
        if self._last_deleted is None:
            return
        box = self._last_deleted
        self._last_deleted = None
        self._boxes.append(box)
        self._active = box
        self._redraw()

    def _add_box_at(self, cx: float, cy: float) -> None:
        if self._pil_image is None:
            return
        iw, ih = self._pil_image.size
        ix, iy = cx / self._scale, cy / self._scale
        half = min(80, iw // 6, ih // 6)
        box = PhotoBox(
            max(0.0, ix - half), max(0.0, iy - half),
            min(float(iw), ix + half), min(float(ih), iy + half),
        )
        self._last_deleted = None
        self._boxes.append(box)
        if self._on_new_box_callback is not None:
            self._on_new_box_callback(box)
        self._active = box
        self._redraw()

    # ── caption-box helpers ───────────────────────────────────────────────────

    def _show_caption_tooltip(self, x_root: int, y_root: int) -> None:
        """Show (or reposition) the OCR-preview tooltip during a caption drag."""
        text = self._caption_drag.get("ocr_text") if self._caption_drag else None
        if text is None:
            return
        self._tooltip.show_now(text or "(no text recognized)", x_root, y_root)

    def _remove_caption_box(self, cb: CaptionBox) -> None:
        if cb in self._caption_boxes:
            self._caption_boxes.remove(cb)
        self._redraw()

    def _try_drop_caption(
        self,
        cb: CaptionBox,
        x_root: int,
        y_root: int,
        ocr_text: Optional[str] = None,
    ) -> None:
        """
        Called when a caption box is released.  Two drop targets are checked:
        1. Caption field in the Photo Information panel → active photo box.
        2. Any green photo box on the canvas → that specific photo box.
        """
        text = ocr_text if ocr_text is not None else self._ocr_region(cb)

        target: Optional[PhotoBox] = None

        # ── target 1: Caption text widget in the info panel ──────────────────
        if self._caption_drop_widget is not None and self._active is not None:
            w = self._caption_drop_widget
            if (w.winfo_rootx() <= x_root <= w.winfo_rootx() + w.winfo_width() and
                    w.winfo_rooty() <= y_root <= w.winfo_rooty() + w.winfo_height()):
                target = self._active

        # ── target 2: any green photo box on the canvas ───────────────────────
        if target is None:
            x_widget = x_root - self.canvas.winfo_rootx()
            y_widget = y_root - self.canvas.winfo_rooty()
            cx = self.canvas.canvasx(x_widget)
            cy = self.canvas.canvasy(y_widget)
            hit, _ = self._hit_box(cx, cy)
            if hit is not None:
                target = hit

        if target is None:
            return

        target.meta.caption = text
        self._caption_boxes.remove(cb)
        was_active = target is self._active
        self._active = target          # selects the box; fires callback if it changed
        self._redraw()
        if was_active and self._on_select_callback is not None:
            self._on_select_callback(target)   # force panel refresh if already active

    def _ocr_region(self, cb: CaptionBox) -> str:
        """OCR the image pixels inside cb; return stripped text or error message."""
        if self._pil_image is None:
            return ""
        try:
            import pytesseract
            # Common Windows install location as fallback when not on PATH
            if not shutil.which("tesseract"):
                default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
                if os.path.isfile(default):
                    pytesseract.pytesseract.tesseract_cmd = default
            x1 = max(0, round(cb.x1))
            y1 = max(0, round(cb.y1))
            x2 = min(self._pil_image.width,  round(cb.x2))
            y2 = min(self._pil_image.height, round(cb.y2))
            if x2 <= x1 or y2 <= y1:
                return ""
            crop = self._pil_image.crop((x1, y1, x2, y2))
            return pytesseract.image_to_string(crop, config="--psm 6").strip()
        except Exception as exc:
            if "TesseractNotFound" in type(exc).__name__:
                _warn_tesseract_missing()
                return ""
            return f"[OCR error: {exc}]"

    # ── split-line: phase 1 — draw ───────────────────────────────────────────

    def _finish_split(self) -> None:
        """
        Called on mouse-up after a Shift+drag.
        Validates the gesture; if valid, creates a pending SplitLine (blue).
        """
        pts = self._split_pts
        self._split_pts = None
        self.canvas.delete("split_preview")
        self.canvas.config(cursor="")

        if pts is None or len(pts) < 2:
            return

        start_img = (pts[0][0]  / self._scale, pts[0][1]  / self._scale)
        end_img   = (pts[-1][0] / self._scale, pts[-1][1] / self._scale)

        box, orient, r0, r1 = self._split_find_box(start_img, end_img)
        if box is None:
            return
        if not _split_validate(pts, orient):
            return

        pos = self._split_best_position(box, r0, r1, orient)
        self._splits.append(SplitLine(box, orient, pos))
        self._redraw()

    # ── split-line: phase 2 — commit or discard ───────────────────────────────

    def _apply_split(self, split: SplitLine) -> None:
        """Confirm a pending split: replace the target box with two boxes."""
        self._splits = [s for s in self._splits if s is not split]
        box = split.box
        if box not in self._boxes:
            self._redraw()
            return
        # Also discard any other pending splits on the same box (it's gone now)
        self._splits = [s for s in self._splits if s.box is not box]
        self._do_split(box, split.pos, split.orient)

    def _remove_split(self, split: SplitLine) -> None:
        """Discard a pending split without modifying any box."""
        self._splits = [s for s in self._splits if s is not split]
        self._redraw()

    # ── split-line: geometry helpers ──────────────────────────────────────────

    def _split_find_box(
        self,
        start: Tuple[float, float],
        end:   Tuple[float, float],
    ) -> Tuple[Optional[PhotoBox], Optional[str], float, float]:
        """
        Return (box, orientation, range_lo, range_hi) for the first PhotoBox
        whose *opposite* sides are both crossed by the segment start→end.

        orientation "vertical"   → top-to-bottom crossing; range is x at top/bottom edges.
        orientation "horizontal" → left-to-right crossing; range is y at left/right edges.
        Returns (None, None, 0, 0) if no valid crossing found.
        """
        sx, sy = start
        ex, ey = end

        for box in self._boxes:
            bx1, by1, bx2, by2 = box.x1, box.y1, box.x2, box.y2

            # top-to-bottom → vertical dividing line
            if min(sy, ey) < by1 and max(sy, ey) > by2:
                dy = ey - sy
                if abs(dy) > 1e-6:
                    t_top = (by1 - sy) / dy
                    t_bot = (by2 - sy) / dy
                    x_top = sx + t_top * (ex - sx)
                    x_bot = sx + t_bot * (ex - sx)
                    if bx1 <= x_top <= bx2 and bx1 <= x_bot <= bx2:
                        return box, "vertical", x_top, x_bot

            # left-to-right → horizontal dividing line
            if min(sx, ex) < bx1 and max(sx, ex) > bx2:
                dx = ex - sx
                if abs(dx) > 1e-6:
                    t_left  = (bx1 - sx) / dx
                    t_right = (bx2 - sx) / dx
                    y_left  = sy + t_left  * (ey - sy)
                    y_right = sy + t_right * (ey - sy)
                    if by1 <= y_left <= by2 and by1 <= y_right <= by2:
                        return box, "horizontal", y_left, y_right

        return None, None, 0.0, 0.0

    def _split_best_position(
        self,
        box:    PhotoBox,
        r0:     float,
        r1:     float,
        orient: str,
    ) -> float:
        """
        Find the column (vertical) or row (horizontal) within [min(r0,r1), max(r0,r1)]
        that has the highest mean pixel value (most background-like) in the image.
        Falls back to the midpoint when numpy / the image is unavailable.
        """
        mid = (r0 + r1) / 2.0
        if self._pil_image is None:
            return mid
        try:
            import numpy as np
            gray = np.array(self._pil_image.convert("L"), dtype=np.float32)
        except Exception:
            return mid

        ih, iw = gray.shape
        lo = int(round(min(r0, r1)))
        hi = int(round(max(r0, r1)))

        if orient == "vertical":
            lo  = max(lo, int(box.x1))
            hi  = min(hi, int(box.x2))
            by1 = max(0, int(box.y1))
            by2 = min(ih, int(box.y2))
            if lo >= hi or by1 >= by2:
                return mid
            means = gray[by1:by2, lo : hi + 1].mean(axis=0)
            return float(lo + int(np.argmax(means)))
        else:
            lo  = max(lo, int(box.y1))
            hi  = min(hi, int(box.y2))
            bx1 = max(0, int(box.x1))
            bx2 = min(iw, int(box.x2))
            if lo >= hi or bx1 >= bx2:
                return mid
            means = gray[lo : hi + 1, bx1:bx2].mean(axis=1)
            return float(lo + int(np.argmax(means)))

    def _do_split(self, box: PhotoBox, pos: float, orient: str) -> None:
        """
        Remove box and insert two replacement boxes with a gap sized so that
        exactly one screen pixel of background is visible between the borders.
        Clears the undo ghost since this is a substantive action.

        A rectangle border of width W is drawn centred on its coordinate edge,
        occupying W/2 pixels inward and W/2 outward.  For one visible pixel
        between the two inner half-borders:
            gap_canvas ≥ BORDER_WIDTH + 1
        Converting to image pixels at the current scale:
            gap_image  = ceil((BORDER_WIDTH + 1) / scale)
        Half of that is placed on each side of the split coordinate.
        """
        self._last_deleted = None
        half_gap = math.ceil((BORDER_WIDTH + 1) / self._scale) / 2.0
        g = max(0.5, half_gap)   # always at least 1 image pixel total

        if orient == "vertical":
            left  = PhotoBox(box.x1, box.y1, pos - g, box.y2, deepcopy(box.meta))
            right = PhotoBox(pos + g, box.y1, box.x2, box.y2, deepcopy(box.meta))
            halves = [left, right]
        else:
            top    = PhotoBox(box.x1, box.y1, box.x2, pos - g, deepcopy(box.meta))
            bottom = PhotoBox(box.x1, pos + g, box.x2, box.y2, deepcopy(box.meta))
            halves = [top, bottom]

        halves = [b for b in halves
                  if b.width >= MIN_BOX_PX and b.height >= MIN_BOX_PX]
        if not halves:
            return

        idx = self._boxes.index(box)
        self._boxes.pop(idx)
        for offset, half in enumerate(halves):
            self._boxes.insert(idx + offset, half)

        self._active = None
        self._redraw()


# ── module-level helpers ──────────────────────────────────────────────────────

def _split_validate(
    pts:    List[Tuple[float, float]],
    orient: str,
) -> bool:
    """
    Return True only when the drawn polyline is:
      • long enough (≥ 20 canvas pixels start-to-end),
      • straight enough (max perp. deviation ≤ _MAX_CURVE × length),
      • aligned with the expected orientation (dominant axis ≥ _MIN_AXIS_RATIO × other).
    """
    try:
        import numpy as np
    except ImportError:
        return True   # can't validate without numpy — accept anyway

    if len(pts) < 2:
        return False

    arr      = np.array(pts, dtype=float)
    diff     = arr[-1] - arr[0]
    dx, dy   = float(diff[0]), float(diff[1])
    line_len = float(np.hypot(dx, dy))

    if line_len < 20:
        return False

    adx, ady = abs(dx), abs(dy)
    if orient == "vertical":
        if ady < _MIN_AXIS_RATIO * adx:
            return False
    else:
        if adx < _MIN_AXIS_RATIO * ady:
            return False

    ux, uy  = dx / line_len, dy / line_len
    offsets = arr - arr[0]
    perp    = np.abs(offsets[:, 0] * uy - offsets[:, 1] * ux)
    return bool(perp.max() <= _MAX_CURVE * line_len)
