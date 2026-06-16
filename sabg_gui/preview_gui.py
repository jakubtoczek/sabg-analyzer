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

Canvas navigation is mouse-only: wheel zooms to the cursor; left-drag pans too
(except while drawing a ROI or painting the exclusion brush), middle/right drag
always pans, *Reset view* restores the full extent. Heavy CZI reads and the
per-change recompute run on a worker thread; results come back through a queue
drained on the Tk main loop.
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
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Circle, Rectangle
from matplotlib.widgets import RectangleSelector
from mpl_toolkits.axes_grid1.anchored_artists import AnchoredSizeBar

from . import widgets as gw
from .widgets import CanvasNav              # shared mouse-only canvas navigation
from sabg_analyzer import export, overlay, preview, whitebalance
from sabg_analyzer.config import load_config

_VIEW_MULTS = [1, 2, 4, 8]          # thumb-resolution multipliers for the px/µm picker
# Scale-bar corner labels (clear) -> the export.draw_scalebar position codes.
_SB_POS = {"bottom-right": "br", "bottom-left": "bl",
           "top-right": "tr", "top-left": "tl"}
# ...and the same corners as matplotlib legend `loc` codes (for the live scale bar).
_SB_LOC = {"br": "lower right", "bl": "lower left",
           "tr": "upper right", "tl": "upper left"}


def _fmt_bar_um(bar_um: float) -> str:
    """Scale-bar label: millimetres for >=1 mm, else micrometres (matches export)."""
    return f"{bar_um / 1000:g} mm" if bar_um >= 1000 else f"{bar_um:g} µm"


