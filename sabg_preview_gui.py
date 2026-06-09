"""Preview / tuning window for the SABG Analyzer (matplotlib embedded in Tkinter).

Opened from the main GUI's *Preview* button. Flow:

  1. Pick a section thumbnail (left); thumbs are sized proportional to physical size.
  2. Drag a rectangle on it to choose a ROI (capped at gui.preview_roi_cap_um).
  3. *Open ROI* reads that crop at full resolution.
  4. Tune every detection setting live in the right-hand panel (grouped in pipeline
     order); each mask is recomputed in-process via `sabg_analyzer.preview` — the SAME
     mask math as the batch analysis — and drawn over the ROI.
  5. *Export → config.yaml* writes the chosen settings so they propagate to the other
     sections and the batch run.

Heavy CZI reads and the per-change recompute run on a worker thread; results come
back through a queue drained on the Tk main loop, so the window stays responsive.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import replace
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

import cv2
import numpy as np
from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                               NavigationToolbar2Tk)
from matplotlib.figure import Figure
from matplotlib.widgets import RectangleSelector

from sabg_analyzer import overlay, preview
from sabg_analyzer.config import load_config


# ---------------------------------------------------------------------------
# tooltips
# ---------------------------------------------------------------------------
class _Tooltip:
    """A lightweight hover tooltip for any widget."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _evt=None) -> None:
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, wraplength=320,
                 font=("Segoe UI", 8)).pack(ipadx=4, ipady=2)

    def _hide(self, _evt=None) -> None:
        if self._tip:
            self._tip.destroy()
            self._tip = None


