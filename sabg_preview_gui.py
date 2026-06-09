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
from sabg_analyzer import overlay, preview, whitebalance
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
        self.wb_on = tk.BooleanVar(value=False)            # raw <-> white-balanced display
        self._wb_cache: dict[str, tuple] = {}              # which -> (src_rgb, wb_rgb)
        self._sb_preview_canvases: list[tk.Canvas] = []    # scale-bar corner schematics
        self.show_vars: dict[str, tk.BooleanVar] = {}
        self.field_vars: dict[tuple[str, str], tk.Variable] = {}
        self._photo_refs: list[tk.PhotoImage] = []         # keep picker thumbs alive
        self._dirty = False
        self._slider_win: tk.Toplevel | None = None        # slider-setup popup

        self.sb_pos.trace_add("write", lambda *_: self._draw_sb_preview())
        self._build_layout()
        self._draw_sb_preview()
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
        tk.Button(bar, text="Clear ROI", command=self.clear_roi).pack(side="left", padx=2)
        tk.Label(bar, text="resolution").pack(side="left", padx=(10, 0))
        labels = self._res_labels()
        ttk.OptionMenu(bar, self.view_res, labels[0], *labels,
                       command=lambda _v: self._reload_display()).pack(side="left")
        self.res_label = tk.Label(bar, text="loaded: —", fg="#557", font=("Segoe UI", 8))
        self.res_label.pack(side="left", padx=(6, 0))
        self._add_shared_tools(bar, "thumb")

        self.thumb_fig = Figure(figsize=(6, 6), tight_layout=True)
        self.thumb_ax = self.thumb_fig.add_subplot(111)
        self.thumb_ax.set_axis_off()
        self.thumb_canvas = FigureCanvasTkAgg(self.thumb_fig, master=tab)
        self.thumb_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.thumb_nav = CanvasNav(self.thumb_canvas, self.thumb_ax)
        # useblit + props => the rectangle renders live while dragging.
        self.selector = RectangleSelector(
            self.thumb_ax, self._on_rect, useblit=True, button=[1],
            minspanx=5, minspany=5, spancoords="pixels", interactive=True,
            props=dict(facecolor="orange", edgecolor="black", alpha=0.25, fill=True))
        self.selector.set_active(False)

    def _build_roi_tab(self) -> None:
        tab = tk.Frame(self.nb)
        self.nb.add(tab, text="ROI")
        self.roi_tab = tab
        bar = tk.Frame(tab)
        bar.pack(fill="x")
        tk.Button(bar, text="Close ROI", command=self.clear_roi).pack(side="left", padx=2, pady=2)
        self._add_shared_tools(bar, "roi")

        self.roi_fig = Figure(figsize=(6, 6), tight_layout=True)
        self.roi_ax = self.roi_fig.add_subplot(111)
        self.roi_ax.set_axis_off()
        self.roi_canvas = FigureCanvasTkAgg(self.roi_fig, master=tab)
        self.roi_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.roi_nav = CanvasNav(self.roi_canvas, self.roi_ax)
        self._roi_hint()
        self.nb.tab(self.roi_tab, state="disabled")    # greyed until a ROI is opened

    # -- shared toolbar / scale-bar preview / white balance ----------------
    def _nav(self, source: str):
        return self.thumb_nav if source == "thumb" else self.roi_nav

    def _add_shared_tools(self, bar: tk.Frame, source: str) -> None:
        """Controls present on BOTH tabs, in the same order: Reset view, white
        balance toggle, scale-bar (length/label/corner + preview), Save, Help."""
        tk.Button(bar, text="Reset view",
                  command=lambda: self._nav(source).reset()).pack(side="left", padx=(10, 2))
        tk.Checkbutton(bar, text="white-balanced", variable=self.wb_on,
                       command=self._on_wb_toggle).pack(side="left", padx=(8, 2))
        tk.Label(bar, text="bar").pack(side="left", padx=(8, 0))
        ttk.OptionMenu(bar, self.sb_len, self.sb_len.get(), "Auto", "50", "100",
                       "200", "500", "1000").pack(side="left")
        tk.Checkbutton(bar, text="label", variable=self.sb_label).pack(side="left")
        ttk.OptionMenu(bar, self.sb_pos, self.sb_pos.get(),
                       *_SB_POS.keys()).pack(side="left")
        cvp = tk.Canvas(bar, width=24, height=16, highlightthickness=1,
                        highlightbackground="#aaa", bg="white")
        cvp.pack(side="left", padx=4)
        self._sb_preview_canvases.append(cvp)
        tk.Button(bar, text="Export…",
                  command=lambda: self.on_export_image(source)).pack(side="left", padx=(8, 2))
        tk.Button(bar, text="?", width=2, command=self._show_help).pack(side="right", padx=2)

    def _draw_sb_preview(self) -> None:
        """Tiny schematic of where the scale bar will land in each corner."""
        pos = _SB_POS.get(self.sb_pos.get(), "br")
        for cv in getattr(self, "_sb_preview_canvases", []):
            cv.delete("all")
            w = int(cv["width"]); h = int(cv["height"])
            bx = 3 if "l" in pos else w - 12
            by = 3 if pos.startswith("t") else h - 6
            cv.create_rectangle(bx, by, bx + 9, by + 3, fill="black", outline="")

    def _wb(self, rgb: np.ndarray, which: str) -> np.ndarray:
        """White-balanced copy of *rgb* (cached per *which* by image identity)."""
        c = self._wb_cache.get(which)
        if c is not None and c[0] is rgb:
            return c[1]
        out = whitebalance.white_balance(rgb, whitebalance.estimate_white_point(rgb))
        self._wb_cache[which] = (rgb, out)
        return out

    def _on_wb_toggle(self) -> None:
        if self.disp_rgb is not None and self.roi_rgb is None:
            self._show_display(self.disp_rgb)
        if self.roi_rgb is not None and self.layers is not None:
            self._redraw()

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
                                       bg="#3a7d44", fg="white", activebackground="#2f6638",
                                       command=self.request_recompute)
        self.btn_recompute.pack(side="left", fill="x", expand=True)
        self.dirty_dot = tk.Label(top, text="✓ up to date", fg="#3a7d44",
                                  font=("Segoe UI", 9, "bold"))
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

        # guided "slider setup" mode (one sensitivity bar per layer)
        tk.Button(body, text="🎚  Slider setup…", command=self.on_slider_setup).pack(
            fill="x", padx=6, pady=(2, 4))

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
        self.thumb_ax.imshow(self._wb(rgb, "disp") if self.wb_on.get() else rgb)
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
        # Drawing a ROI opens it directly (no separate "Open ROI" step).
        self.status.configure(
            text=f"ROI {ww}x{hh}px (~{um:.0f}µm){capped} — reading at full resolution…")
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
        self._wb_cache.pop("roi", None)
        if hasattr(self, "roi_tab"):
            self.nb.tab(self.roi_tab, state="disabled")    # no ROI -> grey the tab
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
        self.dirty_dot.configure(text="● changed", fg="#c8862a")
        self.btn_recompute.configure(bg="#c8862a", activebackground="#a86f22")

    def _clear_dirty(self) -> None:
        self._dirty = False
        self.dirty_dot.configure(text="✓ up to date", fg="#3a7d44")
        self.btn_recompute.configure(bg="#3a7d44", activebackground="#2f6638")

    def _on_manual_toggle(self) -> None:
        auto = self.manual_auto.get()
        self.manual_entry.configure(state="disabled" if auto else "normal")
        self._on_field_edit(recompute=True)

    # -- slider-setup mode -------------------------------------------------
    def on_slider_setup(self) -> None:
        """Open a popup with one guided sensitivity bar per layer.

        Each bar drives the same cfg knob(s) as the field rows (so the right panel
        stays in sync) and the ROI auto-recomputes via the usual 250 ms debounce.
        """
        if self._slider_win is not None and self._slider_win.winfo_exists():
            self._slider_win.lift()
            return
        win = tk.Toplevel(self)
        self._slider_win = win
        win.title("Slider setup — guided tuning")
        win.geometry("440x420")
        adv = tk.BooleanVar(value=False)
        tk.Checkbutton(win, text="advanced (raw knobs)", variable=adv,
                       command=lambda: self._build_sliders(body, adv)).pack(
                           anchor="w", padx=10, side="bottom")
        body = tk.Frame(win)
        body.pack(fill="both", expand=True)
        self._build_sliders(body, adv)

    def _build_sliders(self, body: tk.Frame, adv: tk.BooleanVar) -> None:
        for w in body.winfo_children():
            w.destroy()
        tk.Label(body, justify="left", fg="#555",
                 text="Drag a layer's bar: left = detect less, right = more.\n"
                      "The ROI recomputes automatically as you slide.").pack(
                          anchor="w", padx=10, pady=(8, 4))
        for label, knobs in gw.SLIDER_LAYERS:
            frame = tk.LabelFrame(body, text=label, padx=6, pady=4)
            frame.pack(fill="x", padx=8, pady=3)
            used = knobs if adv.get() else knobs[:1]
            for section, attr, v0, v100, klab in used:
                row = tk.Frame(frame)
                row.pack(fill="x")
                tk.Label(row, text=(klab if adv.get() else "sensitivity"),
                         width=14, anchor="w").pack(side="left")
                obj = getattr(self.cfg, section) if section else self.cfg
                init = gw.value_to_slider(v0, v100, float(getattr(obj, attr)))
                sv = tk.DoubleVar(value=init)
                ttk.Scale(row, from_=0, to=100, variable=sv,
                          command=lambda val, se=section, at=attr, a=v0, b=v100:
                          self._slider_apply(se, at, a, b, float(val))).pack(
                              side="left", fill="x", expand=True, padx=4)

    def _slider_apply(self, section, attr, v0, v100, s) -> None:
        val = gw.slider_to_value(v0, v100, s)
        key = (section, attr)
        if key in self.field_vars:                  # keep the right panel in sync
            self.field_vars[key].set(f"{val:.4g}")  # its trace marks dirty + recomputes
        else:
            obj = getattr(self.cfg, section) if section else self.cfg
            setattr(obj, attr, val)
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
                    self._wb_cache.pop("roi", None)     # new ROI invalidates WB cache
                    self.selector.set_active(False)     # ROI fixed while open
                    self.nb.tab(self.roi_tab, state="normal")
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

    def _overlay_order(self) -> list | None:
        """The visible ``(mask, color, alpha)`` layers, in draw order (or None)."""
        if self.layers is None:
            return None
        ov = self.cfg.overlay
        order = []
        for key, color_attr, alpha_attr, _d in gw.LAYER_SPEC:
            if self.show_vars[key].get():
                order.append((self.layers[key], tuple(getattr(ov, color_attr)),
                              float(getattr(ov, alpha_attr))))
        return order

    def _composite(self) -> np.ndarray | None:
        if self.roi_rgb is None or self.layers is None:
            return None
        base = self._wb(self.roi_rgb, "roi") if self.wb_on.get() else self.roi_rgb
        return overlay.composite_overlay(base, self._overlay_order() or [])

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
    def on_export_image(self, source: str = "roi") -> None:
        """Export publication presets via `preview.export_roi`.

        ROI tab → raw / wb+scalebar / wb+overlay+scalebar (the three handover
        presets). Thumbnail tab → raw / wb+scalebar (no overlay layers there).
        The user picks a base name + format; presets are appended as suffixes.
        """
        if source == "thumb":
            if self.disp_rgb is None or self.entry is None:
                messagebox.showinfo("Export", "Pick a section first.", parent=self)
                return
            rgb, px_um, order = self.disp_rgb, self._loaded_um, None
            default, target = f"{self.entry.alias}_thumb", 1000.0
        else:
            if self.roi_rgb is None:
                messagebox.showinfo("Export", "Open a ROI and recompute first.", parent=self)
                return
            rgb, px_um, order = self.roi_rgb, self.roi_px_um, self._overlay_order()
            default, target = f"{self.entry.alias}_roi", 200.0
        path = filedialog.asksaveasfilename(
            parent=self, title="Export base name (presets are appended)",
            defaultextension=".jpg", initialfile=default,
            filetypes=[("JPEG", "*.jpg"), ("PNG", "*.png")])
        if not path:
            return
        base = Path(path)
        fmt = base.suffix.lstrip(".").lower() or "jpg"
        if fmt not in ("jpg", "jpeg", "png"):
            fmt = "jpg"
        base = base.with_suffix("")
        try:
            written = preview.export_roi(
                rgb, px_um, base, order=order, formats=(fmt,),
                scalebar_um=self.sb_len.get(), scalebar_pos=_SB_POS[self.sb_pos.get()],
                scalebar_label=self.sb_label.get(), wb=True, target_um=target)
            self.status.configure(
                text=f"exported {len(written)} preset(s) → {base.parent}")
            messagebox.showinfo("Exported", "Wrote:\n" + "\n".join(p.name for p in written),
                                parent=self)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)

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

    # -- help --------------------------------------------------------------
    def _show_help(self) -> None:
        gw.help_popup(self, "Preview / Tune — help", [
            ("Picking a section",
             "Click a thumbnail on the left. Thumbnails are sized proportional to "
             "each section's physical size."),
            ("Canvas navigation",
             "Mouse wheel zooms to the cursor; middle- or right-drag pans; "
             "'Reset view' restores the full extent."),
            ("Resolution (Thumbnail tab)",
             "Choose the µm/px the section is read at for ROI drawing. 'loaded: …' "
             "shows the actual µm/px achieved for the image on screen."),
            ("Drawing a ROI",
             "'Draw ROI', then drag a rectangle (it shows live). Releasing opens the "
             "crop at full resolution in the ROI tab (capped at gui.preview_roi_cap_um). "
             "The ROI tab is greyed until a ROI is open. 'Clear ROI' starts over."),
            ("White-balanced",
             "Toggle a publication-style white balance on the displayed image "
             "(quantification always uses raw pixels — display only)."),
            ("Scale bar",
             "Length (Auto picks a nice value), label on/off, and corner; the little "
             "schematic shows where the bar lands. Used by Export."),
            ("Export…",
             "Writes publication presets next to a base name you pick: raw, "
             "white-balanced + scale bar, and (ROI tab) white-balanced + overlay + "
             "scale bar. The Thumbnail tab exports the whole section (no overlay)."),
            ("Tuning (right panel)",
             "Edit any detection setting; the ROI recomputes after a short pause "
             "(orange 'changed' → green 'up to date'). The Result panel shows %SABG, "
             "thresholds, tissue%, pixel counts and mm² areas. Layers toggles which "
             "overlay masks are drawn / their colour + alpha."),
            ("Export → config",
             "Writes the current settings to config.yaml for the batch analyze/export."),
        ])

    def _on_close(self) -> None:
        self.destroy()