class _GuiProgress:
    """Minimal Progress stand-in for `pipeline.analyze_scene`: posts the section
    completion percentage to the GUI queue (throttled to whole-percent steps)."""

    def __init__(self, q: queue.Queue) -> None:
        self.q = q
        self.total = 1
        self.done = 0
        self._last = -1

    def start_section(self, alias, total) -> None:
        self.total = max(1, int(total))
        self.done = 0
        self._last = -1
        self.q.put(("section_progress", 0.0))

    def update(self, k: int = 1) -> None:
        self.done += k
        pct = 100.0 * self.done / self.total
        if int(pct) != self._last:
            self._last = int(pct)
            self.q.put(("section_progress", pct))


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
        # Serialise CZI decodes: pylibCZIrw's WIC/COM decoder is not re-entrant, so two
        # concurrent reads raised COM-ERROR 0x88982F8B. Every open_czi read holds this.
        self._czi_lock = threading.Lock()

        self.entry: preview.SectionEntry | None = None     # selected section
        self.disp_rgb: np.ndarray | None = None            # thumb/overview shown for ROI draw
        self.roi_rgb: np.ndarray | None = None             # the opened full-res ROI
        self.roi_px_um: float | None = None
        self.roi_rect: tuple[int, int, int, int] | None = None   # full-res (x,y,w,h)
        self.layers: dict | None = None
        # provenance of the result currently shown in the Result panel, so it can flag
        # when the view (section / ROI) has moved on since that result was computed.
        self._result_alias: str | None = None
        self._result_roi: tuple[int, int, int, int] | None = None
        self._result_scope: str = "ROI"
        self._disp_artist = None                           # thumbnail base AxesImage
        self._saved_roi_artist = None                      # static outline of a remembered ROI
        self._setting_extents = False                      # guard for programmatic clamp

        self.manual_auto = tk.BooleanVar(value=True)       # auto threshold on ROI
        self.manual_thr = tk.StringVar(value="")
        self.auto_recompute = tk.BooleanVar(value=True)    # debounced auto-recompute
        self.view_res = tk.StringVar()                     # px/µm label (supersedes gui.preview_hi_res)
        self._res_to_mult: dict[str, int] = {}             # label -> thumb multiplier
        self._loaded_um: float | None = None               # achieved µm/px of the shown image
        self._pending_view_frac: tuple | None = None       # carry the zoomed view across a res change
        self.sb_len = tk.StringVar(value="Auto")           # scale-bar length (µm)
        self.sb_label = tk.BooleanVar(value=True)
        self.sb_pos = tk.StringVar(value="bottom-right")
        self.sb_live = tk.BooleanVar(value=False)          # draw a live (non-burned) bar on the image
        self._sb_live_artist: dict = {"thumb": None, "roi": None}  # per-axis AnchoredSizeBar
        self.wb_on = tk.BooleanVar(value=False)            # raw <-> white-balanced display
        self._wb_cache: dict[str, tuple] = {}              # which -> (src_rgb, wb_rgb)
        self.show_vars: dict[str, tk.BooleanVar] = {}
        self.field_vars: dict[tuple[str, str], tk.Variable] = {}
        self._photo_refs: list[tk.PhotoImage] = []         # keep picker thumbs alive
        self._sections: list | None = None                 # cached SectionEntry objs (stable identity)
        # per-section memory (keyed by scene.key): the pending ROI rectangle and the
        # zoom/pan, so returning to a section restores both (like a resolution change).
        self._section_state: dict[str, dict] = {}
        self.order_mode = tk.StringVar(value=gw.SECTION_ORDER_MODES[0])
        self._picker = None                                # picker handle (marker + arrow nav)
        self._dirty = False
        self._params_dirty = False                         # tuning changed since last 'Export → config'
        self._excl_dirty = False                           # exclusion painted/cleared but not 'Save excl'-ed
        self._reflecting = False                           # suppress recompute while
        #                          reflecting the auto threshold back into the seed box

        # manual exclusion mask (preview-drawn, display-resolution uint8 0/255)
        self.brush_var = tk.StringVar(value="off")         # off | draw | erase
        self.brush_mode: str | None = None
        self.brush_size = tk.IntVar(value=18)
        self.excl_mask: np.ndarray | None = None
        self._excl_artist = None                           # exclusion overlay AxesImage
        self._excl_rgba: np.ndarray | None = None          # persistent overlay buffer
        self._brush_cursor = None                          # hover brush-outline patch
        self._painting = False

        for _v in (self.sb_pos, self.sb_len, self.sb_label, self.sb_live):
            _v.trace_add("write", lambda *_: self._refresh_live_scalebar_all())
        self._build_layout()
        self._populate_picker()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_queue)

    # -- layout ------------------------------------------------------------
    def _build_layout(self) -> None:
        # left: picker | center: notebook+status | right: tuning -- resizable via sashes.
        paned = tk.PanedWindow(self, orient="horizontal", sashrelief="raised", sashwidth=6)
        paned.pack(fill="both", expand=True)

        left = tk.Frame(paned)
        tk.Label(left, text="Sections", font=("Segoe UI", 9, "bold")).pack(pady=(6, 2))
        orow = tk.Frame(left)
        orow.pack(fill="x", padx=4)
        tk.Label(orow, text="order", font=("Segoe UI", 8)).pack(side="left")
        self._order_menu = ttk.OptionMenu(
            orow, self.order_mode, gw.SECTION_ORDER_MODES[0], *gw.SECTION_ORDER_MODES,
            command=lambda _v: self._populate_picker(refetch=False))
        self._order_menu.pack(side="left", fill="x", expand=True)
        gw.sync_order_menu_state(self._order_menu, self.out_dir)   # grey %SABG if no results.csv
        self.pick = gw.ScrollFrame(left)
        self.pick.pack(fill="both", expand=True)

        center = tk.Frame(paned)
        self.status = tk.Label(center, text="Pick a section, then Draw ROI.",
                               anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")
        self.nb = ttk.Notebook(center)
        self.nb.pack(fill="both", expand=True)

        right = tk.Frame(paned)
        self._build_tuning_panel(right)

        paned.add(left, minsize=120, width=190, stretch="never")
        paned.add(center, minsize=320, stretch="always")
        paned.add(right, minsize=280, width=360, stretch="never")

        self._build_thumb_tab()
        self._build_roi_tab()
        # Layers apply to the ROI overlay, so enable them only on the ROI tab.
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._set_layers_enabled(False)

    def _build_thumb_tab(self) -> None:
        tab = tk.Frame(self.nb)
        self.nb.add(tab, text="Thumbnail")
        # Two-row toolbar so nothing clips at the default width: row 1 = ROI buttons +
        # resolution, row 2 = shared view tools. Collapsible strips open BELOW the whole
        # container (after `barwrap`), not inside a single row.
        barwrap = tk.Frame(tab)
        barwrap.pack(fill="x")
        row1 = tk.Frame(barwrap)
        row1.pack(fill="x")
        row2 = tk.Frame(barwrap)
        row2.pack(fill="x")

        self.btn_draw_roi = tk.Button(row1, text="Draw ROI", command=self.on_draw_roi)
        self.btn_draw_roi.pack(side="left", padx=2, pady=2)
        self._roi_btn_bg = self.btn_draw_roi.cget("background")   # for the sticky toggle
        # Drawing no longer auto-opens: adjust the rectangle, then "Open ROI".
        self.btn_open_roi = tk.Button(row1, text="Open ROI", command=self.on_open_roi,
                                      state="disabled")
        self.btn_open_roi.pack(side="left", padx=2)
        self.btn_clear_roi = tk.Button(row1, text="Clear ROI", command=self.clear_roi,
                                       state="disabled")
        self.btn_clear_roi.pack(side="left", padx=2)
        tk.Label(row1, text="resolution").pack(side="left", padx=(10, 0))
        labels = self._res_labels()
        ttk.OptionMenu(row1, self.view_res, labels[0], *labels,
                       command=lambda _v: self._reload_display(preserve_view=True)).pack(side="left")
        self.res_label = tk.Label(row1, text="loaded: —", fg="#557", font=("Segoe UI", 8))
        self.res_label.pack(side="left", padx=(6, 0))

        # row 2: shared view tools + the exclusion-brush toggle (strips open below barwrap)
        self._add_shared_tools(row2, "thumb", strip_parent=tab, after_widget=barwrap)
        # manual exclusion brush in a collapsed strip (paint regions out of
        # numerator+denominator; rarely needed, e.g. muscle next to tumour)
        bar2 = self._collapsible_strip(row2, tab, "✏ exclusion", after_widget=barwrap)
        tk.Label(bar2, text="exclusion:").pack(side="left", padx=(2, 2))
        for txt, val in (("off", "off"), ("draw ✏", "draw"), ("erase ⌫", "erase")):
            tk.Radiobutton(bar2, text=txt, value=val, variable=self.brush_var,
                           command=self._on_brush_mode).pack(side="left")
        tk.Label(bar2, text="size").pack(side="left", padx=(8, 0))
        ttk.Scale(bar2, from_=2, to=60, variable=self.brush_size, length=90).pack(side="left")
        self.btn_clear_excl = tk.Button(bar2, text="Clear excl", command=self.on_clear_exclude)
        self.btn_clear_excl.pack(side="left", padx=(8, 2))
        self.btn_save_excl = tk.Button(bar2, text="Save excl", command=self.on_save_exclude)
        self.btn_save_excl.pack(side="left", padx=2)

        self.thumb_fig = Figure(figsize=(6, 6), tight_layout=True)
        self.thumb_ax = self.thumb_fig.add_subplot(111)
        self.thumb_ax.set_axis_off()
        self.thumb_canvas = FigureCanvasTkAgg(self.thumb_fig, master=tab)
        self.thumb_canvas.get_tk_widget().pack(fill="both", expand=True)
        # Left-drag pans the thumbnail UNLESS a ROI is being drawn or the brush is on.
        self.thumb_nav = CanvasNav(self.thumb_canvas, self.thumb_ax,
                                   can_left_pan=self._thumb_can_pan,
                                   on_view_change=lambda: self._refresh_live_scalebar("thumb"))
        # useblit + props => the rectangle renders live while dragging; interactive
        # handles (+ drag_from_anywhere) let it be resized/moved before opening.
        self.selector = RectangleSelector(
            self.thumb_ax, self._on_rect, useblit=True, button=[1],
            minspanx=5, minspany=5, spancoords="pixels", interactive=True,
            drag_from_anywhere=True,
            props=dict(facecolor="orange", edgecolor="black", alpha=0.25, fill=True),
            handle_props=dict(markeredgecolor="black", markerfacecolor="white",
                              markersize=7))
        self.selector.set_active(False)
        # brush painting uses left-drag (only when a brush mode is active); a hover
        # outline shows the brush footprint and is hidden when the pointer leaves.
        self.thumb_canvas.mpl_connect("button_press_event", self._on_brush_press)
        self.thumb_canvas.mpl_connect("motion_notify_event", self._on_brush_drag)
        self.thumb_canvas.mpl_connect("button_release_event", self._on_brush_release)
        self.thumb_canvas.mpl_connect("axes_leave_event", self._on_brush_leave)
        self.thumb_canvas.mpl_connect("figure_leave_event", self._on_brush_leave)

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
        self.roi_nav = CanvasNav(self.roi_canvas, self.roi_ax,
                                 on_view_change=lambda: self._refresh_live_scalebar("roi"))
        self._roi_hint()
        self.nb.tab(self.roi_tab, state="disabled")    # greyed until a ROI is opened

    # -- shared toolbar / scale-bar preview / white balance ----------------
    def _nav(self, source: str):
        return self.thumb_nav if source == "thumb" else self.roi_nav

    def _thumb_can_pan(self) -> bool:
        """Left-drag pans the thumbnail unless a ROI is being drawn / brush is on."""
        sel = getattr(self, "selector", None)
        return self.brush_mode is None and not (sel is not None and sel.get_active())

    def _collapsible_strip(self, btn_parent: tk.Frame, strip_parent: tk.Frame,
                           label: str, after_widget: tk.Widget | None = None) -> tk.Frame:
        """A horizontal strip (a child of *strip_parent*, packed after *after_widget*
        when expanded) toggled by a button on *btn_parent*. Collapsed by default — for
        rarely-used controls (scale bar, exclusion brush). *after_widget* defaults to
        *btn_parent* so the strip opens just below it; pass the toolbar container to open
        below a multi-row toolbar."""
        strip = tk.Frame(strip_parent)
        after = after_widget if after_widget is not None else btn_parent
        btn = tk.Button(btn_parent, relief="groove")

        def toggle():
            if strip.winfo_manager():
                strip.pack_forget()
                btn.configure(text=f"{label} ▸")
            else:
                strip.pack(fill="x", after=after)
                btn.configure(text=f"{label} ▾")

        btn.configure(text=f"{label} ▸", command=toggle)
        btn.pack(side="left", padx=(8, 2))
        return strip

    def _add_shared_tools(self, bar: tk.Frame, source: str, *,
                          strip_parent: tk.Frame | None = None,
                          after_widget: tk.Widget | None = None) -> None:
        """Controls present on BOTH tabs: Reset view, white-balance toggle, a
        collapsible scale-bar strip (length/label/corner + 'on image' live toggle),
        Export, Help.

        *strip_parent*/*after_widget* place the collapsible scale-bar strip; they
        default to opening just below *bar* (single-row ROI toolbar). The thumbnail tab
        passes its 2-row container so the strip opens below both rows."""
        if strip_parent is None:
            strip_parent = bar.master
        if after_widget is None:
            after_widget = bar
        tk.Button(bar, text="Reset view",
                  command=lambda: self._nav(source).reset()).pack(side="left", padx=(10, 2))
        tk.Checkbutton(bar, text="white-balanced", variable=self.wb_on,
                       command=self._on_wb_toggle).pack(side="left", padx=(8, 2))
        # scale-bar controls live in a collapsed strip (rarely changed)
        sb = self._collapsible_strip(bar, strip_parent, "⚖ scale bar",
                                     after_widget=after_widget)
        tk.Label(sb, text="bar").pack(side="left", padx=(8, 0))
        opts = self._sb_len_options()
        if self.sb_len.get() not in opts:        # keep the var on a valid option
            self.sb_len.set(opts[0])
        ttk.OptionMenu(sb, self.sb_len, self.sb_len.get(), *opts).pack(side="left")
        tk.Checkbutton(sb, text="label", variable=self.sb_label).pack(side="left")
        ttk.OptionMenu(sb, self.sb_pos, self.sb_pos.get(),
                       *_SB_POS.keys()).pack(side="left")
        # Live (non-burned) bar drawn on the image as-is: follows the length/label/corner
        # above, repositions on pan, rescales on zoom. Export still burns its own bar in.
        tk.Checkbutton(sb, text="on image", variable=self.sb_live).pack(side="left", padx=(8, 0))
        tk.Button(bar, text="Export…",
                  command=lambda: self.on_export_image(source)).pack(side="left", padx=(8, 2))
        tk.Button(bar, text="?", width=2, command=self._show_help).pack(side="right", padx=2)

    def _refresh_live_scalebar_all(self) -> None:
        """Refresh the live scale bar on both axes (e.g. after a length/label/corner change)."""
        self._refresh_live_scalebar("thumb")
        self._refresh_live_scalebar("roi")

    def _sb_len_options(self) -> list[str]:
        """The "bar" dropdown options: 'Auto' + the configured presets as mm/µm labels."""
        vals = getattr(self.cfg.gui, "scalebar_values", None) or [1000, 500, 200, 100, 50]
        opts = ["Auto"]
        for v in vals:
            try:
                opts.append(_fmt_bar_um(float(v)))
            except (ValueError, TypeError):
                continue
        return opts

    def _sb_len_um(self):
        """Selected scale-bar length as 'Auto' or a float µm, parsing the mm/µm label."""
        sel = self.sb_len.get()
        if sel in ("", "Auto"):
            return "Auto"
        parts = sel.split()
        try:
            v = float(parts[0])
        except (ValueError, IndexError):
            return "Auto"
        return v * 1000.0 if (len(parts) > 1 and parts[1] == "mm") else v

    def _live_bar_um(self, source: str, px_um: float, ax) -> float:
        """The bar length in µm for *source*: the chosen fixed length, or — for 'Auto' —
        the largest configured preset that fits the CURRENT view (so Auto spans the whole
        gui.scalebar_values range as you zoom, not just ~1 mm / 50 µm)."""
        sel = self._sb_len_um()
        if sel != "Auto":
            return float(sel)
        x0, x1 = ax.get_xlim()
        visible_um = max(1.0, abs(x1 - x0)) * px_um
        max_um = 0.4 * visible_um                          # bar at most ~40% of the view width
        vals = sorted((float(v) for v in (self.cfg.gui.scalebar_values or [])
                       if float(v) > 0), reverse=True)
        if not vals:                                       # no presets configured -> fall back
            return export.adaptive_bar_um(
                max(1, int(round(abs(x1 - x0)))), px_um,
                target_um=(1000.0 if source == "thumb" else 200.0))
        for v in vals:
            if v <= max_um:
                return v
        return vals[-1]                                    # very zoomed in: smallest preset

    def _refresh_live_scalebar(self, source: str) -> None:
        """(Re)draw or clear the live (non-burned) scale bar on the thumb or ROI axis.

        Length is set in DATA units (µm ÷ µm/px), so matplotlib rescales it on zoom; the
        bar is anchored to a corner (``loc``), so it stays put on pan; thickness is a fixed
        fraction of the visible height, so it reads ~constant on screen across zoom."""
        ax = self.thumb_ax if source == "thumb" else self.roi_ax
        canvas = self.thumb_canvas if source == "thumb" else self.roi_canvas
        old = self._sb_live_artist.get(source)
        if old is not None:
            try:
                old.remove()
            except Exception:
                pass
            self._sb_live_artist[source] = None
        px_um = self._loaded_um if source == "thumb" else self.roi_px_um
        has_img = (self.disp_rgb is not None) if source == "thumb" else (self.roi_rgb is not None)
        if not self.sb_live.get() or not px_um or not has_img:
            canvas.draw_idle()
            return
        bar_um = self._live_bar_um(source, px_um, ax)
        size_data = bar_um / px_um
        y0, y1 = ax.get_ylim()
        size_vertical = max(abs(y1 - y0) * 0.006, size_data * 1e-3)
        # White backing plate only when a label is drawn; a bare bar shows no square.
        label_on = bool(self.sb_label.get())
        label = _fmt_bar_um(bar_um) if label_on else ""
        loc = _SB_LOC.get(_SB_POS.get(self.sb_pos.get(), "br"), "lower right")
        bar = AnchoredSizeBar(
            ax.transData, size_data, label, loc, pad=0.3, borderpad=0.5, sep=3,
            color="black", frameon=label_on, size_vertical=size_vertical,
            label_top=True, fontproperties=FontProperties(size=8))
        if label_on:
            bar.patch.set(facecolor="white", edgecolor="none", alpha=0.7)
        ax.add_artist(bar)
        self._sb_live_artist[source] = bar
        canvas.draw_idle()

    def _wb(self, rgb: np.ndarray, which: str) -> np.ndarray:
        """White-balanced copy of *rgb* (cached per *which* by image identity)."""
        c = self._wb_cache.get(which)
        if c is not None and c[0] is rgb:
            return c[1]
        wbp = self.cfg.whitebalance
        out = whitebalance.white_balance(
            rgb, whitebalance.estimate_white_point(rgb, wbp.bright_frac),
            target=wbp.target)
        self._wb_cache[which] = (rgb, out)
        return out

    def _on_wb_toggle(self) -> None:
        """Apply/undo the display white balance on BOTH the thumbnail and the ROI,
        without clearing the thumb axes (preserves zoom/pan + the exclusion overlay).
        Skipped mid-stroke so painting isn't interrupted."""
        if self._painting:
            return
        wb = self.wb_on.get()
        if self.disp_rgb is not None and self._disp_artist is not None:
            self._disp_artist.set_data(
                self._wb(self.disp_rgb, "disp") if wb else self.disp_rgb)
            self.thumb_canvas.draw_idle()
        if self.roi_rgb is not None and self.layers is not None:
            self._redraw()

    # -- layers panel gating (ROI tab only) --------------------------------
    def _iter_descendants(self, w):
        for c in w.winfo_children():
            yield c
            yield from self._iter_descendants(c)

    def _set_layers_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in self._iter_descendants(self._layers_frame):
            try:
                w.configure(state=state)
            except tk.TclError:
                pass

    def _on_tab_changed(self, _evt=None) -> None:
        try:
            on_roi = self.nb.index(self.nb.select()) == self.nb.index(self.roi_tab)
        except tk.TclError:
            on_roi = False
        self._set_layers_enabled(on_roi)

    # -- whole-section compute (real analyze_scene) ------------------------
    def on_compute_section(self) -> None:
        if self.entry is None:
            messagebox.showinfo("Compute section", "Pick a section first.", parent=self)
            return
        if self._busy:
            messagebox.showinfo("Compute section", "Busy — wait for the current task.",
                                parent=self)
            return
        if not messagebox.askyesno(
                "Compute whole section",
                "Run the FULL analysis for this section?\n\n"
                "It streams the whole section at full resolution (this can take "
                "minutes), writes its maps, and caches the result so Analyze can "
                "skip it while the config is unchanged.\n\nContinue?", parent=self):
            return
        self.status.configure(text=f"{self.entry.alias}: computing whole section… 0%")
        self._submit(self._compute_section, self.entry.scene, self._snapshot_cfg(),
                     self.entry.alias, tag="section")

    def _compute_section(self, scene, cfg, alias):
        import pylibCZIrw.czi as pyczi
        from sabg_analyzer import pipeline
        prog = _GuiProgress(self.q)
        with self._czi_lock:                       # serialise CZI decodes (see __init__)
            with pyczi.open_czi(scene.path) as doc:
                row = pipeline.analyze_scene(doc, scene, cfg, Path(self.out_dir),
                                             alias=alias, progress=prog)
        pipeline._write_cache(Path(self.out_dir), scene, cfg, row)  # skip-on-analyze
        return ("section", row, scene.pixel_size_um)

    def _show_section_stats(self, row: dict, px_um) -> None:
        thr_s = row.get("threshold_secondary")
        lines = [
            f"SECTION  %SABG = {row.get('pct_sabg', 0)}",
            f"thr {row.get('threshold')}" + (f"   2nd {thr_s}" if thr_s else ""),
            f"SABG+  {int(row.get('positive_px', 0)):>12,} px  "
            f"{(row.get('sabg_area_mm2') or 0):.4f} mm²",
            f"tissue {int(row.get('tissue_px', 0)):>12,} px  "
            f"{(row.get('tissue_area_mm2') or 0):.3f} mm²",
            f"fold {int(row.get('fold_px', 0)):,}  "
            f"artifact {int(row.get('artifact_px', 0)):,}  "
            f"edge {int(row.get('edge_px', 0)):,} px",
        ]
        self.stats_label.configure(text="\n".join(lines))
        self._record_result_provenance("section")

    # -- result provenance / staleness -------------------------------------
    @staticmethod
    def _roi_desc(rect: tuple[int, int, int, int] | None) -> str:
        return f"ROI {rect[2]}×{rect[3]}" if rect else "no ROI"

    def _record_result_provenance(self, scope: str) -> None:
        """Stamp the just-shown result with the section + ROI it came from."""
        self._result_alias = self.entry.alias if self.entry else None
        self._result_roi = self.roi_rect
        self._result_scope = scope
        self._update_result_provenance()

    def _update_result_provenance(self) -> None:
        """Refresh the Result provenance line; amber when the view has moved on."""
        if not hasattr(self, "result_meta"):
            return
        if self._result_alias is None:
            self.result_meta.configure(text="(no result yet)", fg="#888")
            return
        src = self._roi_desc(self._result_roi) if self._result_scope == "ROI" else "whole section"
        cur_alias = self.entry.alias if self.entry else None
        stale = (cur_alias != self._result_alias) or (
            self._result_scope == "ROI" and self.roi_rect != self._result_roi)
        if stale:
            self.result_meta.configure(
                text=f"◑ result: {self._result_alias} · {src} — view changed (recompute)",
                fg="#c8862a")
        else:
            self.result_meta.configure(
                text=f"● showing {self._result_alias} · {src}", fg="#3a7d44")

    # -- manual exclusion brush --------------------------------------------
    def _excl_path(self) -> Path | None:
        """Where this section's exclusion mask lives (cfg ref or default slot)."""
        if self.entry is None:
            return None
        rel = self.cfg.scene_exclude_mask(self.entry.scene.key) \
            or f"exclude/{self.entry.scene.slug}.png"
        return Path(self.out_dir) / rel

    def _load_saved_excl(self) -> np.ndarray | None:
        p = self._excl_path()
        if p and p.exists():
            m = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if m is not None:
                return m > 127
        return None

    def _on_brush_mode(self) -> None:
        mode = self.brush_var.get()
        self.brush_mode = None if mode == "off" else mode
        # brush, ROI-draw and left-pan all use the left button -> brushing is
        # exclusive: it turns the rectangle selector off (pan resumes when off).
        if self.brush_mode is not None:
            self._set_draw_active(False)
        else:
            self._hide_brush_cursor()

    def _on_brush_press(self, e) -> None:
        if self.brush_mode is None or e.inaxes is not self.thumb_ax or e.xdata is None:
            return
        self._painting = True
        self._paint_at(e.xdata, e.ydata)

    def _on_brush_drag(self, e) -> None:
        if self.brush_mode is None:
            return
        if e.inaxes is self.thumb_ax and e.xdata is not None:
            self._update_brush_cursor(e.xdata, e.ydata)
            if self._painting:
                self._paint_at(e.xdata, e.ydata)
        else:
            self._hide_brush_cursor()

    def _on_brush_release(self, _e) -> None:
        if self._painting:
            self._painting = False
            self._update_excl_buttons()

    def _on_brush_leave(self, _e) -> None:
        self._hide_brush_cursor()

    def _update_brush_cursor(self, x: float, y: float) -> None:
        """Show a dashed outline circle at the pointer, sized to the brush (so the user
        sees what will be painted/erased). Magenta for draw, black for erase."""
        r = max(1, int(self.brush_size.get()))
        edge = "black" if self.brush_mode == "erase" else "magenta"
        if self._brush_cursor is None:
            self._brush_cursor = Circle((x, y), r, fill=False, edgecolor=edge,
                                        linewidth=1.2, linestyle="--", zorder=10)
            self.thumb_ax.add_patch(self._brush_cursor)
        else:
            self._brush_cursor.center = (x, y)
            self._brush_cursor.set_radius(r)
            self._brush_cursor.set_edgecolor(edge)
            self._brush_cursor.set_visible(True)
        self.thumb_canvas.draw_idle()

    def _hide_brush_cursor(self) -> None:
        if self._brush_cursor is not None and self._brush_cursor.get_visible():
            self._brush_cursor.set_visible(False)
            self.thumb_canvas.draw_idle()

    def _update_excl_buttons(self) -> None:
        """Enable Clear/Save exclusion only when something is painted (mirrors
        `_update_roi_buttons`)."""
        has = self.excl_mask is not None and bool((self.excl_mask > 0).any())
        for b in (getattr(self, "btn_clear_excl", None),
                  getattr(self, "btn_save_excl", None)):
            if b is not None:
                b.configure(state="normal" if has else "disabled")

    def _excl_color(self) -> tuple[float, float, float, float]:
        """The exclusion overlay RGBA (0-1) from cfg.overlay.excluded_color/alpha."""
        col = self.cfg.overlay.excluded_color
        return (col[0] / 255.0, col[1] / 255.0, col[2] / 255.0,
                float(self.cfg.overlay.excluded_alpha))

    def _paint_at(self, x: float, y: float) -> None:
        if self.excl_mask is None:
            return
        r = max(1, int(self.brush_size.get()))
        val = 255 if self.brush_mode == "draw" else 0
        cx, cy = int(round(x)), int(round(y))
        cv2.circle(self.excl_mask, (cx, cy), r, val, -1)
        self._excl_dirty = True              # painted but not yet 'Save excl'-ed
        # Per-stroke: update ONLY the stamp's bounding box in the persistent RGBA buffer
        # (was rebuilding a full H×W×4 float every motion → slow on large thumbs).
        if self._excl_rgba is None or self._excl_artist is None:
            self._refresh_excl_overlay()
            return
        h, w = self.excl_mask.shape
        x0, x1 = max(0, cx - r - 1), min(w, cx + r + 2)
        y0, y1 = max(0, cy - r - 1), min(h, cy + r + 2)
        if x1 <= x0 or y1 <= y0:
            return
        region = self._excl_rgba[y0:y1, x0:x1]
        region[...] = 0.0
        region[self.excl_mask[y0:y1, x0:x1] > 0] = self._excl_color()
        self._excl_artist.set_data(self._excl_rgba)
        self.thumb_canvas.draw_idle()

    def _refresh_excl_overlay(self) -> None:
        """Full rebuild of the exclusion overlay (after load/clear/section switch),
        using the same magenta as the 'excluded' layer (cfg.overlay.excluded_color/
        alpha). Per-stroke painting updates only the changed region (see _paint_at)."""
        if self.excl_mask is None:
            return
        rgba = np.zeros((*self.excl_mask.shape, 4), np.float32)
        rgba[self.excl_mask > 0] = self._excl_color()
        self._excl_rgba = rgba
        if self._excl_artist is None:
            self._excl_artist = self.thumb_ax.imshow(rgba, interpolation="nearest")
        else:
            self._excl_artist.set_data(rgba)
        self.thumb_canvas.draw_idle()

    def on_clear_exclude(self) -> None:
        if self.excl_mask is not None:
            if (self.excl_mask > 0).any():
                self._excl_dirty = True      # clearing a non-empty mask is an unsaved change
            self.excl_mask[:] = 0
            self._refresh_excl_overlay()
        self._update_excl_buttons()
        self.status.configure(text="exclusion cleared (Save excl to persist)")

    def on_save_exclude(self) -> None:
        """Write the mask to out/exclude/<slug>.png and point cfg.scenes at it.

        An empty mask removes the file + the cfg reference. Persisted to config.yaml
        by 'Export → config' (consistent with the other tuned settings)."""
        if self.entry is None or self.excl_mask is None:
            return
        self._excl_dirty = False             # both branches below persist the current state
        key = self.entry.scene.key
        rel = f"exclude/{self.entry.scene.slug}.png"
        p = Path(self.out_dir) / rel
        if not (self.excl_mask > 0).any():
            if p.exists():
                p.unlink()
            self.cfg.scenes.get(key, {}).pop("exclude_mask", None)
            self._update_excl_buttons()
            self.status.configure(text="exclusion mask empty — removed")
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(p), self.excl_mask)
        self.cfg.scenes.setdefault(key, {})["exclude_mask"] = rel
        self._mark_dirty()
        self._update_excl_buttons()
        self.status.configure(
            text=f"saved exclusion mask → {p.name}  (Export → config to persist)")
        messagebox.showinfo(
            "Exclusion saved",
            f"Wrote {p.name}.\nThe section's config now points at it; click "
            "'Export → config' to persist the link for the batch run.", parent=self)

    def _roi_exclude_crop(self) -> np.ndarray | None:
        """The exclusion mask cropped + scaled to the open ROI (bool), or None."""
        if (self.excl_mask is None or self.roi_rect is None or self.roi_rgb is None
                or not (self.excl_mask > 0).any()):
            return None
        sc = self.entry.scene
        mh, mw = self.excl_mask.shape
        x, y, w, h = self.roi_rect                    # global full-res scene coords
        mx0 = int(round((x - sc.x) / max(sc.w, 1) * mw))
        mx1 = int(round((x - sc.x + w) / max(sc.w, 1) * mw))
        my0 = int(round((y - sc.y) / max(sc.h, 1) * mh))
        my1 = int(round((y - sc.y + h) / max(sc.h, 1) * mh))
        mx0, mx1 = max(0, mx0), min(mw, max(mx0 + 1, mx1))
        my0, my1 = max(0, my0), min(mh, max(my0 + 1, my1))
        crop = self.excl_mask[my0:my1, mx0:mx1]
        if crop.size == 0:
            return None
        rh, rw = self.roi_rgb.shape[:2]
        return cv2.resize(crop, (rw, rh), interpolation=cv2.INTER_NEAREST) > 127

    def _roi_hint(self) -> None:
        self.roi_ax.clear()
        self._sb_live_artist["roi"] = None     # live scale bar cleared with the axes
        self.roi_ax.set_axis_off()
        self.roi_ax.text(0.5, 0.5, "Draw a ROI on the Thumbnail tab,\nthen 'Open ROI'.",
                         ha="center", va="center", fontsize=10, color="#888")
        self.roi_canvas.draw_idle()

    def _build_tuning_panel(self, parent: tk.Frame) -> None:
        sf = gw.ScrollFrame(parent)
        sf.pack(fill="both", expand=True)
        body = sf.interior

        # top controls -- row 1: Recompute + auto + dirty dot; row 2: Export → config.
        top = tk.Frame(body, padx=6, pady=4)
        top.pack(fill="x")
        row1 = tk.Frame(top)
        row1.pack(fill="x")
        self.btn_recompute = tk.Button(row1, text="↻  Recompute", font=("Segoe UI", 10, "bold"),
                                       bg="#3a7d44", fg="white", activebackground="#2f6638",
                                       command=self.request_recompute, state="disabled")
        self.btn_recompute.pack(side="left", fill="x", expand=True)
        tk.Checkbutton(row1, text="auto", variable=self.auto_recompute,
                       command=self._on_auto_toggle).pack(side="left", padx=4)
        self.dirty_dot = tk.Label(row1, text="✓ up to date", fg="#3a7d44",
                                  font=("Segoe UI", 9, "bold"))
        self.dirty_dot.pack(side="left", padx=4)
        # row 2: the two batch/config actions share one line to save vertical space.
        row2 = tk.Frame(top)
        row2.pack(fill="x", pady=(4, 0))
        tk.Button(row2, text="Export → config", command=self.on_export).pack(
            side="left", fill="x", expand=True)
        tk.Button(row2, text="▣  Compute whole section…",
                  command=self.on_compute_section).pack(side="left", fill="x", expand=True,
                                                        padx=(4, 0))

        # result characteristics (filled after each recompute / whole-section run)
        res = tk.LabelFrame(body, text="Result", padx=6, pady=2)
        res.pack(fill="x", padx=6, pady=2)
        self.stats_label = tk.Label(res, text="(recompute to see %SABG, thresholds, areas)",
                                    anchor="w", justify="left", font=("Consolas", 9),
                                    fg="#333")
        self.stats_label.pack(fill="x")
        # provenance / staleness line: which section + ROI this result came from, and
        # whether the current view has moved on since (the Result can lag the view).
        self.result_meta = tk.Label(res, text="(no result yet)", anchor="w", justify="left",
                                    font=("Segoe UI", 8), fg="#888")
        self.result_meta.pack(fill="x")

        # layers panel (show / colour / alpha per layer); enabled on the ROI tab only
        lay = tk.LabelFrame(body, text="Layers (ROI overlay)", padx=6, pady=2)
        lay.pack(fill="x", padx=6, pady=2)
        self._layers_frame = lay
        gw.build_layers_panel(lay, self.cfg, self.show_vars, self._redraw)

        # Detection stages: each ALWAYS open with one composite sensitivity slider
        # (left = detect less, right = more), the raw parameters behind a per-section
        # "details" expander. The per-ROI seed threshold lives in the SABG section.
        gw.build_detection_sections(
            body, self.cfg, self.field_vars, self._on_field, recompute=True,
            section_extra={"3. SABG detection": self._build_seed_controls})

    def _build_seed_controls(self, parent: tk.Frame) -> None:
        """Per-ROI seed-threshold controls, injected into "3. SABG detection". Auto
        (default) estimates the seed on the current ROI's tissue; untick to type a
        manual seed (persisted as scenes.<key>.threshold on Export → config)."""
        tk.Checkbutton(parent, text="Seed: Auto on ROI", variable=self.manual_auto,
                       command=self._on_manual_toggle).grid(row=0, column=0, sticky="w")
        tk.Label(parent, text="manual:").grid(row=0, column=1, sticky="e", padx=(8, 2))
        self.manual_entry = tk.Entry(parent, textvariable=self.manual_thr, width=10,
                                     state="disabled")
        self.manual_entry.grid(row=0, column=2, sticky="w")
        self.manual_thr.trace_add("write", lambda *_: self._on_manual_seed_edit())

    # -- picker ------------------------------------------------------------
    def _populate_picker(self, refetch: bool = True) -> None:
        if refetch or self._sections is None:
            try:
                self._sections = preview.list_sections(self.data_dir, self.out_dir, self.cfg)
            except Exception as exc:
                self._sections = None
                self._picker = None
                tk.Label(self.pick.interior, text=f"(error: {exc})", wraplength=160,
                         fg="red").pack()
                return
        for w in self.pick.interior.winfo_children():     # rebuild (first build / order change)
            w.destroy()
        self._photo_refs.clear()
        ordered = gw.order_sections(self._sections, self.order_mode.get(), self.out_dir)
        self._picker = gw.thumbnail_picker(
            self.pick.interior, ordered, self._select_section, self._photo_refs,
            selected=self.entry)
        if getattr(self, "_order_menu", None) is not None:
            gw.sync_order_menu_state(self._order_menu, self.out_dir)

    # -- section / ROI -----------------------------------------------------
    def _select_section(self, entry: preview.SectionEntry) -> None:
        if entry is not self.entry and not self._confirm_discard_exclusion("switch sections"):
            return                            # keep the current section + its unsaved mask
        # Remember the outgoing section's pending ROI + view before we tear it down,
        # so a later return restores them (captured here while disp_rgb/axes are live).
        if self.entry is not None:
            self._section_state[self.entry.scene.key] = {
                "roi_rect": self.roi_rect,
                "view_frac": self._capture_view_frac(),
            }
        self.entry = entry
        if self._picker is not None:
            self._picker.highlight(entry)     # mark the current section in the list
        self.clear_roi(refresh=False)         # tears down the old ROI before the reload
        saved = self._section_state.get(entry.scene.key, {})
        self.roi_rect = saved.get("roi_rect")     # restore the remembered rectangle (if any)
        self.excl_mask = None                 # reload the section's mask (or blank)
        self._excl_dirty = False              # a freshly loaded section starts clean
        self._excl_artist = None
        self.brush_var.set("off")
        self.brush_mode = None
        self.status.configure(text=f"{entry.alias}: loading view…")
        self.nb.select(0)
        self._reload_display(view_frac=saved.get("view_frac"))
        self._update_result_provenance()      # result now lags the new section

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

    def _reload_display(self, preserve_view: bool = False, view_frac=None) -> None:
        """(Re)load the section's display image for ROI drawing (thumb or finer).

        *preserve_view* (a resolution change) keeps the user looking at the same area:
        the current zoom is captured as fractions of the image and restored after the
        new image loads (both span the whole scene bbox). *view_frac* (a section switch)
        supplies a previously-remembered view for this section directly; it wins over
        *preserve_view*. Neither set => full extent.
        """
        if self.entry is None:
            return
        self._pending_view_frac = (
            view_frac if view_frac is not None
            else (self._capture_view_frac() if preserve_view else None))
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
        with self._czi_lock:                       # serialise CZI decodes (see __init__)
            with pyczi.open_czi(scene.path) as doc:
                rgb, _ = preview.czi_io.read_overview(
                    doc, scene, max_edge=edge, zoom_cap=1.0, um_per_px=um)
        return ("display", rgb)

    def _show_display(self, rgb: np.ndarray) -> None:
        self.disp_rgb = rgb
        self._wb_cache.pop("disp", None)         # this image's WB is (re)computed below
        self.thumb_ax.clear()
        self._excl_artist = None                 # cleared with the axes
        self._excl_rgba = None                   # rebuilt by _refresh_excl_overlay below
        self._brush_cursor = None                # patch removed with the axes clear
        self._saved_roi_artist = None            # remembered-ROI outline (redrawn below)
        self._sb_live_artist["thumb"] = None     # live scale bar cleared with the axes
        self.thumb_ax.set_axis_off()
        self._disp_artist = self.thumb_ax.imshow(
            self._wb(rgb, "disp") if self.wb_on.get() else rgb)
        # exclusion mask follows the display resolution (load saved on a fresh section)
        h, w = rgb.shape[:2]
        if self.excl_mask is None:
            saved = self._load_saved_excl()
            self.excl_mask = (cv2.resize(saved.astype(np.uint8) * 255, (w, h),
                                         interpolation=cv2.INTER_NEAREST)
                              if saved is not None else np.zeros((h, w), np.uint8))
        elif self.excl_mask.shape != (h, w):
            self.excl_mask = cv2.resize(self.excl_mask, (w, h),
                                        interpolation=cv2.INTER_NEAREST)
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
        # Hide the ROI selector's drawn rectangle unless it's actively being edited, so a
        # rectangle drawn on one thumb doesn't leak onto another (set_active(False) only stops
        # event handling — it never hides the rectangle; mirrors the set_visible(True) in
        # _sync_selector_to_rect). A remembered rect still shows via _draw_saved_roi_outline.
        if not self.selector.get_active() and hasattr(self.selector, "set_visible"):
            self.selector.set_visible(False)
        self.thumb_canvas.draw_idle()
        self.thumb_nav.set_home()
        self._restore_pending_view()           # re-zoom to the carried area after a res change
        # A remembered (but not-yet-opened) ROI shows as a static outline so it's "still
        # there" on return; clicking Draw ROI then turns it into the editable selector.
        self._draw_saved_roi_outline()
        # Selector stays inactive here (left-drag pans by default); activation is
        # owned by Draw/Open/Clear ROI + the brush, not by reloading the display.
        self._refresh_excl_overlay()
        self._update_excl_buttons()
        self._update_roi_buttons()             # a remembered rect re-enables Open/Clear ROI
        self._refresh_live_scalebar("thumb")   # redraw the live bar over the fresh image
        if self.roi_rgb is None:
            if self.roi_rect is not None:
                self.status.configure(
                    text=f"{self.entry.alias}: remembered ROI shown — 'Draw ROI' to edit, "
                         "'Open ROI' to read it, 'Clear ROI' to drop it.")
            else:
                self.status.configure(
                    text=f"{self.entry.alias}: drag to pan; 'Draw ROI' to select a region.")

    def _capture_view_frac(self) -> tuple | None:
        """Current thumbnail view as (fx0, fx1, fy0, fy1) fractions of the image
        extent, or None if nothing is zoomable yet. Resolution-independent, so it
        transfers across a reload at a different µm/px."""
        if self.disp_rgb is None:
            return None
        try:
            x0, x1 = self.thumb_ax.get_xlim()
            y0, y1 = self.thumb_ax.get_ylim()
        except Exception:
            return None
        h, w = self.disp_rgb.shape[:2]
        if not w or not h:
            return None
        # imshow extent is [-0.5, dim-0.5]; map lims to [0, 1] across that extent.
        return ((x0 + 0.5) / w, (x1 + 0.5) / w,
                (y0 + 0.5) / h, (y1 + 0.5) / h)

    def _restore_pending_view(self) -> None:
        """Apply a view fraction captured before a resolution change to the freshly
        loaded image, then clear it. No-op when nothing was carried."""
        frac = self._pending_view_frac
        self._pending_view_frac = None
        if frac is None or self.disp_rgb is None:
            return
        fx0, fx1, fy0, fy1 = frac
        h, w = self.disp_rgb.shape[:2]
        try:
            self.thumb_ax.set_xlim(fx0 * w - 0.5, fx1 * w - 0.5)
            self.thumb_ax.set_ylim(fy0 * h - 0.5, fy1 * h - 0.5)
            self.thumb_canvas.draw_idle()
        except Exception:
            pass

    def _set_draw_active(self, active: bool) -> None:
        """Activate/deactivate the ROI rectangle selector and reflect it on the sticky
        'Draw ROI' button (sunken + tinted while drawing, raised otherwise)."""
        sel = getattr(self, "selector", None)
        if sel is not None:
            sel.set_active(active)
        btn = getattr(self, "btn_draw_roi", None)
        if btn is not None:
            btn.configure(relief="sunken" if active else "raised",
                          bg="#cfe3ff" if active else self._roi_btn_bg)

    def on_draw_roi(self) -> None:
        if self.entry is None:
            return
        if self.selector.get_active():         # sticky: a second click returns to pan
            self._set_draw_active(False)
            self.status.configure(
                text=f"{self.entry.alias}: drag to pan; 'Draw ROI' to select a region.")
            return
        if self.roi_rgb is not None:           # an opened ROI -> start a fresh one
            self.clear_roi()
        self.brush_var.set("off")              # drawing is exclusive with the brush
        self.brush_mode = None
        self.nb.select(0)
        self._set_draw_active(True)
        self._clear_saved_roi_outline()        # the editable selector replaces the static one
        if self.roi_rect is not None and self.disp_rgb is not None:
            # Re-activating with a still-pending rectangle: keep it and let the user
            # edit it (don't erase), re-seeding the on-screen handles from the stored rect.
            self._sync_selector_to_rect()
            self.status.configure(
                text="edit the rectangle (drag the handles), then 'Open ROI'.")
        else:
            self.roi_rect = None
            self.status.configure(
                text="drag a rectangle (adjust the handles, then 'Open ROI').")
        self._update_roi_buttons()

    def _rect_to_extents(self, rect: tuple[int, int, int, int]) -> tuple:
        """Full-res (x, y, w, h) -> on-screen selector extents in display pixels."""
        x, y, ww, hh = rect
        h, w = self.disp_rgb.shape[:2]
        sc = self.entry.scene
        tx0 = (x - sc.x) * w / max(sc.w, 1)
        ty0 = (y - sc.y) * h / max(sc.h, 1)
        return (tx0, tx0 + ww * w / max(sc.w, 1),
                ty0, ty0 + hh * h / max(sc.h, 1))

    def _sync_selector_to_rect(self) -> None:
        """Reflect the stored full-res ``roi_rect`` onto the on-screen selector so a
        re-activated Draw ROI edits the existing rectangle instead of erasing it."""
        if self.roi_rect is None or self.disp_rgb is None or self.entry is None:
            return
        self._setting_extents = True
        try:
            self.selector.extents = self._rect_to_extents(self.roi_rect)
            if hasattr(self.selector, "set_visible"):
                self.selector.set_visible(True)
        finally:
            self._setting_extents = False
        self.thumb_canvas.draw_idle()

    def _draw_saved_roi_outline(self) -> None:
        """Draw a static dashed outline of a remembered (not-yet-opened) ROI rectangle
        so it stays visible after a section round-trip. Skipped while the editable
        selector is active (Draw ROI owns the display then) or once a ROI is opened."""
        if (self.roi_rect is None or self.disp_rgb is None or self.entry is None
                or self.roi_rgb is not None or self.selector.get_active()):
            return
        x0, x1, y0, y1 = self._rect_to_extents(self.roi_rect)
        self._saved_roi_artist = Rectangle(
            (x0, y0), x1 - x0, y1 - y0, fill=False, edgecolor="orange",
            linewidth=1.5, linestyle="--")
        self.thumb_ax.add_patch(self._saved_roi_artist)
        self.thumb_canvas.draw_idle()

    def _clear_saved_roi_outline(self) -> None:
        """Remove the static remembered-ROI outline (e.g. when Draw ROI takes over)."""
        if self._saved_roi_artist is not None:
            try:
                self._saved_roi_artist.remove()
            except Exception:
                pass
            self._saved_roi_artist = None

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
        self._setting_extents = True
        try:
            self.selector.extents = self._rect_to_extents(self.roi_rect)
        finally:
            self._setting_extents = False
        um = ww * sc.pixel_size_um if sc.pixel_size_um else 0
        capped = " (at cap)" if self.cfg.gui.preview_roi_cap_um and um >= \
            self.cfg.gui.preview_roi_cap_um - 1 else ""
        # Show the size and wait for an explicit "Open ROI" (no auto-read / tab jump),
        # so the rectangle can still be adjusted first.
        self.status.configure(
            text=f"ROI {ww}x{hh}px (~{um:.0f}µm){capped} — adjust, then 'Open ROI'.")
        self._update_roi_buttons()
        self._update_result_provenance()      # result now lags the new rectangle

    def on_open_roi(self) -> None:
        """Read the pending rectangle at full resolution into the ROI tab."""
        if self.entry is None or self.roi_rect is None or self.roi_rgb is not None:
            return
        self.status.configure(
            text=f"{self.entry.alias}: reading ROI at full resolution…")
        self._submit(self._read_roi, self.entry.scene, self.roi_rect, tag="roi")

    def _update_roi_buttons(self) -> None:
        """Gate Clear/Open ROI by state (no rect drawn -> both disabled)."""
        if not hasattr(self, "btn_clear_roi"):
            return
        has_pending = self.roi_rect is not None
        has_open = self.roi_rgb is not None
        self.btn_clear_roi.configure(
            state="normal" if (has_pending or has_open) else "disabled")
        self.btn_open_roi.configure(
            state="normal" if (has_pending and not has_open) else "disabled")

    def _read_roi(self, scene, rect):
        import pylibCZIrw.czi as pyczi
        x, y, w, h = rect
        with self._czi_lock:                       # serialise CZI decodes (see __init__)
            with pyczi.open_czi(scene.path) as doc:
                rgb = preview.read_roi(doc, x, y, w, h, 1.0)
        return ("roi", rgb, scene.pixel_size_um)

    def clear_roi(self, refresh: bool = True) -> None:
        """Drop the current ROI and return to an editable thumbnail.

        *refresh* redraws the thumbnail (removing the rectangle) and switches to
        the Thumbnail tab; pass False when a new section is about to reload it. A
        user-initiated clear (refresh=True) also forgets this section's remembered
        rectangle, so it won't reappear on return.
        """
        if refresh and self.entry is not None:
            self._section_state.get(self.entry.scene.key, {}).pop("roi_rect", None)
        self._clear_saved_roi_outline()
        self.roi_rgb = None
        self.roi_px_um = None
        self.roi_rect = None
        self.layers = None
        self.roi_nav.clear_home()
        self._wb_cache.pop("roi", None)
        self._set_draw_active(False)           # back to left-drag-to-pan default
        if hasattr(self, "roi_tab"):
            self.nb.tab(self.roi_tab, state="disabled")    # no ROI -> grey the tab
        self._roi_hint()
        self._update_roi_buttons()
        self._update_result_provenance()       # result now lags the cleared ROI
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
        if not recompute:
            self._redraw()
        elif self.auto_recompute.get():
            self._schedule_recompute()
        # auto off: stay dirty (yellow) until the user presses Recompute.

    def _on_auto_toggle(self) -> None:
        """Auto on (default): debounced auto-recompute, Recompute button disabled. Auto
        off: edits just mark dirty; the user presses Recompute. Re-enabling auto while
        dirty catches up immediately."""
        auto = self.auto_recompute.get()
        self.btn_recompute.configure(state="disabled" if auto else "normal")
        if auto and self._dirty:
            self._schedule_recompute()

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._params_dirty = True            # a config-affecting change is now pending export
        self.dirty_dot.configure(text="● changed", fg="#c8862a")
        self.btn_recompute.configure(bg="#c8862a", activebackground="#a86f22")

    def _clear_dirty(self) -> None:
        self._dirty = False
        self.dirty_dot.configure(text="✓ up to date", fg="#3a7d44")
        self.btn_recompute.configure(bg="#3a7d44", activebackground="#2f6638")

    def _on_manual_seed_edit(self) -> None:
        """Manual-seed entry changed → recompute (unless we're reflecting the auto
        threshold back into the box; see `_after_layers`)."""
        if getattr(self, "_reflecting", False):
            return
        self._on_field_edit(recompute=True)

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
        excl = self._roi_exclude_crop()
        self._submit(self._compute, self.roi_rgb, cfg_snap, self.roi_px_um, manual, excl,
                     tag="layers")

    def _snapshot_cfg(self):
        """Deep-ish copy of the dataclass tree so the worker sees a stable config."""
        c = self.cfg
        return replace(
            c, tissue=replace(c.tissue), artifact=replace(c.artifact),
            fold=replace(c.fold), edge=replace(c.edge),
            detection=replace(c.detection), threshold=replace(c.threshold),
            overlay=replace(c.overlay))

    def _compute(self, rgb, cfg, px_um, manual, exclude):
        layers = preview.compute_roi_layers(rgb, cfg, px_um, manual_thr=manual,
                                            exclude=exclude)
        return ("layers", layers)

    # -- worker plumbing ---------------------------------------------------
    def _submit(self, fn, *args, tag="") -> None:
        if self._busy:
            # A recompute coalesces (run once the current task ends); a heavy CZI read
            # (display/roi/section) must NOT start a second concurrent decode — drop it
            # and ask the user to retry (the _czi_lock is the final safety net).
            if tag == "layers":
                self._recompute_pending = True
            else:
                self.status.configure(text="busy — wait for the current read to finish")
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
                kind = item[0]
                if kind == "section_progress":      # not terminal: keep _busy set
                    self.status.configure(
                        text=f"computing whole section… {item[1]:.0f}%")
                    continue
                self._busy = False
                if kind == "section":
                    row, px_um = item[1], item[2]
                    self._show_section_stats(row, px_um)
                    self.status.configure(
                        text=f"{row.get('alias')}: whole section done — "
                             f"{row.get('pct_sabg')}% SABG (cached for Analyze)")
                elif kind == "display":
                    self._show_display(item[1])
                elif kind == "roi":
                    self.roi_rgb, self.roi_px_um = item[1], item[2]
                    self._wb_cache.pop("roi", None)     # new ROI invalidates WB cache
                    self._set_draw_active(False)        # ROI fixed while open
                    self.nb.tab(self.roi_tab, state="normal")
                    self.nb.select(self.roi_tab)
                    self._update_roi_buttons()
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
        if self.manual_auto.get():          # reflect the auto threshold back (no recompute)
            self._reflecting = True         # the manual_thr trace must not re-mark dirty
            try:
                self.manual_thr.set(f"{lay['thr']:.4f}")
            finally:
                self._reflecting = False
        t = lay["tissue"]
        pct = 100.0 * lay["sabg"].sum() / t.sum() if t.any() else 0.0
        self.status.configure(
            text=f"%SABG={pct:.2f}  thr={lay['thr']:.4f}  "
                 f"tissue={100*t.mean():.1f}%  fold={100*lay['fold'].mean():.1f}%")
        self._show_stats(lay, self.roi_px_um, scope="ROI")
        self._record_result_provenance("ROI")
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
        dec = lay.get("deconv")
        if dec is not None and sabg_px:         # intensity-weighted readout (A3.3)
            od = dec[lay["sabg"]]
            lines.append(f"mean OD {float(od.mean()):.4f}  "
                         f"integrated OD {float(od.sum()):.1f}")
        self.stats_label.configure(text="\n".join(lines))

    def _overlay_order(self) -> list | None:
        """The visible ``(mask, color, alpha)`` layers, in draw order (or None)."""
        if self.layers is None:
            return None
        ov = self.cfg.overlay
        order = []
        for idx, (key, color_attr, alpha_attr, _d) in enumerate(gw.LAYER_SPEC):
            if not self.show_vars[key].get():
                continue
            mask = self.layers[key]
            if key == "fold" and mask is not None:
                # Don't paint the orange fold band UNDER a higher signal layer: candidate
                # (cyan), SABG+ (green) and edge-rejected (violet) are drawn above fold in
                # LAYER_SPEC, but alpha-blending cyan over the strong orange just muddied it.
                # Punch the visible upper layers out of the band so they read cleanly over it.
                for up_key, *_ in gw.LAYER_SPEC[idx + 1:]:
                    up = self.layers.get(up_key)
                    if up is not None and self.show_vars[up_key].get():
                        mask = mask & ~up
            order.append((mask, tuple(getattr(ov, color_attr)),
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
        self._sb_live_artist["roi"] = None          # live scale bar cleared with the axes
        self.roi_ax.set_axis_off()
        self.roi_ax.imshow(comp)
        self.roi_ax.set_title(f"{self.entry.alias} — ROI", fontsize=9)
        if self.roi_nav._home is not None:          # preserve zoom/pan across redraws
            self.roi_ax.set_xlim(*keep[0])
            self.roi_ax.set_ylim(*keep[1])
        else:
            self.roi_nav.set_home()
        self._refresh_live_scalebar("roi")          # redraw the live bar over the fresh overlay
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
            kind, target = "thumb", 1000.0
        else:
            if self.roi_rgb is None:
                messagebox.showinfo("Export", "Open a ROI and recompute first.", parent=self)
                return
            rgb, px_um, order = self.roi_rgb, self.roi_px_um, self._overlay_order()
            kind, target = "roi", 200.0
        # default base name + folder from cfg.paths (GUI convenience)
        tmpl = self.cfg.paths.preview_export_name or "{alias}_{kind}"
        try:
            default = tmpl.format(alias=self.entry.alias, kind=kind)
        except Exception:
            default = f"{self.entry.alias}_{kind}"
        path = filedialog.asksaveasfilename(
            parent=self, title="Export base name (presets are appended)",
            defaultextension=".jpg", initialfile=default,
            initialdir=(self.cfg.paths.export_dir or None),
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
                scalebar_um=self._sb_len_um(), scalebar_pos=_SB_POS[self.sb_pos.get()],
                scalebar_label=self.sb_label.get(), wb=True, target_um=target,
                wb_bright_frac=self.cfg.whitebalance.bright_frac,
                wb_target=self.cfg.whitebalance.target)
            self.status.configure(
                text=f"exported {len(written)} preset(s) → {base.parent}")
            messagebox.showinfo("Exported", "Wrote:\n" + "\n".join(p.name for p in written),
                                parent=self)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)

    def _persist_manual_seed(self) -> None:
        """Carry a dialed-in manual seed (Auto off + valid number) for the current
        section into the batch config as scenes.<key>.threshold (used as thr_override by
        analyze). The per-ROI value becomes the section's fixed seed; Auto on leaves any
        existing per-scene override untouched."""
        if self.entry is None or self.manual_auto.get():
            return
        try:
            val = float(self.manual_thr.get())
        except (ValueError, tk.TclError):
            return
        self.cfg.scenes.setdefault(self.entry.scene.key, {})["threshold"] = val

    def on_export(self) -> None:
        dst = self.config_path
        if dst.exists() and not messagebox.askyesno(
                "Overwrite config?", f"Overwrite\n{dst}\nwith the current settings?",
                parent=self):
            return
        self._persist_manual_seed()
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            preview.export_config(self.cfg, dst)
            self._params_dirty = False
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
             "Mouse wheel zooms to the cursor; left-drag pans (except while "
             "drawing a ROI or painting the exclusion brush); middle/right-drag "
             "always pans; 'Reset view' restores the full extent."),
            ("Resolution (Thumbnail tab)",
             "Choose the µm/px the section is read at for ROI drawing. 'loaded: …' "
             "shows the actual µm/px achieved for the image on screen."),
            ("Drawing a ROI",
             "'Draw ROI', then drag a rectangle (it shows live, and its size in px/µm "
             "appears in the status bar). Resize/move it with the handles, then "
             "'Open ROI' reads the crop at full resolution in the ROI tab (capped at "
             "gui.preview_roi_cap_um). The ROI tab is greyed until a ROI is open; "
             "'Clear ROI' (enabled once a rectangle exists) starts over."),
            ("Exclusion brush (Thumbnail tab)",
             "Paint regions to drop from analysis (e.g. muscle next to tumour). "
             "'draw' adds, 'erase' removes, 'size' sets the brush; 'Clear excl' wipes it. "
             "'Save excl' writes the mask and points the config at it — excluded pixels "
             "count as neither SABG+ nor tissue. 'Export → config' persists the link."),
            ("White-balanced",
             "Toggle a publication-style white balance on the displayed image "
             "(quantification always uses raw pixels — display only)."),
            ("Scale bar",
             "Length (Auto picks a nice value for the current view), label on/off, and "
             "corner. 'on image' draws a live bar on the image itself — it stays in the "
             "chosen corner as you pan and rescales as you zoom (it is NOT burned in). "
             "The same length/label/corner are used by Export, which burns its bar in."),
            ("Export…",
             "Writes publication presets next to a base name you pick: raw, "
             "white-balanced + scale bar, and (ROI tab) white-balanced + overlay + "
             "scale bar. The Thumbnail tab exports the whole section (no overlay)."),
            ("Tuning (right panel)",
             "Each detection stage (Tissue, Artifact, Fold, SABG, Edge) is always open "
             "with one 0-100 'sensitivity' slider — drag left to detect less, right to "
             "detect more; it drives several of that stage's knobs together. Open "
             "'details' for the individual raw parameters. The per-ROI seed threshold "
             "lives in the SABG section. The ROI recomputes after a short pause when "
             "'auto' is on (orange 'changed' → green 'up to date'); turn 'auto' off to "
             "edit freely and press Recompute. The Result panel shows %SABG, thresholds, "
             "tissue%, pixel counts and mm² areas."),
            ("Detailed parameters guide (advanced)",
             "Tissue — white_level 0.80-0.95 (brightness above which low-saturation = "
             "glass), sat_min 0.05-0.15, texture_min 0.001-0.02 (lower keeps more faint "
             "tissue), texture_win 15-41 px, bg_margin 0.04-0.20.\n"
             "Artifact — dark_level 0.25-0.45 (max(R,G,B)/255 below = dark), teal_min "
             "protects real teal from being flagged.\n"
             "Fold — source density|sabg, score_min 0.02-0.20 (lower finds more ridges), "
             "min_length_um / max_width_um / band_width_um set the ridge geometry.\n"
             "SABG — threshold.method triangle|otsu|percentile|fixed, threshold.scale "
             "0.5-1.3 (higher = stricter seed), hysteresis + hyst_low_scale 0.2-0.9 "
             "(lower grows seeds further into faint teal), expand_px dilates positives.\n"
             "Edge — min_width_um, shadow_dark_level / shadow_sat_min reject dark "
             "achromatic rims, teal_keep protects clearly-teal pixels."),
            ("Layers (ROI overlay)",
             "Toggle which overlay masks are drawn and their colour/alpha. They apply "
             "to the ROI overlay, so the panel is enabled only on the ROI tab."),
            ("Compute whole section…",
             "Runs the real full-resolution analysis for the selected section "
             "(minutes), writes its maps and caches the result. If you then "
             "'Export → config', clicking Analyze skips this section (config "
             "unchanged) and renders straight from the cached maps."),
            ("Export → config",
             "Writes the current settings to config.yaml for the batch analyze/export."),
        ])

    def _confirm_discard_exclusion(self, action: str) -> bool:
        """True to proceed; if an exclusion mask is painted-but-unsaved, ask first."""
        if not self._excl_dirty:
            return True
        alias = self.entry.alias if self.entry is not None else "this section"
        return messagebox.askyesno(
            "Unsaved exclusion",
            f"The exclusion mask for {alias} hasn't been saved (Save excl).\n"
            f"Discard it and {action}?",
            parent=self, default="no", icon="warning")

    def _on_close(self) -> None:
        pending = []
        if self._excl_dirty:
            pending.append("• an unsaved exclusion mask (use 'Save excl')")
        if self._params_dirty:
            pending.append("• tuning changes not written to config (use 'Export → config')")
        if pending and not messagebox.askyesno(
                "Close Preview?",
                "You have:\n" + "\n".join(pending) + "\n\nClose anyway?",
                parent=self, default="no", icon="warning"):
            return
        self.destroy()