# ---------------------------------------------------------------------------
# tuning-panel spec — fields grouped in pipeline order. Each field is
# (section, attr, kind, label, tooltip[, choices]). `section` is the Config sub-
# object name ("" for a top-level Config attr). kind: bool|int|float|choice.
# ---------------------------------------------------------------------------
TISSUE_FIELDS = [
    ("tissue", "gap_level", "int", "gap_level", "max(R,G,B) <= this is an unsampled black mosaic gap (not tissue)."),
    ("tissue", "white_level", "float", "white_level", "Brightness (0-1) above this AND low saturation = white glass."),
    ("tissue", "sat_min", "float", "sat_min", "Saturation below this (with high brightness) = white glass."),
    ("tissue", "adaptive", "bool", "adaptive", "Also remove pale/tinted glass the fixed white test misses."),
    ("tissue", "bg_margin", "float", "bg_margin", "RGB distance (0-1) within which a pixel matches the estimated glass colour."),
    ("tissue", "bg_bright_quantile", "float", "bg_bright_quantile", "Glass colour = median of the brightest this-fraction of pixels."),
    ("tissue", "bg_teal_guard", "float", "bg_teal_guard", "Opponent score above this is kept as tissue (never glass)."),
    ("tissue", "texture_min", "float", "texture_min", "Local std above this = textured = tissue (lower = more sensitive)."),
    ("tissue", "texture_win", "int", "texture_win", "Window (px) for the local-std texture estimate (wider = fewer interior holes)."),
    ("tissue", "close_px", "int", "close_px", "Morphological close to solidify textured tissue (overview px)."),
    ("tissue", "min_object_px", "int", "min_object_px", "Remove tissue specks smaller than this (overview px)."),
    ("tissue", "fill_holes_max_frac", "float", "fill_holes_max_frac", "Fill enclosed non-tissue holes up to this frame fraction (de-speckle)."),
    ("tissue", "fill_interior_holes", "bool", "fill_interior_holes", "Reclaim faint interior tissue dropped as glass (fill enclosed holes unless they look like glass/gap)."),
    ("tissue", "interior_hole_max_frac", "float", "interior_hole_max_frac", "Upper guard: never fill an interior hole bigger than this frame fraction."),
    ("tissue", "bg_max_tissue_frac", "float", "bg_max_tissue_frac", "If adaptive keeps more than this, retry with a stricter glass estimate."),
    ("artifact", "erode_px", "int", "border erode_px", "Erode the tissue border by this many overview px (drops edge halos). [artifact.erode_px]"),
]
ARTIFACT_FIELDS = [
    ("artifact", "enabled", "bool", "enabled", "Flag dark, non-teal fold/debris pixels and exclude them."),
    ("artifact", "dark_level", "float", "dark_level", "max(R,G,B)/255 below this = suspiciously dark."),
    ("artifact", "teal_min", "float", "teal_min", "Opponent score above this = real teal -> keep (not artifact)."),
    ("artifact", "min_object_px", "int", "min_object_px", "Drop dark components smaller than this (keeps tiny specks countable)."),
]
FOLD_FIELDS = [
    ("fold", "enabled", "bool", "enabled", "Detect thin linear ridges (tissue folds)."),
    ("fold", "source", "choice", "source", "density = ridges of tissue OD excess; sabg = ridges of SABG+ density.", ["density", "sabg"]),
    ("fold", "hp_um", "float", "hp_um", "High-pass scale (µm) for the density excess."),
    ("fold", "border_um", "float", "border_um", "Interior margin (µm) excluded from fold finding."),
    ("fold", "combine", "choice", "combine", "How ridge + coherence responses are combined.", ["product", "agreement", "union", "frangi_only"]),
    ("fold", "smooth_um", "float", "smooth_um", "Smoothing (µm) before ridge detection."),
    ("fold", "min_length_um", "float", "min_length_um", "Minimum ridge length (µm)."),
    ("fold", "max_width_um", "float", "max_width_um", "Maximum ridge width (µm)."),
    ("fold", "min_aspect", "float", "min_aspect", "Minimum length/width aspect ratio."),
    ("fold", "ecc_min", "float", "ecc_min", "Minimum eccentricity (elongation) of a fold component."),
    ("fold", "band_width_um", "float", "band_width_um", "Dilate detected ridges into a band this wide (µm)."),
    ("fold", "ridge_min", "float", "ridge_min", "Minimum ridge-filter response."),
    ("fold", "coherence_min", "float", "coherence_min", "Minimum structure-tensor coherence."),
    ("fold", "score_min", "float", "score_min", "Minimum combined score."),
    ("fold", "exclude_from_tissue", "bool", "exclude_from_tissue", "Exclude the fold band from countable tissue (denominator)."),
]
DETECT_FIELDS = [
    ("detection", "primary", "choice", "primary", "Primary SABG score.", ["deconvolution", "opponent"]),
    ("detection", "require_agreement", "bool", "require_agreement", "SABG+ only where BOTH scores clear their thresholds."),
    ("threshold", "method", "choice", "threshold.method", "Auto-threshold method on the tissue histogram.", ["triangle", "otsu", "percentile", "fixed"]),
    ("threshold", "scale", "float", "threshold.scale", "Seed/high threshold = auto-threshold x this (raise to be stricter)."),
    ("threshold", "percentile", "float", "threshold.percentile", "Percentile (when method=percentile)."),
    ("threshold", "min_score", "float", "threshold.min_score", "Clamp the threshold to at least this."),
    ("detection", "hysteresis", "bool", "hysteresis", "Grow each seed into the connected faint teal around it."),
    ("detection", "hyst_low_scale", "float", "hyst_low_scale", "Grow/low threshold = seed threshold x this (lower = grow further)."),
    ("detection", "hyst_teal_min", "float", "hyst_teal_min", "Only grow into pixels at least this teal (opponent)."),
    ("detection", "expand_px", "int", "expand_px", "Dilate the final positive mask by this many px."),
    ("detection", "expand_teal_min", "float", "expand_teal_min", "Only expand into pixels this teal (0 = any tissue)."),
    ("detection", "auto_estimate", "bool", "auto_estimate", "Estimate the SABG stain direction from this ROI's most-teal tissue."),
]
EDGE_FIELDS = [
    ("edge", "enabled", "bool", "enabled", "Reject thin edge-shadow rims from positives."),
    ("edge", "morph_open", "bool", "morph_open", "Drop structures thinner than min_width_um by morphological opening."),
    ("edge", "min_width_um", "float", "min_width_um", "Minimum positive structure width (µm)."),
    ("edge", "reject_shadow", "bool", "reject_shadow", "Reject dark + achromatic shadow pixels."),
    ("edge", "shadow_dark_level", "float", "shadow_dark_level", "Brightness below this counts as shadow-dark."),
    ("edge", "shadow_sat_min", "float", "shadow_sat_min", "Saturation below this counts as achromatic."),
    ("edge", "teal_keep", "float", "teal_keep", "Protect clearly-teal pixels (opponent >= this) from edge rejection."),
]
OVERLAY_FIELDS = [   # redraw-only (no recompute)
    ("overlay", "sabg_alpha", "float", "sabg_alpha", "SABG+ overlay opacity."),
    ("overlay", "artifact_alpha", "float", "artifact_alpha", "Artifact overlay opacity."),
    ("overlay", "fold_alpha", "float", "fold_alpha", "Fold-band overlay opacity."),
    ("overlay", "edge_alpha", "float", "edge_alpha", "Edge-rejected overlay opacity."),
    ("overlay", "nontissue_alpha", "float", "nontissue_alpha", "Non-tissue shade opacity."),
]

