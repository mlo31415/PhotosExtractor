"""Main application window for PhotosExtractor."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import types
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

from PIL import Image

from .canvas import ImageCanvas
from .detector import detect_photos, detect_photos_pil, mask_text_in_regions

_IMAGE_TYPES = [
    ("Image files", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp *.pgm"),
    ("All files",   "*.*"),
]

_ALL_FILE_TYPES = [
    ("Images and PDFs", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp *.pgm *.pdf"),
    ("Image files",     "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp *.pgm"),
    ("PDF files",       "*.pdf"),
    ("All files",       "*.*"),
]


# ── settings persistence ──────────────────────────────────────────────────────

def _settings_path() -> Path:
    """Settings file lives beside the running script / frozen exe."""
    base = Path(sys.argv[0]).resolve().parent
    new = base / "PhotosExtractor Settings.json"
    old = base / "px_settings.json"
    if not new.exists() and old.exists():
        old.rename(new)   # migrate from old name on first run
    return new

def _load_settings() -> dict:
    try:
        p = _settings_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_settings(data: dict) -> None:
    try:
        _settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── filename / EXIF helpers ───────────────────────────────────────────────────

def _sanitize(text: str) -> str:
    """Convert arbitrary text to a safe filename stem."""
    # Control characters (0x00-0x1F, DEL) plus Windows-illegal chars
    safe = re.sub(r'[\x00-\x1f\x7f\\/:*?"<>|]', "_", text).strip(". ")
    return safe[:251] or "photo"


def _clean_folder_path(raw: str) -> str:
    """Normalise a folder path that may have been pasted with surrounding quotes."""
    p = raw.strip()
    if len(p) >= 2 and p[0] in ('"', "'") and p[-1] == p[0]:
        p = p[1:-1].strip()
    return p


def _ask_conflict(parent: tk.Tk, filename: str) -> tuple:
    """Modal dialog shown when an output file already exists.
    Returns (choice, apply_to_rest) — choice is 'overwrite'|'rename'|'skip'|'cancel'.
    """
    dlg = tk.Toplevel(parent)
    dlg.title("File already exists")
    dlg.resizable(False, False)
    dlg.grab_set()
    dlg.transient(parent)

    tk.Label(dlg, text=f'"{filename}" already exists.', padx=16, pady=12).pack()

    remember_var = tk.BooleanVar(value=False)
    result = ["cancel"]

    def choose(c: str) -> None:
        result[0] = c
        dlg.destroy()

    btn_frame = tk.Frame(dlg)
    btn_frame.pack(padx=16, pady=(0, 4))
    for label, key in (("Overwrite", "overwrite"), ("Rename", "rename"),
                       ("Skip", "skip"), ("Cancel", "cancel")):
        tk.Button(btn_frame, text=label, width=10,
                  command=lambda k=key: choose(k)).pack(side=tk.LEFT, padx=4)

    tk.Checkbutton(dlg, text="Do this for any other photos",
                   variable=remember_var).pack(padx=16, pady=(0, 12))

    dlg.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width()  - dlg.winfo_width())  // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - dlg.winfo_height()) // 2
    dlg.geometry(f"+{x}+{y}")

    parent.wait_window(dlg)
    return result[0], remember_var.get()


def _set_file_creation_time_win32(path: str, timestamp: float) -> None:
    """Set the file creation time on Windows via the SetFileTime API (best-effort)."""
    try:
        import ctypes, ctypes.wintypes
        EPOCH_DIFF       = 116_444_736_000_000_000  # 100-ns ticks: 1601-01-01 → 1970-01-01
        ticks            = int(timestamp * 10_000_000) + EPOCH_DIFF
        ft               = ctypes.wintypes.FILETIME(ticks & 0xFFFF_FFFF, ticks >> 32)
        FILE_WRITE_ATTR  = 0x0100
        FILE_SHARE_READ  = 0x0001
        OPEN_EXISTING    = 3
        FILE_ATTR_NORMAL = 0x0080
        h = ctypes.windll.kernel32.CreateFileW(
            path, FILE_WRITE_ATTR, FILE_SHARE_READ,
            None, OPEN_EXISTING, FILE_ATTR_NORMAL, None,
        )
        INVALID = ctypes.cast(ctypes.c_void_p(-1), ctypes.c_void_p).value
        if h and ctypes.cast(h, ctypes.c_void_p).value != INVALID:
            ctypes.windll.kernel32.SetFileTime(h, ctypes.byref(ft), None, None)
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass


def _save_jpg(img: Image.Image, dest: Path, meta) -> None:
    """Save a cropped PIL image as JPEG with metadata in EXIF and filesystem timestamps."""
    # Parse the date once — used for both EXIF fields and OS timestamps.
    dt = None
    if meta.date:
        try:
            from dateutil import parser as _duparser
            dt = _duparser.parse(meta.date)
        except Exception:
            pass

    try:
        import piexif
        exif: dict = {"0th": {}, "Exif": {}, "GPS": {}}
        if meta.caption:
            exif["0th"][piexif.ImageIFD.ImageDescription] = meta.caption.encode("utf-8")
        if meta.source:
            exif["0th"][piexif.ImageIFD.Artist] = meta.source.encode("utf-8")
        if dt is not None:
            exif_dt = dt.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
            exif["Exif"][piexif.ExifIFD.DateTimeOriginal]  = exif_dt
            exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_dt
        img.save(str(dest), "JPEG", quality=92, exif=piexif.dump(exif))
    except ImportError:
        # piexif unavailable — fall back to JPEG comment marker
        parts = [
            f"Caption: {meta.caption}" if meta.caption else "",
            f"Date: {meta.date}"       if meta.date    else "",
            f"Source: {meta.source}"   if meta.source  else "",
        ]
        comment = "\n".join(p for p in parts if p).encode("utf-8")
        img.save(str(dest), "JPEG", quality=92,
                 comment=comment if comment else None)

    # Stamp the file's modification time (and on Windows, creation time) to match.
    if dt is not None:
        try:
            ts = dt.timestamp()
            os.utime(dest, (ts, ts))
            if sys.platform == "win32":
                _set_file_creation_time_win32(str(dest), ts)
        except Exception:
            pass


# ── application ───────────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PhotosExtractor")
        self.root.minsize(600, 400)

        self._image_path:  str                  = ""
        self._pil_image:   Optional[Image.Image] = None
        self._geo_save_id: Optional[str]         = None
        self._info_box                           = None   # currently displayed PhotoBox
        self._info_panel_loading: bool           = False
        self._debug_text_recognition: bool       = False

        # PDF state
        self._pdf_doc    = None   # fitz.Document when a PDF is open, else None
        self._pdf_path:  str  = ""
        self._pdf_page:  int  = 0   # 0-based current page index
        self._page_dirty: bool = False

        # Track which default values have already been pushed into box metadata
        # so we can propagate edits to boxes that still hold the old default.
        self._applied_default_date:   str           = ""
        self._applied_default_source: str           = ""
        self._apply_defaults_id:      Optional[str] = None

        self._build_menu()
        self._build_toolbar()
        self._build_statusbar()
        self._build_canvas()
        self._restore_geometry()

        self.root.bind("<Control-o>",     lambda _e: self.open_image())
        self.root.bind("<Control-s>",     lambda _e: self.save_selected())
        self.root.bind("<Control-h>",     lambda _e: self.show_help())
        self.root.bind("<F5>",            lambda _e: self.redetect())
        self.root.bind("<Control-equal>", lambda _e: self._canvas.zoom(1.25))
        self.root.bind("<Control-minus>", lambda _e: self._canvas.zoom(0.8))
        self.root.bind("<Control-0>",     lambda _e: self._canvas._fit())
        self.root.bind("<Right>",         lambda _e: self._nav_key("next"))
        self.root.bind("<Left>",          lambda _e: self._nav_key("prev"))
        self.root.bind("<Home>",          lambda _e: self._nav_key("begin"))
        self.root.bind("<End>",           lambda _e: self._nav_key("end"))
        self.root.bind("<Configure>",     self._on_configure)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_dnd()

    # -- construction ---------------------------------------------------------

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)

        fm = tk.Menu(bar, tearoff=0)
        fm.add_command(label="Open Image…",      accelerator="Ctrl+O", command=self.open_image)
        fm.add_separator()
        fm.add_command(label="Save Selected Photo", accelerator="Ctrl+S", command=self.save_selected)
        fm.add_command(label="Save All Photos…",                        command=self.save_all)
        fm.add_separator()
        fm.add_command(label="Exit", command=self._on_close)
        bar.add_cascade(label="File", menu=fm)

        em = tk.Menu(bar, tearoff=0)
        em.add_command(label="Re-detect Photos", accelerator="F5",    command=self.redetect)
        em.add_command(label="Clear All Boxes",                        command=self.clear_boxes)
        bar.add_cascade(label="Edit", menu=em)

        vm = tk.Menu(bar, tearoff=0)
        vm.add_command(label="Zoom In",       accelerator="Ctrl++", command=lambda: self._canvas.zoom(1.25))
        vm.add_command(label="Zoom Out",      accelerator="Ctrl+-", command=lambda: self._canvas.zoom(0.8))
        vm.add_command(label="Fit to Window", accelerator="Ctrl+0", command=self._canvas._fit if False else lambda: self._canvas._fit())
        bar.add_cascade(label="View", menu=vm)

        hm = tk.Menu(bar, tearoff=0)
        hm.add_command(label="Keyboard Shortcuts…", accelerator="Ctrl+H", command=self.show_help)
        bar.add_cascade(label="Help", menu=hm)

        self.root.config(menu=bar)

    def _build_toolbar(self) -> None:
        # StringVars are created here so they exist before _build_info_panel runs.
        # The visible widgets live in the info panel (top of the right-hand column).
        self._default_date_var = tk.StringVar()
        self._default_date_var.trace_add("write", self._on_date_changed)
        self._default_source_var = tk.StringVar()
        self._default_source_var.trace_add("write", self._on_source_changed)
        self._output_folder_var = tk.StringVar()
        self._output_folder_var.trace_add("write", lambda *_: self._schedule_save())

    def _build_canvas(self) -> None:
        mid = tk.Frame(self.root)
        mid.pack(fill=tk.BOTH, expand=True)
        self._build_info_panel(mid)

        left_frame = tk.Frame(mid)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._output_bar = tk.Frame(left_frame)
        self._build_output_bar(self._output_bar)
        self._output_bar.pack(side=tk.TOP, fill=tk.X)

        # Navigation bar (PDF pages) — packed first so pack(before=) works later,
        # but immediately hidden until a PDF is opened.
        self._nav_frame = tk.Frame(left_frame, bd=1, relief=tk.FLAT)
        self._build_nav_bar(self._nav_frame)
        self._nav_frame.pack(side=tk.TOP, fill=tk.X)
        self._nav_frame.pack_forget()

        self._canvas = ImageCanvas(left_frame)
        self._canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._canvas.set_on_select(self._on_box_selected)
        self._canvas.set_on_new_box(self._on_new_box)
        self._canvas.set_on_change(self._on_canvas_change)
        self._canvas.set_caption_drop_widget(self._caption_text)

    def _build_output_bar(self, parent: tk.Widget) -> None:
        tk.Button(
            parent, text="Output Folder:", command=self._choose_output_folder,
        ).pack(side=tk.LEFT, padx=(6, 4), pady=4)
        tk.Entry(parent, textvariable=self._output_folder_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), pady=4,
        )

    def _choose_output_folder(self) -> None:
        current = self._output_folder_var.get().strip()
        folder = filedialog.askdirectory(
            title="Select output folder",
            initialdir=current if current else None,
        )
        if folder:
            self._output_folder_var.set(folder)

    def _build_nav_bar(self, parent: tk.Widget) -> None:
        btn = dict(width=3)
        self._nav_begin = tk.Button(parent, text="|◄", command=self._nav_go_begin, **btn)
        self._nav_prev  = tk.Button(parent, text="◄",  command=self._nav_go_prev,  **btn)
        self._nav_next  = tk.Button(parent, text="►",  command=self._nav_go_next,  **btn)
        self._nav_end   = tk.Button(parent, text="►|", command=self._nav_go_end,   **btn)
        self._nav_page_var   = tk.StringVar()
        self._nav_page_entry = tk.Entry(parent, textvariable=self._nav_page_var,
                                        width=4, justify=tk.CENTER)
        self._nav_page_entry.bind("<Return>",   self._nav_goto_typed)
        self._nav_page_entry.bind("<FocusOut>", self._nav_goto_typed)
        self._nav_total_lbl  = tk.Label(parent, text="of 0")
        self._nav_goto_var   = tk.StringVar()
        self._nav_goto_entry = tk.Entry(parent, textvariable=self._nav_goto_var,
                                        width=4, justify=tk.CENTER)
        self._nav_goto_entry.bind("<Return>", self._nav_do_goto)

        for w in (self._nav_begin, self._nav_prev):
            w.pack(side=tk.LEFT, padx=2, pady=3)
        tk.Label(parent, text="Page:").pack(side=tk.LEFT, padx=(8, 2), pady=3)
        self._nav_page_entry.pack(side=tk.LEFT, pady=3)
        self._nav_total_lbl.pack(side=tk.LEFT, padx=(2, 8), pady=3)
        for w in (self._nav_next, self._nav_end):
            w.pack(side=tk.LEFT, padx=2, pady=3)
        tk.Label(parent, text="Goto:").pack(side=tk.LEFT, padx=(16, 2), pady=3)
        self._nav_goto_entry.pack(side=tk.LEFT, pady=3)

    # -- PDF support ----------------------------------------------------------

    def _close_pdf(self) -> None:
        if self._pdf_doc is not None:
            try:
                self._pdf_doc.close()
            except Exception:
                pass
            self._pdf_doc = None
        self._pdf_path  = ""
        self._pdf_page  = 0
        self._page_dirty = False
        self._nav_frame.pack_forget()

    def _load_pdf(self, path: str) -> None:
        try:
            import fitz
        except ImportError:
            messagebox.showerror(
                "PyMuPDF Required",
                "Opening PDF files requires PyMuPDF.\n\nRun:  pip install pymupdf",
            )
            return
        self._close_pdf()
        try:
            self._pdf_doc = fitz.open(path)
        except Exception as exc:
            messagebox.showerror("Cannot open PDF", str(exc))
            return
        self._pdf_path = path
        self._pdf_page = 0
        self.root.title(f"PhotosExtractor — {Path(path).name}")
        self._nav_frame.pack(side=tk.TOP, fill=tk.X, before=self._canvas)
        self._update_nav_buttons()
        self._render_pdf_page()

    def _render_pdf_page(self) -> None:
        if self._pdf_doc is None:
            return
        try:
            import fitz
            page = self._pdf_doc[self._pdf_page]
            mat  = fitz.Matrix(150 / 72, 150 / 72)
            pix  = page.get_pixmap(matrix=mat, alpha=False)
            pil  = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        except Exception as exc:
            messagebox.showerror("PDF render failed", str(exc))
            return
        n = len(self._pdf_doc)
        self._pil_image = pil
        self._canvas.set_image(pil)
        self.root.title(
            f"PhotosExtractor — {Path(self._pdf_path).name}  "
            f"[Page {self._pdf_page + 1} of {n}]"
        )
        self._set_status(
            f"Page {self._pdf_page + 1} of {n} — detecting photos…"
        )
        self._run_detection_pil(pil)

    def _run_detection_pil(self, pil_image: Image.Image) -> None:
        img   = pil_image
        dbg   = self._debug_text_recognition
        def _worker() -> None:
            try:
                regions = detect_photos_pil(
                    img,
                    progress=lambda _p, msg: self.root.after(
                        0, lambda m=msg: self._set_status(m)
                    ),
                )
                self.root.after(0, lambda: self._set_status(
                    f"Scanning {len(regions)} box(es) for text…"))
                masked_img, regions, dbg_img = mask_text_in_regions(img, regions, debug=dbg)
                self.root.after(0, lambda r=regions, m=masked_img, d=dbg_img: self._on_done(r, m, d))
            except Exception as exc:
                self.root.after(0, lambda e=str(exc): self._on_error(e))
        threading.Thread(target=_worker, daemon=True).start()

    # -- navigation -----------------------------------------------------------

    def _update_nav_buttons(self) -> None:
        if self._pdf_doc is None:
            return
        n = len(self._pdf_doc)
        p = self._pdf_page
        self._nav_begin.config(state=tk.NORMAL if p > 0     else tk.DISABLED)
        self._nav_prev .config(state=tk.NORMAL if p > 0     else tk.DISABLED)
        self._nav_next .config(state=tk.NORMAL if p < n - 1 else tk.DISABLED)
        self._nav_end  .config(state=tk.NORMAL if p < n - 1 else tk.DISABLED)
        self._nav_page_var.set(str(p + 1))
        self._nav_total_lbl.config(text=f"of {n}")

    def _nav_check_dirty(self) -> bool:
        """Return True if safe to navigate (no unsaved changes, or user chose to discard)."""
        if self._pdf_doc is None or not self._page_dirty:
            return True
        if self._all_boxes_saved():
            return True
        return messagebox.askyesno(
            "Unsaved Changes",
            "This page has unsaved changes. Discard them and navigate away?",
            default=messagebox.NO,
        )

    def _all_boxes_saved(self) -> bool:
        boxes = self._canvas.get_boxes()
        return bool(boxes) and all(b.saved for b in boxes)

    def _go_to_page(self, page: int) -> None:
        if self._pdf_doc is None:
            return
        page = max(0, min(page, len(self._pdf_doc) - 1))
        if page == self._pdf_page:
            return
        self._pdf_page   = page
        self._page_dirty = False
        self._update_nav_buttons()
        self._render_pdf_page()

    def _nav_go_begin(self) -> None:
        if self._nav_check_dirty():
            self._go_to_page(0)

    def _nav_go_prev(self) -> None:
        if self._nav_check_dirty():
            self._go_to_page(self._pdf_page - 1)

    def _nav_go_next(self) -> None:
        if self._nav_check_dirty():
            self._go_to_page(self._pdf_page + 1)

    def _nav_go_end(self) -> None:
        if self._nav_check_dirty():
            self._go_to_page(len(self._pdf_doc) - 1)

    def _nav_goto_typed(self, _event=None) -> None:
        if self._pdf_doc is None:
            return
        try:
            p = int(self._nav_page_var.get()) - 1
        except ValueError:
            self._update_nav_buttons()
            return
        p = max(0, min(p, len(self._pdf_doc) - 1))
        if p == self._pdf_page:
            self._update_nav_buttons()
            return
        if not self._nav_check_dirty():
            self._update_nav_buttons()
            return
        self._go_to_page(p)

    def _nav_do_goto(self, _event=None) -> None:
        if self._pdf_doc is None:
            return
        try:
            p = int(self._nav_goto_var.get()) - 1
        except ValueError:
            self._nav_goto_var.set("")
            return
        self._nav_goto_var.set("")
        p = max(0, min(p, len(self._pdf_doc) - 1))
        if p == self._pdf_page:
            return
        if not self._nav_check_dirty():
            return
        self._go_to_page(p)

    def _nav_key(self, direction: str) -> None:
        """Keyboard handler for PDF page navigation (suppressed in text widgets)."""
        if self._pdf_doc is None:
            return
        if isinstance(self.root.focus_get(), (tk.Entry, tk.Text)):
            return
        {"next": self._nav_go_next, "prev": self._nav_go_prev,
         "begin": self._nav_go_begin, "end": self._nav_go_end}[direction]()

    # -- dirty tracking -------------------------------------------------------

    def _mark_page_dirty(self) -> None:
        if self._pdf_doc is not None:
            self._page_dirty = True

    def _on_canvas_change(self) -> None:
        self._mark_page_dirty()

    def _build_info_panel(self, parent: tk.Widget) -> None:
        panel = tk.Frame(parent, width=290, bd=1, relief=tk.GROOVE)
        panel.pack(side=tk.RIGHT, fill=tk.Y)
        panel.pack_propagate(False)

        # ── defaults section (top of panel) ──────────────────────────────────
        tk.Label(panel, text="Default Date:", anchor="w").pack(
            fill=tk.X, padx=10, pady=(8, 0)
        )
        self._date_entry = tk.Entry(panel, textvariable=self._default_date_var)
        self._date_entry.pack(fill=tk.X, padx=10, pady=(2, 6))

        tk.Label(panel, text="Default Source:", anchor="w").pack(fill=tk.X, padx=10)
        tk.Entry(panel, textvariable=self._default_source_var).pack(
            fill=tk.X, padx=10, pady=(2, 8)
        )

        tk.Frame(panel, height=2, bg="black").pack(fill=tk.X, padx=0, pady=(4, 0))

        # ── photo information section ─────────────────────────────────────────
        tk.Label(panel, text="Photo Information",
                 font=("TkDefaultFont", 9, "bold")).pack(
            anchor="w", padx=10, pady=(4, 2)
        )
        tk.Frame(panel, height=1, bg="#aaaaaa").pack(fill=tk.X, padx=6, pady=(0, 6))

        # Caption (multi-line)
        tk.Label(panel, text="Caption:", anchor="w").pack(fill=tk.X, padx=10)
        cap_frame = tk.Frame(panel)
        cap_frame.pack(fill=tk.X, padx=10, pady=(2, 8))
        cap_sb = tk.Scrollbar(cap_frame, orient=tk.VERTICAL)
        self._caption_text = tk.Text(
            cap_frame, height=5, wrap=tk.WORD, font="TkDefaultFont",
            yscrollcommand=cap_sb.set,
        )
        cap_sb.config(command=self._caption_text.yview)
        cap_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._caption_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._caption_text.bind("<<Modified>>", self._on_caption_modified)

        self._caption_warn = tk.Label(
            panel, text="", anchor="w", fg="#cc0000", font=("TkDefaultFont", 8),
        )
        self._caption_warn.pack(fill=tk.X, padx=10, pady=(0, 4))

        # Date/Time
        tk.Label(panel, text="Date/Time:", anchor="w").pack(fill=tk.X, padx=10)
        self._panel_date_var = tk.StringVar()
        self._panel_date_entry = tk.Entry(panel, textvariable=self._panel_date_var)
        self._panel_date_entry.pack(fill=tk.X, padx=10, pady=(2, 8))
        self._panel_date_var.trace_add("write", self._on_panel_date_changed)

        # Source
        tk.Label(panel, text="Source:", anchor="w").pack(fill=tk.X, padx=10)
        self._panel_source_var = tk.StringVar()
        self._panel_source_entry = tk.Entry(panel, textvariable=self._panel_source_var)
        self._panel_source_entry.pack(fill=tk.X, padx=10, pady=(2, 8))
        self._panel_source_var.trace_add("write", self._on_panel_source_changed)

        self._update_info_panel()

    def _build_statusbar(self) -> None:
        self._status = tk.StringVar(value="Open an image to begin  (File › Open Image…)")
        tk.Label(
            self.root, textvariable=self._status,
            bd=1, relief=tk.SUNKEN, anchor=tk.W, padx=6,
        ).pack(fill=tk.X, side=tk.BOTTOM)

    # -- geometry persistence -------------------------------------------------

    def _restore_geometry(self) -> None:
        s = _load_settings()
        try:
            self.root.geometry(s.get("geometry", "1100x800"))
        except Exception:
            self.root.geometry("1100x800")
        self._default_date_var.set(s.get("default_date", ""))
        self._default_source_var.set(s.get("default_source", ""))
        self._output_folder_var.set(s.get("output_folder", ""))
        self._debug_text_recognition = bool(s.get("Debug_Recognized_Text", False))

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        self._schedule_save()

    def _schedule_save(self) -> None:
        if self._geo_save_id:
            self.root.after_cancel(self._geo_save_id)
        self._geo_save_id = self.root.after(500, self._persist_geometry)

    def _persist_geometry(self) -> None:
        s = _load_settings()
        s["geometry"]                = self.root.geometry()
        s["default_date"]            = self._default_date_var.get()
        s["default_source"]          = self._default_source_var.get()
        s["output_folder"]           = self._output_folder_var.get()
        s.setdefault("Debug_Recognized_Text", self._debug_text_recognition)
        _save_settings(s)

    def _on_close(self) -> None:
        if self._pdf_doc is not None and self._page_dirty and not self._all_boxes_saved():
            if not messagebox.askyesno(
                "Unsaved Changes",
                "The current PDF page has unsaved changes. Exit anyway?",
                default=messagebox.NO,
            ):
                return
        self._persist_geometry()
        if self._pdf_doc is not None:
            try:
                self._pdf_doc.close()
            except Exception:
                pass
        self.root.destroy()

    # -- toolbar --------------------------------------------------------------

    def _on_date_changed(self, *_) -> None:
        text = self._default_date_var.get().strip()
        if not text:
            self._date_entry.config(bg="white")
        else:
            try:
                from dateutil import parser as _duparser
                _duparser.parse(text)
                self._date_entry.config(bg="white")
            except Exception:
                self._date_entry.config(bg="#FFB0B0")
        self._schedule_save()
        self._schedule_apply_defaults()

    def _on_source_changed(self, *_) -> None:
        self._schedule_save()
        self._schedule_apply_defaults()

    # -- default propagation --------------------------------------------------

    def _schedule_apply_defaults(self) -> None:
        if self._apply_defaults_id:
            self.root.after_cancel(self._apply_defaults_id)
        self._apply_defaults_id = self.root.after(500, self._propagate_defaults)

    def _propagate_defaults(self) -> None:
        """Push changed defaults into every box whose field is empty or held the old default."""
        new_date   = self._default_date_var.get().strip()
        new_source = self._default_source_var.get().strip()
        old_date   = self._applied_default_date
        old_source = self._applied_default_source

        changed = False
        for box in self._canvas.get_boxes():
            if new_date and new_date != old_date:
                if not box.meta.date or box.meta.date == old_date:
                    box.meta.date = new_date
                    changed = True
            if new_source and new_source != old_source:
                if not box.meta.source or box.meta.source == old_source:
                    box.meta.source = new_source
                    changed = True

        self._applied_default_date   = new_date
        self._applied_default_source = new_source

        if changed and self._info_box is not None:
            self._update_info_panel()

    def _apply_defaults_to_boxes(self, boxes) -> None:
        """Fill empty date/source fields on *boxes* with current defaults."""
        date   = self._default_date_var.get().strip()
        source = self._default_source_var.get().strip()
        for box in boxes:
            if date   and not box.meta.date:
                box.meta.date   = date
            if source and not box.meta.source:
                box.meta.source = source

    def _on_new_box(self, box) -> None:
        """Apply current defaults to a freshly created box."""
        self._apply_defaults_to_boxes([box])

    # -- info panel -----------------------------------------------------------

    def _on_box_selected(self, box) -> None:
        self._info_box = box
        self._update_info_panel()

    def _update_info_panel(self) -> None:
        self._info_panel_loading = True
        try:
            box = self._info_box
            if box is None:
                self._caption_text.config(state=tk.NORMAL)
                self._caption_text.delete("1.0", tk.END)
                self._caption_text.config(state=tk.DISABLED)
                self._panel_date_var.set("")
                self._panel_date_entry.config(state=tk.DISABLED, bg="white")
                self._panel_source_var.set("")
                self._panel_source_entry.config(state=tk.DISABLED)
                self._caption_warn.config(text="")
            else:
                self._caption_text.config(state=tk.NORMAL)
                self._caption_text.delete("1.0", tk.END)
                self._caption_text.insert("1.0", box.meta.caption)
                self._caption_text.edit_modified(False)
                self._panel_date_var.set(box.meta.date)
                self._panel_date_entry.config(state=tk.NORMAL)
                self._panel_source_var.set(box.meta.source)
                self._panel_source_entry.config(state=tk.NORMAL)
                self._update_caption_warn(box.meta.caption)
        finally:
            self._info_panel_loading = False

    _CAPTION_FILENAME_LIMIT = 251  # max stem length: NTFS 255 minus len(".jpg")

    def _on_caption_modified(self, _event=None) -> None:
        if self._info_panel_loading or self._info_box is None:
            self._caption_text.edit_modified(False)
            return
        text = self._caption_text.get("1.0", "end-1c")
        self._info_box.meta.caption = text
        self._caption_text.edit_modified(False)
        self._mark_page_dirty()
        self._update_caption_warn(text)

    def _update_caption_warn(self, caption: str) -> None:
        first = caption.split("\n")[0]
        n = len(first)
        if n > self._CAPTION_FILENAME_LIMIT:
            self._caption_warn.config(
                text=f"First line: {n} chars — filename capped at {self._CAPTION_FILENAME_LIMIT}"
            )
        else:
            self._caption_warn.config(text="")

    def _on_panel_date_changed(self, *_) -> None:
        if self._info_panel_loading:
            return
        text = self._panel_date_var.get().strip()
        if self._info_box is not None:
            self._info_box.meta.date = text
            self._mark_page_dirty()
        if not text:
            self._panel_date_entry.config(bg="white")
        else:
            try:
                from dateutil import parser as _duparser
                _duparser.parse(text)
                self._panel_date_entry.config(bg="white")
            except Exception:
                self._panel_date_entry.config(bg="#FFB0B0")

    def _on_panel_source_changed(self, *_) -> None:
        if self._info_panel_loading or self._info_box is None:
            return
        self._info_box.meta.source = self._panel_source_var.get()
        self._mark_page_dirty()

    def _effective_date(self, meta) -> str:
        """Return meta.date if set; else the default date, defaulting time to 23:59:59."""
        if meta.date:
            return meta.date
        raw = self._default_date_var.get().strip()
        if not raw:
            return ""
        try:
            from dateutil import parser as _duparser
            from datetime import datetime
            # Use 23:59:59 as the default time only when no time was typed
            if not re.search(r'\d:\d\d|\d\s*[aApP][mM]', raw):
                dt = _duparser.parse(raw, default=datetime(1900, 1, 1, 23, 59, 59))
            else:
                dt = _duparser.parse(raw)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    # -- status bar -----------------------------------------------------------

    def _set_status(self, msg: str) -> None:
        self._status.set(msg)
        self.root.update_idletasks()

    def _show_brief_message(self, text: str, ms: int = 1800) -> None:
        """Display a small borderless popup that self-destructs after *ms* milliseconds."""
        win = tk.Toplevel(self.root)
        win.wm_overrideredirect(True)
        win.wm_attributes("-topmost", True)
        tk.Label(
            win, text=text,
            font=("TkDefaultFont", 11),
            bg="#e8f5e9", fg="#1b5e20",
            relief="solid", borderwidth=1,
            padx=20, pady=12,
        ).pack()
        win.update_idletasks()
        # Centre over the main window
        rx = self.root.winfo_rootx() + (self.root.winfo_width()  - win.winfo_width())  // 2
        ry = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 2
        win.wm_geometry(f"+{rx}+{ry}")
        win.after(ms, win.destroy)

    # -- help -----------------------------------------------------------------

    def show_help(self) -> None:
        """Display a non-modal keyboard-shortcuts reference window."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Keyboard Shortcuts")
        dlg.resizable(False, False)
        dlg.transient(self.root)

        sections = [
            ("File", [
                ("Ctrl+O",         "Open image or PDF"),
                ("Ctrl+S",         "Save selected photo  (saves all if none selected)"),
                ("← / →",          "Previous / next page  (PDF)"),
                ("Home / End",     "First / last page  (PDF)"),
            ]),
            ("Zoom", [
                ("Ctrl+Scroll",    "Zoom in / out"),
                ("Ctrl++",         "Zoom in"),
                ("Ctrl+-",         "Zoom out"),
                ("Ctrl+0",         "Fit image to window"),
            ]),
            ("Boxes", [
                ("Drag edge/corner",  "Resize box"),
                ("Drag body",         "Move box"),
                ("Delete",            "Delete selected box"),
                ("Shift+drag",        "Draw split line across box"),
                ("Double-click",      "Edit box metadata"),
            ]),
            ("Right-click menu", [
                ("Add Box Here",      "Create a new box at the cursor"),
                ("Split",             "Split box along the split line"),
                ("Delete Box",        "Delete the box"),
                ("Undo Delete Box",   "Restore last deleted box (ghost region)"),
                ("Shrink to Size",    "Shrink box to fit the photo content"),
            ]),
            ("OCR drag", [
                ("Rubber-band drag",  "Draw a selection; OCR fires when hovering a target"),
                ("Drop on caption",   "Deposit OCR text into the Caption field"),
                ("Drop on photo box", "Deposit OCR text into that box's Caption"),
            ]),
            ("Other", [
                ("F5",               "Re-detect photos on current image"),
                ("Ctrl+H",           "This help window"),
            ]),
        ]

        pad_x, pad_y = 20, 1
        row = 0
        for section, items in sections:
            tk.Label(dlg, text=section, font=("TkDefaultFont", 9, "bold"),
                     anchor="w").grid(row=row, column=0, columnspan=2,
                                      padx=pad_x, pady=(5 if row else 6, 1), sticky="w")
            row += 1
            for key, desc in items:
                tk.Label(dlg, text=key,  anchor="w", width=22,
                         font=("TkFixedFont", 9)).grid(row=row, column=0,
                                                        padx=(pad_x + 8, 4), pady=pad_y, sticky="w")
                tk.Label(dlg, text=desc, anchor="w",
                         font=("TkDefaultFont", 9)).grid(row=row, column=1,
                                                          padx=(4, pad_x), pady=pad_y, sticky="w")
                row += 1

        tk.Button(dlg, text="Close", width=10,
                  command=dlg.destroy).grid(row=row, column=0, columnspan=2, pady=(5, 8))

        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width()  - dlg.winfo_width())  // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

    # -- file / detection actions ---------------------------------------------

    def open_image(self) -> None:
        path = filedialog.askopenfilename(title="Select image or PDF",
                                          filetypes=_ALL_FILE_TYPES)
        if not path:
            return
        if not self._nav_check_dirty():
            return
        if Path(path).suffix.lower() == ".pdf":
            self._load_pdf(path)
        else:
            self._close_pdf()
            self._load_image(path)

    def _load_image(self, path: str) -> None:
        self._close_pdf()
        self._image_path = path
        try:
            pil = Image.open(path)
            self._pil_image = pil
            self._canvas.set_image(pil)
            self.root.title(f"PhotosExtractor — {Path(path).name}")
            self._set_status(f"Loaded {pil.width} × {pil.height} px — detecting photos…")
        except Exception as exc:
            messagebox.showerror("Cannot open image", str(exc))
            return
        self._run_detection(path)

    def _setup_dnd(self) -> None:
        try:
            from tkinterdnd2 import DND_FILES
            # Register every major widget — on Windows each is a separate HWND.
            # _nav_frame is included so drops onto the PDF nav bar also work.
            for w in (self.root, self._canvas, self._canvas.canvas, self._nav_frame):
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    pass
        except (ImportError, AttributeError, tk.TclError):
            pass

    def _on_drop(self, event) -> None:
        data = event.data.strip()
        m = re.match(r'^\{([^}]+)\}|^(\S+)', data)
        if not m:
            return
        path = (m.group(1) or m.group(2)).strip()
        if not self._nav_check_dirty():
            return
        ext = Path(path).suffix.lower()
        if ext == ".pdf":
            self._load_pdf(path)
        elif ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff",
                     ".bmp", ".gif", ".webp", ".pgm"}:
            self._close_pdf()
            self._load_image(path)

    def redetect(self) -> None:
        if self._pdf_doc is not None:
            if self._pil_image is None:
                return
            self._canvas.clear_boxes()
            self._page_dirty = False
            self._set_status(f"Re-detecting photos on page {self._pdf_page + 1}…")
            self._run_detection_pil(self._pil_image)
        elif self._image_path:
            self._canvas.clear_boxes()
            self._set_status("Re-detecting photos…")
            self._run_detection(self._image_path)

    def clear_boxes(self) -> None:
        self._canvas.clear_boxes()
        self._set_status("All boxes cleared.")

    def _run_detection(self, path: str) -> None:
        img = self._pil_image
        dbg = self._debug_text_recognition
        def _worker() -> None:
            try:
                regions = detect_photos(
                    path,
                    progress=lambda _p, msg: self.root.after(
                        0, lambda m=msg: self._set_status(m)
                    ),
                )
                self.root.after(0, lambda: self._set_status(
                    f"Scanning {len(regions)} box(es) for text…"))
                masked_img, regions, dbg_img = mask_text_in_regions(img, regions, debug=dbg)
                self.root.after(0, lambda r=regions, m=masked_img, d=dbg_img: self._on_done(r, m, d))
            except Exception as exc:
                self.root.after(0, lambda e=str(exc): self._on_error(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_done(self, regions, masked_image=None, debug_image=None) -> None:
        # regions and masked_image come pre-processed from the background worker.
        # Boxes are set and shrunk against the text-masked copy; the canvas
        # always displays the original self._pil_image.
        self._canvas.set_boxes(regions)
        self._canvas.shrink_all_to_content(masked_image)
        if debug_image is not None:
            self._show_debug_text_recognition(debug_image)
        boxes = self._canvas.get_boxes()
        self._apply_defaults_to_boxes(boxes)
        self._applied_default_date   = self._default_date_var.get().strip()
        self._applied_default_source = self._default_source_var.get().strip()
        self._page_dirty = False   # freshly detected state is the clean baseline
        n = len(regions)
        noun = "region" if n == 1 else "regions"
        self._set_status(
            f"Found {n} photo {noun}.  "
            "Drag edges/corners to adjust · "
            "Double-click to edit metadata · "
            "Right-click for options."
        )

    def _show_debug_text_recognition(self, debug_image) -> None:
        """Pop up a non-modal window showing the text-recognition debug overlay."""
        from PIL import ImageTk as _ITk
        win = tk.Toplevel(self.root)
        win.title("Debug: Text Recognition  —  green = no text found   red = text found")
        win.transient(self.root)

        # Scale to fit a reasonable screen area
        max_w = min(self.root.winfo_screenwidth()  - 80, 1400)
        max_h = min(self.root.winfo_screenheight() - 80, 900)
        scale = min(max_w / debug_image.width, max_h / debug_image.height, 1.0)
        if scale < 1.0:
            disp = debug_image.resize(
                (round(debug_image.width * scale), round(debug_image.height * scale)),
                Image.LANCZOS,
            )
        else:
            disp = debug_image

        photo = _ITk.PhotoImage(disp)
        lbl = tk.Label(win, image=photo)
        lbl.image = photo
        lbl.pack()
        tk.Button(win, text="Close", command=win.destroy).pack(pady=6)

        win.update_idletasks()
        x = self.root.winfo_rootx() + 40
        y = self.root.winfo_rooty() + 40
        win.geometry(f"+{x}+{y}")

    def _on_error(self, msg: str) -> None:
        messagebox.showerror("Detection failed", msg)
        self._set_status("Detection failed — see error dialog.")

    # -- Save -----------------------------------------------------------------

    def save_all(self) -> None:
        if self._pil_image is None:
            messagebox.showinfo("Save All", "No image is open.")
            return
        boxes = self._canvas.get_boxes()
        if not boxes:
            messagebox.showinfo("Save All", "There are no photo boxes to save.")
            return
        self._do_save(boxes)

    def save_selected(self) -> None:
        if self._pil_image is None:
            messagebox.showinfo("Save", "No image is open.")
            return
        if self._info_box is None:
            self.save_all()
            return
        self._do_save([self._info_box])

    def _do_save(self, boxes: list) -> None:
        out_dir = _clean_folder_path(self._output_folder_var.get())
        if not out_dir:
            out_dir = filedialog.askdirectory(title="Select output folder")
            if not out_dir:
                return
            self._output_folder_var.set(out_dir)
        else:
            self._output_folder_var.set(out_dir)

        out_path = Path(out_dir)
        if not out_path.exists():
            if not messagebox.askyesno(
                "Create folder?",
                f"Output folder does not exist:\n{out_path}\n\nCreate it?",
            ):
                return
            try:
                out_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "Output folder error",
                    f"Could not create output folder:\n{out_path}\n\n{exc}",
                )
                return
        elif not out_path.is_dir():
            messagebox.showerror(
                "Output folder error",
                f"The output path is not a folder:\n{out_path}",
            )
            return

        saved, errors = 0, []
        auto_choice = None  # remembered answer when "Do this for any other photos" is checked

        for i, box in enumerate(boxes, 1):
            region = (
                max(0, round(box.x1)),
                max(0, round(box.y1)),
                min(self._pil_image.width,  round(box.x2)),
                min(self._pil_image.height, round(box.y2)),
            )
            crop = self._pil_image.crop(region).convert("RGB")

            first_line = box.meta.caption.split("\n")[0] if box.meta.caption else ""
            stem = _sanitize(first_line) if first_line else f"photo_{i:03d}"
            dest = out_path / f"{stem}.jpg"

            if dest.exists():
                choice = auto_choice
                if choice is None:
                    choice, remember = _ask_conflict(self.root, dest.name)
                    if remember:
                        auto_choice = choice
                if choice == "cancel":
                    break
                elif choice == "skip":
                    continue
                elif choice == "rename":
                    n = 1
                    while dest.exists():
                        dest = out_path / f"{stem}_{n}.jpg"
                        n += 1
                # "overwrite": dest stays as-is

            eff = types.SimpleNamespace(
                caption=box.meta.caption,
                date=self._effective_date(box.meta),
                source=box.meta.source or self._default_source_var.get().strip(),
            )
            try:
                _save_jpg(crop, dest, eff)
                box.saved = True
                saved += 1
            except Exception as exc:
                errors.append(f"{dest.name}: {exc}")

        if saved:
            self._page_dirty = False
            self._persist_geometry()
        if errors:
            messagebox.showerror(
                "Save — Errors",
                f"Saved {saved} photo(s); {len(errors)} error(s):\n\n"
                + "\n".join(errors),
            )
        elif saved:
            self._show_brief_message(f"{saved} photo{'s' if saved != 1 else ''} saved")
        self._set_status(f"Saved {saved} photo(s) to {out_dir}")
