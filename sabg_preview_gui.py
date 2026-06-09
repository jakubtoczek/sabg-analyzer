"""Preview / tuning window for the SABG Analyzer (matplotlib embedded in Tkinter).

Opened from the main GUI's *Preview* button. Flow:

  1. Pick a section thumbnail (left); thumbs are sized proportional to physical size.
  2. *Draw ROI* and drag a rectangle on the Thumbnail tab (capped at gui.preview_roi_cap_um).
  3. *Open ROI* reads that crop at full resolution into the ROI tab.
  4. Tune every detection setting live in the right-hand panel (collapsible groups in
     pipeline order); each mask is recomputed in-process via `sabg_analyzer.preview` —
     the SAME mask math as the batch analysis — and drawn over the ROI.
  5. *Save* exports the current ROI overlay as a PNG with a scale bar burned in;
     *Export → config.yaml* writes the chosen settings for the batch run.

Canvas navigation is mouse-only: wheel zooms to the cursor, middle/right drag pans,
*Reset view* restores the full extent. Heavy CZI reads and the per-change recompute
run on a worker thread; results come back through a queue drained on the Tk main loop.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import replace
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.widgets import RectangleSelector

import sabg_gui_widgets as gw
from sabg_analyzer import export, overlay, preview
from sabg_analyzer.config import load_config

_VIEW_MULTS = [1, 2, 4, 8]          # thumb-resolution multipliers for the px/µm picker
# Scale-bar corner labels (clear) -> the export.draw_scalebar position codes.
_SB_POS = {"bottom-right": "br", "bottom-left": "bl",
           "top-right": "tr", "top-left": "tl"}


# ---------------------------------------------------------------------------
# mouse-only canvas navigation (replaces NavigationToolbar2Tk)
# ---------------------------------------------------------------------------
class CanvasNav:
    """Wheel-zoom-to-cursor + middle/right-drag pan on one matplotlib axes."""

    def __init__(self, canvas: FigureCanvasTkAgg, ax) -> None:
        self.canvas = canvas
        self.ax = ax
        self._home: tuple | None = None
        self._panning = False
        canvas.mpl_connect("scroll_event", self._zoom)
        canvas.mpl_connect("button_press_event", self._press)
        canvas.mpl_connect("motion_notify_event", self._drag)
        canvas.mpl_connect("button_release_event", self._release)

    def set_home(self) -> None:
        self._home = (self.ax.get_xlim(), self.ax.get_ylim())

    def clear_home(self) -> None:
        self._home = None

    def reset(self) -> None:
        if self._home is not None:
            self.ax.set_xlim(*self._home[0])
            self.ax.set_ylim(*self._home[1])
            self.canvas.draw_idle()

    def _zoom(self, e) -> None:
        if e.inaxes is not self.ax or e.xdata is None:
            return
        scale = 0.8 if e.button == "up" else 1.25      # wheel up = zoom in
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        xd, yd = e.xdata, e.ydata
        self.ax.set_xlim(xd - (xd - x0) * scale, xd + (x1 - xd) * scale)
        self.ax.set_ylim(yd - (yd - y0) * scale, yd + (y1 - yd) * scale)
        self.canvas.draw_idle()

    def _press(self, e) -> None:
        if e.button in (2, 3) and e.inaxes is self.ax:
            self.ax.start_pan(e.x, e.y, 1)             # button 1 => plain pan
            self._panning = True

    def _drag(self, e) -> None:
        if self._panning:
            self.ax.drag_pan(1, e.key, e.x, e.y)
            self.canvas.draw_idle()

    def _release(self, e) -> None:
        if self._panning:
            self.ax.end_pan()
            self._panning = False


class PreviewWindow(tk.Toplevel):
    def __init__(self, master, data_dir: str, out_dir: str, config_path: str) -> None:
        super().__init__(master)
        self.title("SABG Preview / Tune")
        self.geometry("1320x840")
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
        self._setting_extents = False                      # guard for programmatic clamp

        self.manual_auto = tk.BooleanVar(value=True)       # auto threshold on ROI
        self.manual_thr = tk.StringVar(value="")
        self.view_res = tk.StringVar()                     # px/µm label (supersedes gui.preview_hi_res)
        self._res_to_mult: dict[str, int] = {}             # label -> thumb multiplier
        self._loaded_um: float | None = None               # achieved µm/px of the shown image
        self.sb_len = tk.StringVar(value="Auto")           # scale-bar length (µm)
        self.sb_label = tk.BooleanVar(value=True)
        self.sb_pos = tk.StringVar(value="bottom-right")
        self.show_vars: dict[str, tk.BooleanVar] = {}
        self.field_vars: dict[tuple[str, str], tk.Variable] = {}
        self._photo_refs: list[tk.PhotoImage] = []         # keep picker thumbs alive
        self._dirty = False

        self._build_layout()
        self._populate_picker()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_queue)

    # -- layout ------------------------------------------------------------
    def _build_layout(self) -> None:
        # left: picker (scroll) | center: notebook of canvases | right: tuning (scroll)
        left = tk.Frame(self, width=190)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="Sections", font=("Segoe UI", 9, "bold")).pack(pady=(6, 2))
        self.pick = gw.ScrollFrame(left)
        self.pick.pack(fill="both", expand=True)

        right = tk.Frame(self, width=360)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self._build_tuning_panel(right)

        center = tk.Frame(self)
        center.pack(side="left", fill="both", expand=True)
        self.status = tk.Label(center, text="Pick a section, then Draw ROI.",
                               anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")
        self.nb = ttk.Notebook(center)
        self.nb.pack(fill="both", expand=True)
        self._build_thumb_tab()
        self._build_roi_tab()

    def _build_thumb_tab(self) -> None:
        tab = tk.Frame(self.nb)
        self.nb.add(tab, text="Thumbnail")
        bar = tk.Frame(tab)
        bar.pack(fill="x")
        tk.Button(bar, text="Draw ROI", command=self.on_draw_roi).pack(side="left", padx=2, pady=2)
        self.btn_open = tk.Button(bar, text="Open ROI", command=self.on_open_roi,
                                  state="disabled")
        self.btn_open.pack(side="left", padx=2)
        tk.Button(bar, text="Clear ROI", command=self.clear_roi).pack(side="left", padx=2)
        tk.Button(bar, text="Reset view", command=lambda: self.thumb_nav.reset()).pack(side="left", padx=2)
        tk.Label(bar, text="resolution").pack(side="left", padx=(12, 0))
        labels = self._res_labels()
        ttk.OptionMenu(bar, self.view_res, labels[0], *labels,
                       command=lambda _v: self._reload_display()).pack(side="left")
        self.res_label = tk.Label(bar, text="loaded: —", fg="#557", font=("Segoe UI", 8))
        self.res_label.pack(side="left", padx=(8, 0))

        self.thumb_fig = Figure(figsize=(6, 6), tight_layout=True)
        self.thumb_ax = self.thumb_fig.add_subplot(111)
        self.thumb_ax.set_axis_off()
        self.thumb_canvas = FigureCanvasTkAgg(self.thumb_fig, master=tab)
        self.thumb_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.thumb_nav = CanvasNav(self.thumb_canvas, self.thumb_ax)
        self.selector = RectangleSelector(
            self.thumb_ax, self._on_rect, useblit=False, button=[1],
            minspanx=5, minspany=5, spancoords="pixels", interactive=True)
        self.selector.set_active(False)

    def _build_roi_tab(self) -> None:
        tab = tk.Frame(self.nb)
        self.nb.add(tab, text="ROI")
        self.roi_tab = tab
        bar = tk.Frame(tab)
        bar.pack(fill="x")
        tk.Button(bar, text="Reset view", command=lambda: self.roi_nav.reset()).pack(side="left", padx=2, pady=2)
        tk.Button(bar, text="Save…", command=self.on_save).pack(side="left", padx=2)
        tk.Label(bar, text="bar").pack(side="left", padx=(10, 0))
        ttk.OptionMenu(bar, self.sb_len, "Auto", "Auto", "50", "100", "200",
                       "500", "1000").pack(side="left")
        tk.Checkbutton(bar, text="label", variable=self.sb_label).pack(side="left")
        ttk.OptionMenu(bar, self.sb_pos, "bottom-right", *_SB_POS.keys()).pack(side="left")
        tk.Button(bar, text="Close ROI", command=self.clear_roi).pack(side="left", padx=(10, 2))

        self.roi_fig = Figure(figsize=(6, 6), tight_layout=True)
        self.roi_ax = self.roi_fig.add_subplot(111)
        self.roi_ax.set_axis_off()
        self.roi_canvas = FigureCanvasTkAgg(self.roi_fig, master=tab)
        self.roi_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.roi_nav = CanvasNav(self.roi_canvas, self.roi_ax)
        self._roi_hint()

    def _roi_hint(self) -> None:
        self.roi_ax.clear()
        self.roi_ax.set_axis_off()
        self.roi_ax.text(0.5, 0.5, "Draw a ROI on the Thumbnail tab,\nthen 'Open ROI'.",
                         ha="center", va="center", fontsize=10, color="#888")
        self.roi_canvas.draw_idle()

    def _build_tuning_panel(self, parent: tk.Frame) -> None:
        sf = gw.ScrollFrame(parent)
        sf.pack(fill="both", expand=True)
        body = sf.interior

        # top controls
        top = tk.Frame(body, padx=6, pady=6)
        top.pack(fill="x")
        self.btn_recompute = tk.Button(top, text="↻  Recompute", font=("Segoe UI", 10, "bold"),
                                       bg="#2e7d32", fg="white", command=self.request_recompute)
        self.btn_recompute.pack(side="left", fill="x", expand=True)
        self.dirty_dot = tk.Label(top, text="●", fg="#bbb", font=("Segoe UI", 12))
        self.dirty_dot.pack(side="left", padx=4)
        tk.Button(top, text="Export → config", command=self.on_export).pack(side="left", padx=2)

        # result characteristics (filled after each recompute / whole-section run)
        res = tk.LabelFrame(body, text="Result", padx=6, pady=4)
        res.pack(fill="x", padx=6, pady=2)
        self.stats_label = tk.Label(res, text="(recompute to see %SABG, thresholds, areas)",
                                    anchor="w", justify="left", font=("Consolas", 9),
                                    fg="#333")
        self.stats_label.pack(fill="x")

        # threshold override
        thr = tk.LabelFrame(body, text="Seed threshold", padx=6, pady=4)
        thr.pack(fill="x", padx=6, pady=2)
        tk.Checkbutton(thr, text="Auto on ROI", variable=self.manual_auto,
                       command=self._on_manual_toggle).grid(row=0, column=0, sticky="w")
        tk.Label(thr, text="manual:").grid(row=0, column=1, sticky="e")
        self.manual_entry = tk.Entry(thr, textvariable=self.manual_thr, width=10,
                                     state="disabled")
        self.manual_entry.grid(row=0, column=2, sticky="w")
        self.manual_thr.trace_add("write", lambda *_: self._on_field_edit(recompute=True))

        # layers panel (show / colour / alpha per layer)
        lay = tk.LabelFrame(body, text="Layers", padx=6, pady=4)
        lay.pack(fill="x", padx=6, pady=2)
        gw.build_layers_panel(lay, self.cfg, self.show_vars, self._redraw)

        # collapsible detection groups
        gw.build_groups(body, self.cfg, gw.DETECTION_GROUPS, self.field_vars,
                        self._on_field, recompute=True,
                        opened={"1. Tissue", "4. SABG detection"})

    # -- picker ------------------------------------------------------------
    def _populate_picker(self) -> None:
        try:
            entries = preview.list_sections(self.data_dir, self.out_dir, self.cfg)
        except Exception as exc:
            tk.Label(self.pick.interior, text=f"(error: {exc})", wraplength=160,
                     fg="red").pack()
            return
        gw.thumbnail_picker(self.pick.interior, entries, self._select_section,
                            self._photo_refs)

    # -- section / ROI -----------------------------------------------------
    def _select_section(self, entry: preview.SectionEntry) -> None:
        self.entry = entry
        self.clear_roi(refresh=False)         # switching sections clears any ROI
        self.status.configure(text=f"{entry.alias}: loading view…")
        self.nb.select(0)
        self._reload_display()

    def _res_labels(self) -> list[str]:
        """px/µm picker labels (one per thumb multiplier) + fill ``_res_to_mult``.

        Thumb resolution is ``cfg.thumb_um_per_px``; finer multipliers read the
        section at proportionally smaller µm/px (×2 -> half the µm/px).
        """
        self._res_to_mult.clear()
        labels: list[str] = []
        for m in _VIEW_MULTS:
            um = self.cfg.thumb_um_per_px / m
            lab = f"{um:g} µm/px" + ("  (thumb)" if m == 1 else "")
            labels.append(lab)
            self._res_to_mult[lab] = m
        if not self.view_res.get():
            self.view_res.set(labels[0])
        return labels

    def _reload_display(self) -> None:
        """(Re)load the section's display image for ROI drawing (thumb or finer)."""
        if self.entry is None:
            return
        mult = self._res_to_mult.get(self.view_res.get(), 1)
        if mult <= 1:
            try:
                bgr = cv2.imread(str(self.entry.thumb_path), cv2.IMREAD_COLOR)
                if bgr is None:
                    raise OSError("thumb unreadable")
                self._show_display(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            except Exception as exc:
                self.status.configure(text=f"thumb error: {exc}")
        else:
            self._submit(self._read_display_hi, self.entry.scene, mult, tag="display")

    def _read_display_hi(self, scene, mult):
        import pylibCZIrw.czi as pyczi
        um = max(self.cfg.maps_um_per_px * 0.25, self.cfg.thumb_um_per_px / mult)
        edge = min(self.cfg.maps_max_edge, int(self.cfg.thumb_max_edge * mult))
        with pyczi.open_czi(scene.path) as doc:
            rgb, _ = preview.czi_io.read_overview(
                doc, scene, max_edge=edge, zoom_cap=1.0, um_per_px=um)
        return ("display", rgb)

    def _show_display(self, rgb: np.ndarray) -> None:
        self.disp_rgb = rgb
        self.thumb_ax.clear()
        self.thumb_ax.set_axis_off()
        self.thumb_ax.imshow(rgb)
        title = "drag a ROI" if self.roi_rgb is None else "ROI fixed — Clear ROI to redraw"
        self.thumb_ax.set_title(f"{self.entry.alias} — {title}", fontsize=9)
        # Achieved µm/px: the display spans the whole scene bbox, so it's the
        # section's pixel size scaled by (full-res width / displayed width).
        sc = self.entry.scene
        if sc.pixel_size_um and rgb.shape[1]:
            self._loaded_um = sc.pixel_size_um * sc.w / rgb.shape[1]
            self.res_label.configure(text=f"loaded: {self._loaded_um:.3g} µm/px")
        else:
            self._loaded_um = None
            self.res_label.configure(text="loaded: —")
        self.thumb_canvas.draw_idle()
        self.thumb_nav.set_home()
        self.selector.set_active(self.roi_rgb is None)
        if self.roi_rgb is None:
            self.status.configure(
                text=f"{self.entry.alias}: 'Draw ROI', then drag a rectangle.")

    def on_draw_roi(self) -> None:
        if self.entry is None:
            return
        if self.roi_rgb is not None:           # start a fresh ROI
            self.clear_roi()
        self.nb.select(0)
        self.selector.set_active(True)
        self.status.configure(text="drag a rectangle to set the ROI.")

    def _on_rect(self, eclick, erelease) -> None:
        if self._setting_extents or self.entry is None or self.disp_rgb is None:
            return
        if eclick.xdata is None or erelease.xdata is None:
            return
        x0, x1 = sorted((eclick.xdata, erelease.xdata))
        y0, y1 = sorted((eclick.ydata, erelease.ydata))
        h, w = self.disp_rgb.shape[:2]
        x, y, ww, hh = preview.roi_rect_full(
            self.entry.scene, w, h, x0, y0, x1 - x0, y1 - y0,
            cap_um=self.cfg.gui.preview_roi_cap_um)
        self.roi_rect = (x, y, ww, hh)
        # reflect the cap back onto the on-screen rectangle (held at the cap)
        sc = self.entry.scene
        tx0 = (x - sc.x) * w / max(sc.w, 1)
        ty0 = (y - sc.y) * h / max(sc.h, 1)
        tw = ww * w / max(sc.w, 1)
        th = hh * h / max(sc.h, 1)
        self._sel_extents = (tx0, ty0, tw, th)
        self._setting_extents = True
        try:
            self.selector.extents = (tx0, tx0 + tw, ty0, ty0 + th)
        finally:
            self._setting_extents = False
        um = ww * sc.pixel_size_um if sc.pixel_size_um else 0
        capped = " (at cap)" if self.cfg.gui.preview_roi_cap_um and um >= \
            self.cfg.gui.preview_roi_cap_um - 1 else ""
        self.btn_open.configure(state="normal")
        self.status.configure(text=f"ROI {ww}x{hh}px (~{um:.0f}µm){capped} — 'Open ROI'.")

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

    def clear_roi(self, refresh: bool = True) -> None:
        """Drop the current ROI and return to an editable thumbnail.

        *refresh* redraws the thumbnail (removing the rectangle) and switches to
        the Thumbnail tab; pass False when a new section is about to reload it.
        """
        self.roi_rgb = None
        self.roi_px_um = None
        self.roi_rect = None
        self.layers = None
        self._sel_extents = None
        self.roi_nav.clear_home()
        self.btn_open.configure(state="disabled")
        self._roi_hint()
        if refresh and self.entry is not None and self.disp_rgb is not None:
            self._show_display(self.disp_rgb)   # clears axes -> removes the rectangle
            self.nb.select(0)

    # -- recompute / dirty -------------------------------------------------
    def _on_field(self, section, attr, kind, recompute) -> None:
        var = self.field_vars[(section, attr)]
        if not gw.apply_field(self.cfg, section, attr, kind, var):
            return                              # mid-typing; ignore until valid
        self._on_field_edit(recompute)

    def _on_field_edit(self, recompute) -> None:
        self._mark_dirty()
        if recompute:
            self._schedule_recompute()
        else:
            self._redraw()

    def _mark_dirty(self) -> None:
        self._dirty = True
        self.dirty_dot.configure(fg="#e8a000")
        self.btn_recompute.configure(bg="#e8a000")

    def _clear_dirty(self) -> None:
        self._dirty = False
        self.dirty_dot.configure(fg="#bbb")
        self.btn_recompute.configure(bg="#2e7d32")

    def _on_manual_toggle(self) -> None:
        auto = self.manual_auto.get()
        self.manual_entry.configure(state="disabled" if auto else "normal")
        self._on_field_edit(recompute=True)

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
                    self.selector.set_active(False)     # ROI fixed while open
                    self.nb.select(self.roi_tab)
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
        self._clear_dirty()
        if self.manual_auto.get():          # reflect the auto threshold back
            self.manual_thr.set(f"{lay['thr']:.4f}")
        t = lay["tissue"]
        pct = 100.0 * lay["sabg"].sum() / t.sum() if t.any() else 0.0
        self.status.configure(
            text=f"%SABG={pct:.2f}  thr={lay['thr']:.4f}  "
                 f"tissue={100*t.mean():.1f}%  fold={100*lay['fold'].mean():.1f}%")
        self._show_stats(lay, self.roi_px_um, scope="ROI")
        self._redraw()

    def _show_stats(self, lay: dict, px_um: float | None, scope: str = "ROI") -> None:
        """Fill the Result panel with the key characteristics of *lay*."""
        t = lay["tissue"]
        tissue_px = int(t.sum())
        sabg_px = int(lay["sabg"].sum())
        fold_px = int(lay["fold"].sum())
        art_px = int(lay["artifact"].sum())
        edge_px = int(lay["edge_removed"].sum())
        pct = 100.0 * sabg_px / tissue_px if tissue_px else 0.0
        thr_s = lay.get("thr_s")
        thr_line = f"thr {lay['thr']:.4f}" + (f"   2nd {thr_s:.4f}" if thr_s else "")
        lines = [f"{scope}   %SABG = {pct:.2f}", thr_line,
                 f"tissue {100*t.mean():.1f}% of {scope.lower()}"]
        if px_um:
            mm2 = (px_um / 1000.0) ** 2
            lines.append(f"SABG+  {sabg_px:>12,} px  {sabg_px*mm2:.4f} mm²")
            lines.append(f"tissue {tissue_px:>12,} px  {tissue_px*mm2:.3f} mm²")
        else:
            lines.append(f"SABG+  {sabg_px:,} px   tissue {tissue_px:,} px")
        lines.append(f"fold {100*lay['fold'].mean():.1f}%  "
                     f"artifact {100*lay['artifact'].mean():.1f}%  "
                     f"edge-rej {100*lay['edge_removed'].mean():.1f}%")
        self.stats_label.configure(text="\n".join(lines))

    def _composite(self) -> np.ndarray | None:
        if self.roi_rgb is None or self.layers is None:
            return None
        ov = self.cfg.overlay
        order = []
        for key, color_attr, alpha_attr, _d in gw.LAYER_SPEC:
            if self.show_vars[key].get():
                order.append((self.layers[key], tuple(getattr(ov, color_attr)),
                              float(getattr(ov, alpha_attr))))
        return overlay.composite_overlay(self.roi_rgb, order)

    def _redraw(self) -> None:
        comp = self._composite()
        if comp is None:
            return
        keep = (self.roi_ax.get_xlim(), self.roi_ax.get_ylim())
        self.roi_ax.clear()
        self.roi_ax.set_axis_off()
        self.roi_ax.imshow(comp)
        self.roi_ax.set_title(f"{self.entry.alias} — ROI", fontsize=9)
        if self.roi_nav._home is not None:          # preserve zoom/pan across redraws
            self.roi_ax.set_xlim(*keep[0])
            self.roi_ax.set_ylim(*keep[1])
        else:
            self.roi_nav.set_home()
        self.roi_canvas.draw_idle()

    # -- save / export / close ---------------------------------------------
    def on_save(self) -> None:
        comp = self._composite()
        if comp is None:
            messagebox.showinfo("Save", "Open a ROI and recompute first.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg")],
            initialfile=f"{self.entry.alias}_roi.png")
        if not path:
            return
        img = comp
        px_um = self.roi_px_um
        if px_um:
            if self.sb_len.get() == "Auto":
                bar_um = export.adaptive_bar_um(img.shape[1], px_um, target_um=200.0)
            else:
                bar_um = float(self.sb_len.get())
            img = export.draw_scalebar(img, px_um, bar_um, color=(0, 0, 0),
                                       label=self.sb_label.get(),
                                       position=_SB_POS[self.sb_pos.get()])
        else:
            messagebox.showwarning("Save", "No pixel size — saving without a scale bar.",
                                   parent=self)
        try:
            cv2.imwrite(path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
            self.status.configure(text=f"saved {path}")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)

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