GROUPS = [
    ("1. Tissue", TISSUE_FIELDS, True),
    ("2. Artifact / dark folds", ARTIFACT_FIELDS, True),
    ("3. Fold (linear ridges)", FOLD_FIELDS, True),
    ("4. SABG detection", DETECT_FIELDS, True),
    ("5. Edge-shadow rejection", EDGE_FIELDS, True),
    ("6. Overlay appearance", OVERLAY_FIELDS, False),   # redraw only
]

# layers drawn in pipeline order: (key, overlay-color attr, default show)
LAYER_SPEC = [
    ("nontissue", "nontissue_color", True),
    ("artifact", "artifact_color", True),
    ("fold", "fold_color", True),
    ("sabg", "sabg_color", True),
    ("edge_removed", "edge_color", True),
]


class PreviewWindow(tk.Toplevel):
    def __init__(self, master, data_dir: str, out_dir: str, config_path: str) -> None:
        super().__init__(master)
        self.title("SABG Preview / Tune")
        self.geometry("1280x820")
        self.minsize(1040, 640)

        self.data_dir = data_dir
        self.out_dir = out_dir
        self.config_path = Path(config_path)
        self.cfg = load_config(str(config_path) if self.config_path.exists() else None)

        self.q: queue.Queue = queue.Queue()
        self._busy = False
        self._recompute_pending = False
        self._recompute_job = None

        self.entry: preview.SectionEntry | None = None     # selected section
        self.disp_rgb: np.ndarray | None = None            # thumb/overview shown for ROI draw
        self.roi_rgb: np.ndarray | None = None             # the opened full-res ROI
        self.roi_px_um: float | None = None
        self.roi_rect: tuple[int, int, int, int] | None = None   # full-res (x,y,w,h)
        self.layers: dict | None = None
        self._sel_extents = None                           # last rectangle (thumb px)

        self.manual_auto = tk.BooleanVar(value=True)       # auto threshold on ROI
        self.manual_thr = tk.StringVar(value="")
        self.hi_res = tk.BooleanVar(value=self.cfg.gui.preview_hi_res)
        self.show_vars: dict[str, tk.BooleanVar] = {}
        self.field_vars: dict[tuple[str, str], tk.Variable] = {}
        self._photo_refs: list[tk.PhotoImage] = []         # keep picker thumbs alive

        self._build_layout()
        self._populate_picker()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_queue)

    # -- layout ------------------------------------------------------------
    def _build_layout(self) -> None:
        # left: picker (scrollable) | center: figure | right: tuning (scrollable)
        left = tk.Frame(self, width=180)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="Sections", font=("Segoe UI", 9, "bold")).pack(pady=(6, 2))
        self.pick_canvas = tk.Canvas(left, width=176, highlightthickness=0)
        psb = tk.Scrollbar(left, orient="vertical", command=self.pick_canvas.yview)
        self.pick_inner = tk.Frame(self.pick_canvas)
        self.pick_inner.bind("<Configure>", lambda e: self.pick_canvas.configure(
            scrollregion=self.pick_canvas.bbox("all")))
        self.pick_canvas.create_window((0, 0), window=self.pick_inner, anchor="nw")
        self.pick_canvas.configure(yscrollcommand=psb.set)
        self.pick_canvas.pack(side="left", fill="both", expand=True)
        psb.pack(side="right", fill="y")

        right = tk.Frame(self, width=340)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self._build_tuning_panel(right)

        center = tk.Frame(self)
        center.pack(side="left", fill="both", expand=True)
        self.fig = Figure(figsize=(6, 6), tight_layout=True)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_axis_off()
        self.canvas = FigureCanvasTkAgg(self.fig, master=center)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, center)
        self.selector = RectangleSelector(
            self.ax, self._on_rect, useblit=True, button=[1],
            minspanx=5, minspany=5, spancoords="pixels", interactive=True)
        self.selector.set_active(False)
        self.status = tk.Label(center, text="Pick a section, then drag a ROI.",
                               anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

    def _build_tuning_panel(self, parent: tk.Frame) -> None:
        # top controls
        top = tk.Frame(parent, padx=6, pady=6)
        top.pack(fill="x")
        tk.Checkbutton(top, text="Hi-res view", variable=self.hi_res,
                       command=self._reload_display).grid(row=0, column=0, sticky="w")
        self.btn_open = tk.Button(top, text="Open ROI", command=self.on_open_roi,
                                  state="disabled")
        self.btn_open.grid(row=0, column=1, padx=4)
        tk.Button(top, text="Recompute", command=self.request_recompute).grid(row=0, column=2, padx=4)
        tk.Button(top, text="Export → config", command=self.on_export).grid(row=0, column=3, padx=4)

        # threshold override
        thr = tk.LabelFrame(parent, text="Seed threshold", padx=6, pady=4)
        thr.pack(fill="x", padx=6, pady=2)
        tk.Checkbutton(thr, text="Auto on ROI", variable=self.manual_auto,
                       command=self._on_manual_toggle).grid(row=0, column=0, sticky="w")
        tk.Label(thr, text="manual:").grid(row=0, column=1, sticky="e")
        self.manual_entry = tk.Entry(thr, textvariable=self.manual_thr, width=10,
                                     state="disabled")
        self.manual_entry.grid(row=0, column=2, sticky="w")
        self.manual_thr.trace_add("write", lambda *_: self._schedule_recompute())

        # layer show toggles
        lay = tk.LabelFrame(parent, text="Layers", padx=6, pady=4)
        lay.pack(fill="x", padx=6, pady=2)
        labels = {"nontissue": "non-tissue", "artifact": "artifact", "fold": "fold",
                  "sabg": "SABG+", "edge_removed": "edge-rejected"}
        for i, (key, _c, default) in enumerate(LAYER_SPEC):
            v = tk.BooleanVar(value=default)
            self.show_vars[key] = v
            tk.Checkbutton(lay, text=labels[key], variable=v,
                           command=self._redraw).grid(row=i // 2, column=i % 2, sticky="w")

        # scrollable settings groups
        canv = tk.Canvas(parent, highlightthickness=0)
        sb = tk.Scrollbar(parent, orient="vertical", command=canv.yview)
        inner = tk.Frame(canv)
        inner.bind("<Configure>", lambda e: canv.configure(scrollregion=canv.bbox("all")))
        canv.create_window((0, 0), window=inner, anchor="nw")
        canv.configure(yscrollcommand=sb.set)
        canv.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=2)
        sb.pack(side="right", fill="y")
        for title, fields, recompute in GROUPS:
            self._build_group(inner, title, fields, recompute)

    def _build_group(self, parent, title, fields, recompute) -> None:
        lf = tk.LabelFrame(parent, text=title, padx=4, pady=2)
        lf.pack(fill="x", expand=True, pady=2)
        for row, spec in enumerate(fields):
            section, attr, kind, label, tip = spec[0], spec[1], spec[2], spec[3], spec[4]
            obj = getattr(self.cfg, section) if section else self.cfg
            cur = getattr(obj, attr)
            lbl = tk.Label(lf, text=label, anchor="w", width=20)
            lbl.grid(row=row, column=0, sticky="w")
            _Tooltip(lbl, tip)
            if kind == "bool":
                var = tk.BooleanVar(value=bool(cur))
                w = tk.Checkbutton(lf, variable=var,
                                   command=lambda s=section, a=attr, v=None, rc=recompute:
                                   self._on_field_change(s, a, "bool", rc))
                self.field_vars[(section, attr)] = var
                w.grid(row=row, column=1, sticky="w")
            elif kind == "choice":
                choices = spec[5]
                var = tk.StringVar(value=str(cur))
                w = ttk.OptionMenu(lf, var, str(cur), *choices,
                                   command=lambda _v, s=section, a=attr, rc=recompute:
                                   self._on_field_change(s, a, "choice", rc))
                self.field_vars[(section, attr)] = var
                w.grid(row=row, column=1, sticky="ew")
            else:  # int / float
                var = tk.StringVar(value=str(cur))
                w = tk.Entry(lf, textvariable=var, width=10)
                var.trace_add("write", lambda *_a, s=section, at=attr, k=kind, rc=recompute:
                              self._on_field_change(s, at, k, rc))
                self.field_vars[(section, attr)] = var
                w.grid(row=row, column=1, sticky="w")
            _Tooltip(w, tip)
        lf.columnconfigure(1, weight=1)

    # -- picker ------------------------------------------------------------
    def _populate_picker(self) -> None:
        try:
            entries = preview.list_sections(self.data_dir, self.out_dir, self.cfg)
        except Exception as exc:
            tk.Label(self.pick_inner, text=f"(error: {exc})", wraplength=160,
                     fg="red").pack()
            return
        have_thumbs = [e for e in entries if e.thumb_path.exists()]
        if not have_thumbs:
            tk.Label(self.pick_inner, text="No thumbnails.\nRun Scan first.",
                     wraplength=160, fg="#a00").pack(pady=10)
            return
        # one shared integer subsample factor -> picker thumbs stay proportional
        longest = 1
        for e in have_thumbs:
            try:
                img = tk.PhotoImage(file=str(e.thumb_path))
                longest = max(longest, img.width(), img.height())
            except Exception:
                pass
        factor = max(1, -(-longest // 150))         # ceil(longest / 150)
        for e in entries:
            cell = tk.Frame(self.pick_inner, padx=2, pady=3)
            cell.pack(fill="x")
            if e.thumb_path.exists():
                try:
                    img = tk.PhotoImage(file=str(e.thumb_path)).subsample(factor, factor)
                    self._photo_refs.append(img)
                    b = tk.Button(cell, image=img, relief="raised",
                                  command=lambda en=e: self._select_section(en))
                    b.pack()
                except Exception:
                    tk.Button(cell, text=e.alias,
                              command=lambda en=e: self._select_section(en)).pack()
            txt = e.alias + ("  (skip)" if e.skipped else "")
            tk.Label(cell, text=txt, font=("Segoe UI", 7),
                     fg="#888" if e.skipped else "#000").pack()

    # -- section / ROI -----------------------------------------------------
    def _select_section(self, entry: preview.SectionEntry) -> None:
        self.entry = entry
        self.roi_rgb = None
        self.layers = None
        self.btn_open.configure(state="disabled")
        self.status.configure(text=f"{entry.alias}: loading view…")
        self._reload_display()

    def _reload_display(self) -> None:
        """(Re)load the section's display image for ROI drawing (thumb or hi-res)."""
        if self.entry is None:
            return
        if self.hi_res.get():
            self._submit(self._read_hires, self.entry.scene, tag="display")
        else:
            try:
                bgr = cv2.imread(str(self.entry.thumb_path), cv2.IMREAD_COLOR)
                if bgr is None:
                    raise OSError("thumb unreadable")
                self._show_display(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            except Exception as exc:
                self.status.configure(text=f"thumb error: {exc}")

    def _read_hires(self, scene):
        import pylibCZIrw.czi as pyczi
        with pyczi.open_czi(scene.path) as doc:
            rgb, _ = preview.czi_io.read_overview(
                doc, scene, max_edge=self.cfg.maps_max_edge, zoom_cap=1.0,
                um_per_px=self.cfg.maps_um_per_px)
        return ("display", rgb)

    def _show_display(self, rgb: np.ndarray) -> None:
        self.disp_rgb = rgb
        self.roi_rgb = None
        self.ax.clear()
        self.ax.set_axis_off()
        self.ax.imshow(rgb)
        self.ax.set_title(f"{self.entry.alias} — drag a ROI", fontsize=9)
        self.selector.set_active(True)
        self.canvas.draw_idle()
        self.status.configure(text=f"{self.entry.alias}: drag a rectangle to set the ROI.")

    def _on_rect(self, eclick, erelease) -> None:
        x0, x1 = sorted((eclick.xdata, erelease.xdata))
        y0, y1 = sorted((eclick.ydata, erelease.ydata))
        self._sel_extents = (x0, y0, x1 - x0, y1 - y0)
        if self.entry is not None and self.disp_rgb is not None:
            h, w = self.disp_rgb.shape[:2]
            x, y, ww, hh = preview.roi_rect_full(
                self.entry.scene, w, h, *self._sel_extents,
                cap_um=self.cfg.gui.preview_roi_cap_um)
            self.roi_rect = (x, y, ww, hh)
            um = (ww * self.entry.scene.pixel_size_um
                  if self.entry.scene.pixel_size_um else 0)
            self.btn_open.configure(state="normal")
            self.status.configure(
                text=f"ROI {ww}x{hh}px (~{um:.0f}µm) — click 'Open ROI'.")

    def on_open_roi(self) -> None:
        if self.entry is None or self.roi_rect is None:
            return
        self.status.configure(text="reading ROI at full resolution…")
        self._submit(self._read_roi, self.entry.scene, self.roi_rect, tag="roi")

    def _read_roi(self, scene, rect):
        import pylibCZIrw.czi as pyczi
        x, y, w, h = rect
        with pyczi.open_czi(scene.path) as doc:
            rgb = preview.read_roi(doc, x, y, w, h, 1.0)
        return ("roi", rgb, scene.pixel_size_um)

    # -- recompute ---------------------------------------------------------
    def _on_field_change(self, section, attr, kind, recompute) -> None:
        var = self.field_vars[(section, attr)]
        obj = getattr(self.cfg, section) if section else self.cfg
        try:
            if kind == "bool":
                val = bool(var.get())
            elif kind == "int":
                val = int(float(var.get()))
            elif kind == "float":
                val = float(var.get())
            else:
                val = var.get()
        except (ValueError, tk.TclError):
            return                                  # mid-typing; ignore until valid
        setattr(obj, attr, val)
        if recompute:
            self._schedule_recompute()
        else:
            self._redraw()

    def _on_manual_toggle(self) -> None:
        auto = self.manual_auto.get()
        self.manual_entry.configure(state="disabled" if auto else "normal")
        self._schedule_recompute()

    def _schedule_recompute(self) -> None:
        if self._recompute_job is not None:
            self.after_cancel(self._recompute_job)
        self._recompute_job = self.after(250, self.request_recompute)

    def request_recompute(self) -> None:
        self._recompute_job = None
        if self.roi_rgb is None:
            return
        manual = None
        if not self.manual_auto.get():
            try:
                manual = float(self.manual_thr.get())
            except ValueError:
                manual = None
        cfg_snap = self._snapshot_cfg()
        self._submit(self._compute, self.roi_rgb, cfg_snap, self.roi_px_um, manual,
                     tag="layers")

    def _snapshot_cfg(self):
        """Deep-ish copy of the dataclass tree so the worker sees a stable config."""
        c = self.cfg
        return replace(
            c, tissue=replace(c.tissue), artifact=replace(c.artifact),
            fold=replace(c.fold), edge=replace(c.edge),
            detection=replace(c.detection), threshold=replace(c.threshold),
            overlay=replace(c.overlay))

    def _compute(self, rgb, cfg, px_um, manual):
        layers = preview.compute_roi_layers(rgb, cfg, px_um, manual_thr=manual)
        return ("layers", layers)

    # -- worker plumbing ---------------------------------------------------
    def _submit(self, fn, *args, tag="") -> None:
        if self._busy and tag == "layers":
            self._recompute_pending = True
            return
        self._busy = True
        self.status.configure(text="working…")

        def run():
            try:
                self.q.put(fn(*args))
            except Exception as exc:        # surface, don't crash the worker
                self.q.put(("error", f"{type(exc).__name__}: {exc}"))
        threading.Thread(target=run, daemon=True).start()

    def _drain_queue(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                self._busy = False
                kind = item[0]
                if kind == "display":
                    self._show_display(item[1])
                elif kind == "roi":
                    self.roi_rgb, self.roi_px_um = item[1], item[2]
                    self.selector.set_active(False)
                    self.request_recompute()
                elif kind == "layers":
                    self.layers = item[1]
                    self._after_layers()
                elif kind == "error":
                    self.status.configure(text=item[1])
                    messagebox.showerror("Preview error", item[1], parent=self)
        except queue.Empty:
            pass
        if self._recompute_pending and not self._busy:
            self._recompute_pending = False
            self.request_recompute()
        self.after(100, self._drain_queue)

    def _after_layers(self) -> None:
        lay = self.layers
        if lay is None:
            return
        if self.manual_auto.get():          # reflect the auto threshold back
            self.manual_thr.set(f"{lay['thr']:.4f}")
        t = lay["tissue"]
        pct = 100.0 * lay["sabg"].sum() / t.sum() if t.any() else 0.0
        self.status.configure(
            text=f"%SABG={pct:.2f}  thr={lay['thr']:.4f}  "
                 f"tissue={100*t.mean():.1f}%  fold={100*lay['fold'].mean():.1f}%")
        self._redraw()

    def _redraw(self) -> None:
        if self.roi_rgb is None or self.layers is None:
            return
        ov = self.cfg.overlay
        order = []
        for key, color_attr, _d in LAYER_SPEC:
            if self.show_vars[key].get():
                color = getattr(ov, color_attr)
                alpha = getattr(ov, {"nontissue": "nontissue_alpha",
                                     "artifact": "artifact_alpha",
                                     "fold": "fold_alpha", "sabg": "sabg_alpha",
                                     "edge_removed": "edge_alpha"}[key])
                order.append((self.layers[key], tuple(color), float(alpha)))
        comp = overlay.composite_overlay(self.roi_rgb, order)
        self.ax.clear()
        self.ax.set_axis_off()
        self.ax.imshow(comp)
        self.ax.set_title(f"{self.entry.alias} — ROI", fontsize=9)
        self.canvas.draw_idle()

    # -- export / close ----------------------------------------------------
    def on_export(self) -> None:
        dst = self.config_path
        if dst.exists() and not messagebox.askyesno(
                "Overwrite config?", f"Overwrite\n{dst}\nwith the current settings?",
                parent=self):
            return
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            preview.export_config(self.cfg, dst)
            self.status.configure(text=f"exported settings → {dst}")
            messagebox.showinfo("Exported", f"Settings written to\n{dst}", parent=self)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)

    def _on_close(self) -> None:
        self.destroy()
