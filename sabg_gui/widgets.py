"""Reusable Tkinter widgets shared by the Preview, Info and Config windows.

This module centralises the settings UI so all three windows render one
implementation:

  * ``Tooltip``            -- hover help for any widget.
  * ``ScrollFrame``        -- a vertically scrolling region (mousewheel aware).
  * ``CollapsibleFrame``   -- a titled section that expands/collapses.
  * field-spec lists + ``build_field_rows`` -- label+editor grids driven by a
    ``(section, attr, kind, label, tooltip[, choices])`` spec, in pipeline order.
  * ``build_layers_panel`` -- one row per overlay layer: show / colour / alpha.
  * ``thumbnail_picker``   -- proportional section thumbnails with a select hook.

The field specs describe the public ``Config`` tree (see
``sabg_analyzer/config.py``); ``section`` is the sub-object name ("" for a
top-level ``Config`` attr) and ``kind`` is bool|int|float|choice|str|list.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser, ttk
from typing import Callable


# ---------------------------------------------------------------------------
# tooltips
# ---------------------------------------------------------------------------
class Tooltip:
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
# scrolling container
# ---------------------------------------------------------------------------
class ScrollFrame(tk.Frame):
    """A vertically scrollable frame. Pack/grid content into ``.interior``.

    The interior is kept the width of the canvas (no horizontal scrolling) and
    the mousewheel scrolls while the pointer is over the region.
    """

    def __init__(self, parent, **kw) -> None:
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        sb = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.interior = tk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.interior,
                                              anchor="nw")
        self._sr_job = None                  # pending scrollregion recompute (debounce)
        self._last_canvas_w = -1
        self.interior.bind("<Configure>", self._on_interior_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # mousewheel only while the pointer is inside this region
        self.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>",
                                                            self._on_wheel))
        self.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _content_fits(self) -> bool:
        """True when the interior is no taller than the visible canvas (nothing to
        scroll), so view moves should be a no-op kept pinned to the top."""
        bbox = self.canvas.bbox("all")
        if not bbox:
            return True
        return (bbox[3] - bbox[1]) <= self.canvas.winfo_height()

    def _schedule_scrollregion(self) -> None:
        # ponytail: coalesce the O(n) bbox("all")+scrollregion recompute to once per ~60ms.
        # During a sash drag this fires every pixel over 100s of gridded widgets -> lag;
        # debounce instead. Upgrade path if still slow: render the table as a ttk.Treeview.
        if self._sr_job is None:
            self._sr_job = self.after(60, self._apply_scrollregion)

    def _apply_scrollregion(self) -> None:
        self._sr_job = None
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self._content_fits():            # short content stays anchored at the top
            self.canvas.yview_moveto(0.0)

    def _on_interior_configure(self, _evt=None) -> None:
        self._schedule_scrollregion()

    def _on_canvas_configure(self, evt) -> None:
        if evt.width != self._last_canvas_w:   # skip redundant width sets (avoid feedback churn)
            self._last_canvas_w = evt.width
            self.canvas.itemconfigure(self._win, width=evt.width)
        self._schedule_scrollregion()

    def _on_wheel(self, event) -> None:
        if self._content_fits():            # don't overscroll into blank space above
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


# ---------------------------------------------------------------------------
# help popup
# ---------------------------------------------------------------------------
def help_popup(parent, title: str, sections) -> tk.Toplevel:
    """A scrollable help window. *sections* is a list of ``(heading, body)``.

    Used by the Preview and Config windows for a discoverable "what does each
    control do" overview (per-field ``Tooltip``s cover the fine detail).
    """
    top = tk.Toplevel(parent)
    top.title(title)
    top.geometry("560x640")
    sf = ScrollFrame(top)
    sf.pack(fill="both", expand=True)
    tk.Label(sf.interior, text=title, font=("Segoe UI", 12, "bold"),
             anchor="w").pack(fill="x", padx=10, pady=(10, 4))
    for heading, body in sections:
        tk.Label(sf.interior, text=heading, font=("Segoe UI", 10, "bold"),
                 anchor="w", fg="#1c3d6e").pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(sf.interior, text=body, font=("Segoe UI", 9), anchor="w",
                 justify="left", wraplength=510).pack(fill="x", padx=16)
    tk.Button(top, text="Close", command=top.destroy).pack(pady=6)
    return top


# ---------------------------------------------------------------------------
# collapsible section
# ---------------------------------------------------------------------------
class CollapsibleFrame(tk.Frame):
    """A titled section with a header button that shows/hides its body.

    Pack content into ``.body``. Pass ``description`` for a short grey line
    under the header explaining what the stage does.
    """

    def __init__(self, parent, title: str, description: str = "",
                 opened: bool = True, relief: str = "groove", borderwidth: int = 1,
                 btn_font=("Segoe UI", 9, "bold"), btn_fg: str | None = None, **kw) -> None:
        super().__init__(parent, relief=relief, borderwidth=borderwidth, **kw)
        self._title = title
        self._opened = bool(opened)
        self._btn = tk.Button(self, anchor="w", relief="flat", font=btn_font,
                              command=self.toggle)
        if btn_fg is not None:
            self._btn.configure(fg=btn_fg)
        self._btn.pack(fill="x")
        self.body = tk.Frame(self)
        if description:
            tk.Label(self.body, text=description, anchor="w", justify="left",
                     wraplength=300, fg="#666", font=("Segoe UI", 7)).pack(
                         fill="x", padx=4, pady=(2, 0))
        self._refresh()
        if self._opened:
            self.body.pack(fill="x", expand=True)

    def _refresh(self) -> None:
        self._btn.configure(text=("▾  " if self._opened else "▸  ") + self._title)

    def toggle(self) -> None:
        self._opened = not self._opened
        self._refresh()
        if self._opened:
            self.body.pack(fill="x", expand=True)
        else:
            self.body.pack_forget()


# ---------------------------------------------------------------------------
# mouse-only matplotlib canvas navigation (shared by Preview + Info viewers)
# ---------------------------------------------------------------------------
class CanvasNav:
    """Wheel-zoom-to-cursor + middle/right-drag pan on one matplotlib axes.

    Left-drag also pans when *can_left_pan* (a no-arg predicate) returns True --
    used so the Preview thumbnail pans on left-drag *unless* a ROI rectangle or
    the exclusion brush is active. Defaults to always-on (Info viewers, ROI tab).
    """

    def __init__(self, canvas, ax, can_left_pan=None, on_view_change=None) -> None:
        self.canvas = canvas
        self.ax = ax
        self._home = None
        self._panning = False
        self._can_left_pan = can_left_pan
        # Optional no-arg callback fired after the view EXTENT changes (wheel-zoom +
        # Reset view). Used by the Preview to rescale a live scale bar. Pan is excluded:
        # a corner-anchored overlay stays put and only needs the normal redraw.
        self._on_view_change = on_view_change
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
            if self._on_view_change is not None:
                self._on_view_change()
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
        if self._on_view_change is not None:
            self._on_view_change()
        self.canvas.draw_idle()

    def _press(self, e) -> None:
        if e.inaxes is not self.ax:
            return
        left_pan = e.button == 1 and (self._can_left_pan is None
                                      or self._can_left_pan())
        if e.button in (2, 3) or left_pan:
            self.ax.start_pan(e.x, e.y, 1)
            self._panning = True

    def _drag(self, e) -> None:
        if self._panning:
            self.ax.drag_pan(1, e.key, e.x, e.y)
            self.canvas.draw_idle()

    def _release(self, e) -> None:
        if self._panning:
            self.ax.end_pan()
            self._panning = False


# ---------------------------------------------------------------------------
# tuning-panel field specs (pipeline order)
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
    ("tissue", "fill_interior_holes", "bool", "fill_interior_holes", "Reclaim faint interior tissue dropped as glass: fill an enclosed hole only if it shows tissue evidence (texture or teal)."),
    ("tissue", "interior_hole_min_tissue_frac", "float", "interior_hole_min_tissue_frac", "Fill an interior hole only if at least this fraction of its pixels are textured or teal (raise to fill less)."),
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
    ("detection", "primary", "choice", "primary", "Which colour score drives detection: deconvolution (stain-unmixed SABG channel, default) or opponent (teal-vs-magenta colour).", ["deconvolution", "opponent"]),
    ("detection", "require_agreement", "bool", "require_agreement", "SABG+ only where BOTH scores clear their thresholds."),
    ("threshold", "method", "choice", "threshold.method", "Auto-threshold method on the tissue histogram.", ["triangle", "otsu", "percentile", "fixed"]),
    ("threshold", "scale", "float", "threshold.scale", "Seed/high threshold = auto-threshold x this (raise to be stricter)."),
    ("threshold", "percentile", "float", "threshold.percentile", "Percentile (when method=percentile)."),
    ("threshold", "min_score", "float", "threshold.min_score", "Clamp the threshold to at least this."),
    ("detection", "hysteresis", "bool", "hysteresis", "Grow each seed into the connected faint teal around it."),
    ("detection", "hyst_low_scale", "float", "hyst_low_scale", "Grow/low threshold = seed threshold x this (lower = grow further)."),
    ("detection", "hyst_teal_min", "float", "hyst_teal_min", "Grow only into pixels at least this teal (opponent score, 0-1); stops the grow leaking into non-teal tissue."),
    ("detection", "expand_px", "int", "expand_px", "Grow the final SABG+ area outward by this many pixels (0 = off)."),
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

# (title, fields, description) -- the detection groups, shared by Preview + Config.
# Order = tissue, then SABG detection, then the rejection stages (artifact, fold, edge);
# the pipeline runs in its own fixed sequence regardless of this list.
DETECTION_GROUPS = [
    ("1. Tissue", TISSUE_FIELDS,
     "Separate stained tissue from glass / black mosaic gaps; clean and reclaim faint interior tissue."),
    ("2. SABG detection", DETECT_FIELDS,
     "Threshold the SABG score on tissue, then grow seeds into connected faint teal."),
    ("3. Artifact / dark folds", ARTIFACT_FIELDS,
     "Flag dark, non-teal fold/debris pixels and exclude them from counting."),
    ("4. Fold (linear ridges)", FOLD_FIELDS,
     "Detect thin linear tissue folds and optionally drop them from the denominator."),
    ("5. Edge-shadow rejection", EDGE_FIELDS,
     "Reject thin dark edge-shadow rims wrongly counted as positive."),
]

# Layers in composite DRAW order (bottom -> top; last = on top). `_overlay_order` ALPHA-BLENDS
# them in this order (so overlaps stay visible — fold + artifact are NOT mutually exclusive),
# EXCEPT `excluded` + `nontissue` occlude: the detection layers are not drawn under them, so the
# overlay shows everything except in the excluded / non-tissue areas. Order puts `sabg_candidate`
# (cyan) above the fold/artifact bands so it reads over them, with `edge_removed` (violet) and the
# final `sabg` (green) above candidate. (key, colour attr, alpha attr, default show).
LAYER_SPEC = [
    ("artifact", "artifact_color", "artifact_alpha", True),
    ("fold", "fold_color", "fold_alpha", True),
    ("sabg_candidate", "sabg_candidate_color", "sabg_candidate_alpha", False),
    ("edge_removed", "edge_color", "edge_alpha", True),
    ("sabg", "sabg_color", "sabg_alpha", True),
    ("excluded", "excluded_color", "excluded_alpha", True),
    ("nontissue", "nontissue_color", "nontissue_alpha", True),
]
LAYER_LABELS = {"nontissue": "non-tissue", "excluded": "excluded", "artifact": "artifact",
                "fold": "fold", "sabg_candidate": "candidate SABG+", "sabg": "SABG+",
                "edge_removed": "edge-rejected", "masked": "masked (grouped)"}

# The Layers *panel* lists layers in its own order (decoupled from the composite z-order above):
# excluded, non-tissue, candidate, artifact, fold, edge, final SABG+, then the grouped
# "masked" union (default OFF — it's the clean 2-layer alternative to the individual layers,
# the same grey union the default section figure draws). `masked` is panel-only, not in
# LAYER_SPEC; `_overlay_order` paints its union explicitly.
_LAYER_BY_KEY = {s[0]: s for s in LAYER_SPEC}
LAYER_PANEL_SPEC = [_LAYER_BY_KEY[k] for k in
                    ("excluded", "nontissue", "sabg_candidate", "artifact",
                     "fold", "edge_removed", "sabg")] + [
                    ("masked", "masked_color", "masked_alpha", False)]

# ---------------------------------------------------------------------------
# "Slider setup" mode: one guided sensitivity bar per layer.
# Each knob = (section, attr, value@0, value@100, label); slider position 0 maps to
# value@0 and 100 to value@100, so value@0/value@100 bake in the direction.
# DETECTION stages (tissue, SABG+) run 0 (detect LESS) -> 100 (detect MORE).
# REJECTION stages (artifact, fold, edge-reject; see REJECT_SLIDER_LABELS) run
# 0 (reject LESS) -> 100 (reject MORE), so all three rejection sliders agree:
# right = strip more positives. e.g. texture_min DROPS as detection rises; edge
# teal_keep RISES as rejection rises (a higher teal_keep protects FEWER pixels, so
# rejects more). The first knob drives the slider position; the rest follow.
# Ranges are CENTRED on the config defaults so each slider opens at the midpoint (50)
# when the parameter sits at its default. Recentred in session 17 after the cfg07 tuning
# defaults landed (threshold.scale 0.825->0.70); each (v0, v100) midpoint = the program
# default for that knob. The raw entry still accepts values beyond the slider span.
SLIDER_LAYERS = [
    ("tissue", [("tissue", "texture_min", 0.009, 0.0, "texture_min"),     # center 0.0045
                ("tissue", "bg_margin", 0.16, 0.012, "bg_margin")]),      # center 0.086
    ("SABG+", [("threshold", "scale", 1.00, 0.40, "threshold.scale"),     # center 0.70 (cfg07)
               ("detection", "hyst_low_scale", 0.95, 0.05, "hyst_low_scale")]),  # center 0.50
    ("artifact", [("artifact", "dark_level", 0.25, 0.65, "dark_level")]),  # center 0.45
    ("fold", [("fold", "score_min", 0.10, 0.0, "score_min")]),             # center 0.05
    # edge teal_keep RISES with the slider so right = reject MORE (was inverted at
    # 0.20->0.02, which made slider-right reject LESS, opposite of artifact/fold).
    ("edge-reject", [("edge", "teal_keep", 0.01, 0.17, "teal_keep")]),     # center 0.09
]


def slider_to_value(v0: float, v100: float, s: float) -> float:
    """Sensitivity *s* in [0, 100] -> the knob value (linear v0..v100)."""
    return v0 + (v100 - v0) * (max(0.0, min(100.0, s)) / 100.0)


def value_to_slider(v0: float, v100: float, cur: float) -> float:
    """Inverse of ``slider_to_value`` (knob value -> slider position 0..100)."""
    if v100 == v0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (cur - v0) / (v100 - v0)))


# Flat {(section, attr): (v0, v100)} of every guided knob, so the detection panel
# can show a 0-100 "sensitivity" slider next to the raw entry of these fields.
SENSITIVITY_KNOBS: dict[tuple[str, str], tuple[float, float]] = {
    (section, attr): (v0, v100)
    for _label, knobs in SLIDER_LAYERS
    for section, attr, v0, v100, _klab in knobs
}


def _add_sensitivity_slider(parent, row: int, var: tk.StringVar,
                            v0: float, v100: float) -> None:
    """Add a 0-100 sensitivity ``ttk.Scale`` in column 2, two-way synced with the
    raw-value entry *var* (left = detect less, right = more). A guard breaks the
    slider<->entry feedback loop (same pattern as ``_AlphaControl``)."""
    guard = {"on": False}
    sv = tk.DoubleVar()
    try:
        sv.set(value_to_slider(v0, v100, float(var.get())))
    except (ValueError, tk.TclError):
        pass

    def from_slider(_v=None) -> None:
        if guard["on"]:
            return
        guard["on"] = True
        var.set(f"{slider_to_value(v0, v100, sv.get()):.4g}")   # fires the entry trace
        guard["on"] = False

    def from_entry(*_a) -> None:
        if guard["on"]:
            return
        try:
            cur = float(var.get())
        except (ValueError, tk.TclError):
            return
        guard["on"] = True
        sv.set(value_to_slider(v0, v100, cur))
        guard["on"] = False

    ttk.Scale(parent, from_=0, to=100, variable=sv, command=from_slider,
              length=90).grid(row=row, column=2, sticky="ew", padx=(4, 2))
    var.trace_add("write", from_entry)

# ---------------------------------------------------------------------------
# "Other settings" (non-detection) groups -- for the Config window's 2nd tab.
# ---------------------------------------------------------------------------
SIZING_FIELDS = [
    ("", "process_zoom", "float", "process_zoom", "Global processing zoom multiplier."),
    ("", "tile_size", "int", "tile_size", "Tile edge (px) for full-res reads."),
    ("", "overview_um_per_px", "float", "overview_um_per_px", "Gating/histogram overview resolution (µm/px)."),
    ("", "overview_max_edge", "int", "overview_max_edge", "Safety cap (px) for the overview canvas."),
    ("", "maps_um_per_px", "float", "maps_um_per_px", "Maps canvas resolution (µm/px)."),
    ("", "maps_max_edge", "int", "maps_max_edge", "Safety cap (px) for the maps canvas."),
    ("", "thumb_um_per_px", "float", "thumb_um_per_px", "Scan thumbnail resolution (µm/px)."),
    ("", "thumb_max_edge", "int", "thumb_max_edge", "Safety cap (px) for scan thumbnails."),
    ("", "full_debug", "bool", "full_debug", "Write extra debug artifacts."),
]
WHITEBALANCE_FIELDS = [
    ("whitebalance", "auto", "bool", "auto",
     "Estimate the white point automatically (on). Off = manual: use a picked white point "
     "(the Preview 'pick white' pipette, or white_point below for global)."),
    ("whitebalance", "scope", "choice", "scope",
     "Where the white point comes from + how widely it's shared (display/figures only): "
     "image = the viewed image self-balances; section = one per section (its overview), reused "
     "for all its ROIs; global = one for the whole loaded dataset.",
     ["image", "section", "global"]),
    ("whitebalance", "bright_frac", "float", "bright_frac",
     "Fraction of the brightest pixels averaged as the white point (auto estimate)."),
    ("whitebalance", "neutralize", "float", "neutralize",
     "Auto de-cast strength 0..1: 0 = mild (brightest pixels); 1 = map the dominant glass "
     "colour fully to white (removes more of the yellow background cast)."),
    ("whitebalance", "glass_percentile", "float", "glass_percentile",
     "Per-channel percentile that defines the 'glass' colour used when neutralize > 0."),
    ("whitebalance", "target", "float", "target",
     "Channel value the white point is scaled to (near-white)."),
    ("whitebalance", "temperature", "float", "temperature",
     "Warm(+)/cool(-) nudge of the white point. ZEN ±1 ≈ ±10 K."),
    ("whitebalance", "temperature_k", "float", "temperature_k",
     "Per-step gain of `temperature` (calibration knob; flip its sign if warm reads cool)."),
    ("whitebalance", "brightness", "float", "brightness",
     "Display tone: additive brightness on [0,1] after white balance (-1..1; 0 = no-op)."),
    ("whitebalance", "contrast", "float", "contrast",
     "Display tone: contrast about mid-grey (-1..1; 0 = no-op)."),
    ("whitebalance", "gamma", "float", "gamma",
     "Display tone: gamma, out = in**(1/gamma) (>0; 1 = no-op)."),
    ("whitebalance", "homogeneity_tol", "float", "homogeneity_tol",
     "Reserved: max RGB spread allowed for a manual white-point pick."),
]
OUTPUT_FIELDS = [
    ("output", "debug", "bool", "debug", "Write debug/<alias>_compare.jpg (6-panel audit)."),
    ("output", "maps", "bool", "maps", "Write maps/<alias>_* (consumed by export)."),
    ("output", "keep_maps", "bool", "keep_maps", "Keep maps/ after export."),
    ("output", "export_on_analyze", "bool", "export_on_analyze", "Run export automatically after analyze."),
    ("output", "log_files", "bool", "log_files", "Log the files written at each step."),
    ("output", "run_log", "bool", "run_log", "Tee the console output to a timestamped log file."),
    ("output", "run_log_name", "str", "run_log_name", "strftime template for the run-log filename."),
]
INTENSITY_FIELDS = [
    ("intensity", "enabled", "bool", "enabled",
     "Write OD-weighted intensity columns to results.csv (stain amount, not just area):\n"
     "• sabg_integrated_od = Σ OD over positives (area × intensity)\n"
     "• sabg_mean_od = mean OD per positive pixel (intensity alone)\n"
     "Off = %SABG by area only. This never changes %SABG."),
    ("intensity", "per_tissue", "bool", "per-tissue OD",
     "Also write sabg_od_per_tissue = Σ OD over all tissue px ÷ tissue px — an "
     "intensity-weighted analogue of %SABG (stain amount normalised to tissue, not just "
     "positives). Requires 'enabled'; has no effect on its own."),
]
REPORT_FIELDS = [
    ("report", "metadata", "bool", "metadata",
     "Write metadata.csv: a human digest of the run (analysis settings + timing + version) "
     "plus the CZI acquisition metadata (microscope/objective/NA/etc.)."),
    ("report", "details", "bool", "details",
     "Write results_detailed.csv: per-section image dims, per-layer px/areas, the pos∩fold "
     "intersection, and intensities (everything already computed)."),
    ("report", "workbook", "bool", "workbook",
     "Write results.xlsx bundling results / metadata / details into one workbook (tabs)."),
    ("report", "workbook_name", "str", "workbook_name", "Filename for the bundled workbook."),
]
PROGRESS_FIELDS = [
    ("progress", "section", "bool", "section", "Show per-section progress."),
    ("progress", "total", "bool", "total", "Show overall progress."),
    ("progress", "elapsed", "bool", "elapsed", "Show elapsed time."),
    ("progress", "eta", "bool", "eta", "Show estimated time remaining."),
]
GUI_FIELDS = [
    ("gui", "preview_roi_cap_um", "float", "preview_roi_cap_um", "Preview ROI draw cap (µm)."),
    ("gui", "info_opens", "list", "info_opens", "What the Info button opens (comma separated: sections, labels, thumbs)."),
    ("gui", "scalebar_values", "list", "scalebar_values",
     "Preview scale-bar length presets in µm (comma separated, e.g. 10000, 1000, 100, 10)."),
]
PATHS_FIELDS = [
    ("paths", "data_dir", "str", "data_dir", "Default data folder (blank = ../data)."),
    ("paths", "out_dir", "str", "out_dir", "Default output folder (blank = ../outputs)."),
    ("paths", "export_dir", "str", "export_dir",
     "Default folder for the preview Export… dialog (blank = last/ask)."),
    ("paths", "preview_export_name", "str", "preview_export_name",
     "Default base name for preview exports (tokens: {alias}, {kind} = roi/thumb)."),
]
ALIAS_FIELDS = [
    ("alias", "fields", "list", "fields", "Metadata columns joined into the alias (comma separated)."),
    ("alias", "optional", "list", "optional", "Extra columns appended only to break ties (comma separated)."),
    ("alias", "spacer", "str", "spacer", "Separator between alias parts."),
    ("alias", "tag_field", "str", "tag_field", "Column whose value (if filled) overrides the alias."),
]
OTHER_GROUPS = [
    ("Canvas sizing", SIZING_FIELDS,
     "How big each working canvas is (µm/px, proportional to physical size, with px caps)."),
    ("Output artifacts", OUTPUT_FIELDS, "Which files analyze/export write."),
    ("Run report", REPORT_FIELDS,
     "Optional companion tables: metadata.csv (run params + timing + CZI acquisition), "
     "results_detailed.csv, and a bundled results.xlsx workbook."),
    ("Intensity quantification", INTENSITY_FIELDS,
     "Optional OD-weighted intensity columns in results.csv (stain amount): "
     "integrated/mean OD over positives, plus per-tissue OD. %SABG by area is unaffected."),
    ("White balance", WHITEBALANCE_FIELDS,
     "Display/figure white balance (quantification always uses raw pixels)."),
    ("Progress", PROGRESS_FIELDS, "What the progress reporter prints."),
    ("GUI", GUI_FIELDS, "GUI-only knobs."),
    ("Paths & filenames", PATHS_FIELDS,
     "Default data/output/export folders + preview export name (GUI convenience)."),
    ("Alias", ALIAS_FIELDS, "How each section's short alias is built from sections.csv."),
]

# ---------------------------------------------------------------------------
# Export options. `Config.export` is a free-form dict, so these fields use
# section "" and are edited through a `DictObj` proxy (below) over the effective
# export dict; mirrors `export.ExportParams`.
# ---------------------------------------------------------------------------
# Files per FOV = (enabled colour bases: raw and/or wb) × (enabled variants: plain
# and/or qc_overlay) × (formats). E.g. wb+plain and raw+qc = 2 files per FOV.
EXPORT_FOV_FIELDS = [
    ("", "n_fov", "int", "n_fov", "How many representative FOV crops to write per section."),
    ("", "fov_um", "float", "fov_um", "FOV crop side length in microns (square)."),
    ("", "min_tissue_frac", "float", "min_tissue_frac",
     "A FOV must be at least this fraction tissue (0-1) to qualify."),
    ("", "fov_select_mode", "choice", "fov_select_mode",
     "How FOVs are chosen: average (near the section mean %SABG), deciles (10 FOVs "
     "spanning the %SABG range), or n (the n_fov cleanest).", ["average", "deciles", "n"]),
    ("", "max_artifact_frac", "float", "max_artifact_frac",
     "Reject a FOV if at least this fraction is artifact (1 = off)."),
    ("", "max_fold_frac", "float", "max_fold_frac",
     "Reject a FOV if at least this fraction is fold (1 = off)."),
    ("", "scalebar_um", "float", "scalebar_um", "Scale-bar length burned into each FOV (µm)."),
    ("", "scalebar_label", "bool", "scalebar_label", "Draw the '… µm' text above the FOV scale bar."),
    ("", "wb", "bool", "wb", "Colour base: write a WHITE-BALANCED version of each FOV."),
    ("", "raw", "bool", "raw", "Colour base: write an ORIGINAL-COLOUR version of each FOV."),
    ("", "plain", "bool", "plain", "Variant: write the clean FOV image (no overlay) for each enabled base."),
    ("", "plain_bases", "list", "plain_bases",
     "Which colour bases get the plain (no-overlay) variant (comma separated: wb, raw)."),
    ("", "qc_overlay", "bool", "qc_overlay",
     "Variant: write a FOV copy with the SABG+/artifact overlay burned in."),
    ("", "qc_bases", "list", "qc_bases",
     "Which colour bases get the qc_overlay variant (comma separated: wb, raw)."),
    ("", "formats", "list", "formats", "Output formats per file (comma separated: jpg, png, tif)."),
]
EXPORT_SECTION_FIELDS = [
    ("", "section_figures", "bool", "section_figures", "Render whole-section overlay figures."),
    ("", "section_um_per_px", "float", "section_um_per_px", "Section-figure resolution (µm/px)."),
    ("", "sec_variants", "list", "sec_variants",
     "One file per entry; each is underscore-joined tokens: base raw|wb, overlay "
     "overlay|overlaysabg, fov (numbered FOV boxes), scalebar. e.g. wb_overlay_fov_scalebar."),
    ("", "sec_formats", "list", "sec_formats", "Section figure formats (comma separated: jpg, png, tif)."),
    ("", "sec_scalebar_um", "float", "sec_scalebar_um", "Section scale-bar length (µm)."),
    ("", "sec_scalebar_adaptive", "bool", "sec_scalebar_adaptive",
     "Snap the section scale bar to a nice value near sec_scalebar_um."),
    ("", "sec_scalebar_label", "bool", "sec_scalebar_label", "Draw the section scale-bar label."),
]
EXPORT_GROUPS = [
    ("FOV crops", EXPORT_FOV_FIELDS,
     "Per-FOV full-resolution crops. Files written per FOV = the colour bases you enable "
     "(raw and/or white-balanced) × the variants (plain and/or qc_overlay) × formats. "
     "FOVs are picked ≥ min_tissue_frac tissue, near the section mean %SABG."),
    ("Section figures", EXPORT_SECTION_FIELDS,
     "Whole-section overlay figures rendered from the maps — one file per sec_variants "
     "entry (token grammar in the sec_variants tooltip)."),
]


class DictObj:
    """Attribute access over a plain dict, so a dict-backed config block (the
    free-form ``Config.export``) can reuse ``build_field_rows`` / ``apply_field``.
    Missing keys read as None; writes go straight to the dict."""

    def __init__(self, d: dict) -> None:
        object.__setattr__(self, "_d", d)

    def __getattr__(self, k):
        return object.__getattribute__(self, "_d").get(k)

    def __setattr__(self, k, v) -> None:
        object.__getattribute__(self, "_d")[k] = v


# ---------------------------------------------------------------------------
# field editors
# ---------------------------------------------------------------------------
def parse_field(kind: str, raw):
    """Convert a widget value to the typed Python value for the config."""
    if kind == "bool":
        return bool(raw)
    if kind == "int":
        return int(float(raw))
    if kind == "float":
        return float(raw)
    if kind == "list":
        return [s.strip() for s in str(raw).split(",") if s.strip()]
    return str(raw)            # choice | str


def _current_str(kind: str, cur) -> str:
    if kind == "list":
        return ", ".join(str(x) for x in (cur or []))
    return str(cur)


def build_field_rows(parent, cfg, fields, field_vars: dict, on_change: Callable,
                     recompute: bool, sensitivity: bool = False) -> None:
    """Lay out label+editor rows for *fields* into *parent* (a frame).

    ``field_vars[(section, attr)]`` is populated with the tk.Variable and
    ``on_change(section, attr, kind, recompute)`` is invoked on every edit.
    When *sensitivity* is True, float fields in ``SENSITIVITY_KNOBS`` also get a
    0-100 sensitivity slider (column 2) synced with their raw value.
    """
    has_slider = False
    for row, spec in enumerate(fields):
        section, attr, kind, label, tip = spec[0], spec[1], spec[2], spec[3], spec[4]
        obj = getattr(cfg, section) if section else cfg
        cur = getattr(obj, attr)
        lbl = tk.Label(parent, text=label, anchor="w", width=22)
        lbl.grid(row=row, column=0, sticky="w")
        Tooltip(lbl, tip)
        if kind == "bool":
            var = tk.BooleanVar(value=bool(cur))
            w = tk.Checkbutton(parent, variable=var,
                               command=lambda s=section, a=attr, k=kind, rc=recompute:
                               on_change(s, a, k, rc))
            w.grid(row=row, column=1, sticky="w")
        elif kind == "choice":
            choices = spec[5]
            var = tk.StringVar(value=str(cur))
            w = ttk.OptionMenu(parent, var, str(cur), *choices,
                               command=lambda _v, s=section, a=attr, k=kind, rc=recompute:
                               on_change(s, a, k, rc))
            w.grid(row=row, column=1, sticky="ew")
        else:  # int | float | str | list
            var = tk.StringVar(value=_current_str(kind, cur))
            w = tk.Entry(parent, textvariable=var, width=12)
            var.trace_add("write", lambda *_a, s=section, a=attr, k=kind, rc=recompute:
                          on_change(s, a, k, rc))
            w.grid(row=row, column=1, sticky="ew")
            if (sensitivity and kind == "float"
                    and (section, attr) in SENSITIVITY_KNOBS):
                v0, v100 = SENSITIVITY_KNOBS[(section, attr)]
                _add_sensitivity_slider(parent, row, var, v0, v100)
                has_slider = True
        field_vars[(section, attr)] = var
        Tooltip(w, tip)
    parent.columnconfigure(1, weight=1)
    if has_slider:
        parent.columnconfigure(2, weight=1)


def apply_field(cfg, section: str, attr: str, kind: str, var: tk.Variable) -> bool:
    """Read *var*, coerce by *kind*, set on the config. Returns True on success."""
    obj = getattr(cfg, section) if section else cfg
    try:
        val = parse_field(kind, var.get())
    except (ValueError, tk.TclError):
        return False                          # mid-typing; ignore until valid
    setattr(obj, attr, val)
    return True


# Map each detection group title -> its SLIDER_LAYERS label (the composite knob set),
# so a section's one sensitivity slider can drive all of its guided knobs together.
SECTION_SLIDERS = {
    "1. Tissue": "tissue",
    "2. SABG detection": "SABG+",
    "3. Artifact / dark folds": "artifact",
    "4. Fold (linear ridges)": "fold",
    "5. Edge-shadow rejection": "edge-reject",
}
_SLIDER_KNOBS_BY_LABEL = dict(SLIDER_LAYERS)        # label -> [(section, attr, v0, v100, klab), ...]

# Rejection stages: their composite slider reads "reject less -> reject more" (right
# = strip more positives), vs detection stages ("detect less -> detect more"). Used
# to label the slider and its tooltip correctly per stage.
REJECT_SLIDER_LABELS = {"artifact", "fold", "edge-reject"}

# Pre-SABG exclusion stages (tissue, artifact) run BEFORE detection, so their sliders are
# framed in tissue/exclusion terms, NOT "fewer/more SABG⁺". (left, right, tip-template);
# `{knobs}` is filled with the driven knob list. Directions verified vs SLIDER_LAYERS:
# tissue right -> texture_min 0.0 / bg_margin 0.012 = more tissue; artifact right ->
# dark_level 0.65 = reject more. Stages not listed keep the generic reject/detect labels.
SLIDER_LABEL_OVERRIDES = {
    "tissue":   ("← less tissue", "more tissue →",
                 "Drives {knobs} together. Right keeps MORE tissue, left trims toward cleaner "
                 "core tissue — sets the pre-SABG tissue mask, not the SABG⁺ count."),
    "artifact": ("← keep more", "reject more →",
                 "Drives {knobs} together. Right excludes MORE dark fold/debris before SABG "
                 "detection, left keeps more — a pre-SABG exclusion, not the SABG⁺ count."),
}


def _add_composite_slider(parent, knobs, field_vars: dict, reject: bool = False,
                          extra_tip: str = "", labels: tuple | None = None) -> None:
    """One 0-100 slider driving EVERY knob in *knobs* together. Detection stages read
    left = detect less, right = detect more; *reject* stages (artifact/fold/edge) read
    left = reject less, right = reject more. Each knob's field var must already be in
    *field_vars* (build the raw rows first). Editing the primary (first) knob re-derives
    the slider position; a guard breaks the slider<->entry feedback loop. *extra_tip* is
    appended to the slider tooltip (the stage description, now off the always-visible area)."""
    guard = {"on": False}
    sv = tk.DoubleVar()
    p_sec, p_attr, p_v0, p_v100 = knobs[0][0], knobs[0][1], knobs[0][2], knobs[0][3]
    primary_var = field_vars.get((p_sec, p_attr))
    try:
        if primary_var is not None:
            sv.set(value_to_slider(p_v0, p_v100, float(primary_var.get())))
    except (ValueError, tk.TclError):
        pass

    def from_slider(_v=None) -> None:
        if guard["on"]:
            return
        guard["on"] = True
        try:                              # ttk.Scale passes the new value; prefer it
            s = float(_v) if _v is not None else sv.get()
        except (ValueError, tk.TclError):
            s = sv.get()
        for sec, attr, v0, v100, _kl in knobs:
            var = field_vars.get((sec, attr))
            if var is not None:
                var.set(f"{slider_to_value(v0, v100, s):.4g}")   # fires the entry trace
        guard["on"] = False

    def from_primary(*_a) -> None:
        if guard["on"] or primary_var is None:
            return
        try:
            cur = float(primary_var.get())
        except (ValueError, tk.TclError):
            return
        guard["on"] = True
        sv.set(value_to_slider(p_v0, p_v100, cur))
        guard["on"] = False

    # End labels read in terms of the SABG⁺ OUTCOME (not the raw parameter value), so
    # every stage agrees: right = more SABG⁺ survives for detection, right = more
    # rejected for the reject stages (D6).
    knob_list = ", ".join(k[4] for k in knobs)
    if labels is not None:                      # per-stage override (pre-SABG exclusion stages)
        left_txt, right_txt, tip = labels[0], labels[1], labels[2].format(knobs=knob_list)
    elif reject:
        left_txt, right_txt = "← keep more", "reject more →"
        tip = (f"Drives {knob_list} together. Right rejects MORE SABG⁺ positives "
               "(left keeps more) — the slider follows the SABG⁺ outcome, not the raw "
               "parameter value.")
    else:
        left_txt, right_txt = "← fewer SABG⁺", "more SABG⁺ →"
        tip = (f"Drives {knob_list} together. Left = FEWER SABG⁺ positives, right = more "
               "— the slider follows the SABG⁺ outcome, not the raw parameter value.")
    tk.Label(parent, text=left_txt, fg="#888", font=("Segoe UI", 7)).grid(
        row=0, column=0, sticky="e")
    scale = ttk.Scale(parent, from_=0, to=100, variable=sv, command=from_slider,
                      length=110)
    scale.grid(row=0, column=1, sticky="ew", padx=3)
    tk.Label(parent, text=right_txt, fg="#888", font=("Segoe UI", 7)).grid(
        row=0, column=2, sticky="w")
    # The "sensitivity"/"strength" sub-label was dropped (G2 compact-panel): it added a
    # whole row per stage for no information the end-labels + tooltip don't already give.
    if extra_tip:
        tip = f"{tip}\n\n{extra_tip}"
    Tooltip(scale, tip)                  # hover help on the slider itself (B4)
    parent.columnconfigure(1, weight=1)
    if primary_var is not None:
        primary_var.trace_add("write", from_primary)


_DEFAULT_PARAMS_CACHE: dict | None = None


def _default_param_instances() -> dict:
    """Fresh dataclass instances holding the program defaults for each config block a
    detection stage edits. Imported lazily so this pure-Tk module stays importable
    without the analysis package loaded at import time (and avoids any import cycle)."""
    global _DEFAULT_PARAMS_CACHE
    if _DEFAULT_PARAMS_CACHE is None:
        from sabg_analyzer.tissue import ArtifactParams, TissueParams
        from sabg_analyzer.fold import FoldParams
        from sabg_analyzer.edge import EdgeFilterParams
        from sabg_analyzer.threshold import ThresholdParams
        from sabg_analyzer.config import DetectionParams
        _DEFAULT_PARAMS_CACHE = {
            "tissue": TissueParams(), "artifact": ArtifactParams(),
            "fold": FoldParams(), "edge": EdgeFilterParams(),
            "detection": DetectionParams(), "threshold": ThresholdParams(),
        }
    return _DEFAULT_PARAMS_CACHE


def _reset_section_fields(cfg, fields, field_vars: dict, on_change: Callable,
                          recompute: bool) -> None:
    """Restore every field in *fields* to its dataclass default, updating both the
    on-screen var and cfg (via *on_change*) so the composite slider re-syncs and a
    single debounced recompute runs. Each displayed field is reset to ITS OWN block's
    default (the Tissue stage shows ``artifact.erode_px``, the SABG stage spans
    ``detection`` + ``threshold``), so we look the default up per (section, attr).
    bool/choice editors don't fire on ``var.set``, so on_change is called explicitly."""
    defaults = _default_param_instances()
    for spec in fields:
        section, attr, kind = spec[0], spec[1], spec[2]
        d = defaults.get(section)
        if d is None or not hasattr(d, attr):
            continue
        var = field_vars.get((section, attr))
        if var is None:
            continue
        dv = getattr(d, attr)
        if kind == "bool":
            var.set(bool(dv))
        else:
            var.set(_current_str(kind, dv))
        on_change(section, attr, kind, recompute)


def build_detection_sections(parent, cfg, field_vars: dict, on_change: Callable,
                             recompute: bool, section_extra: dict | None = None) -> list:
    """Preview detection panel: each ``DETECTION_GROUPS`` stage as an ALWAYS-OPEN
    section showing a single composite *sensitivity* slider (driving that stage's knobs),
    with the full raw parameters behind a collapsed **details** expander.

    *section_extra* maps a group title -> ``callable(frame)`` to inject extra controls
    (e.g. the per-ROI seed threshold into "2. SABG detection"). The raw rows are built
    first so ``field_vars`` is populated before the composite slider wires to it."""
    section_extra = section_extra or {}
    out = []
    for title, fields, desc in DETECTION_GROUPS:
        sec = tk.Frame(parent, padx=3, pady=1)               # borderless (the wrapper is bordered)
        sec.pack(fill="x", expand=True, pady=1)
        # Title row: stage name (not bold) + the stage 'enable' tick (artifact/fold/edge) on
        # the same line. The 'enabled' field is pulled OUT of the raw rows below.
        trow = tk.Frame(sec)
        trow.pack(fill="x")
        tk.Label(trow, text=title, anchor="w").pack(side="left")
        enable_spec = next((f for f in fields if f[1] == "enabled"), None)
        body_fields = [f for f in fields if f[1] != "enabled"]
        if enable_spec is not None:
            esec, eattr, ekind = enable_spec[0], enable_spec[1], enable_spec[2]
            eobj = getattr(cfg, esec) if esec else cfg
            evar = tk.BooleanVar(value=bool(getattr(eobj, eattr)))
            tk.Checkbutton(trow, text="enable", variable=evar,
                           command=lambda s=esec, a=eattr, k=ekind:
                           on_change(s, a, k, recompute)).pack(side="left", padx=(6, 0))
            field_vars[(esec, eattr)] = evar
        # Compact (session 17): the per-stage description + raw params live behind a collapsed
        # "details" expander (small grey, styled like the slider end-labels), so the always-
        # visible part of each stage is just the title row + the composite slider. The expander
        # is built first so field_vars is populated before the slider wires to it.
        det = CollapsibleFrame(sec, "details", description=desc, opened=False,
                               relief="flat", borderwidth=0,
                               btn_font=("Segoe UI", 7), btn_fg="#888")
        Tooltip(det._btn, "Show or hide this stage's raw parameters.")
        grid = tk.Frame(det.body, padx=2, pady=2)
        grid.pack(fill="x")
        build_field_rows(grid, cfg, body_fields, field_vars, on_change, recompute,
                         sensitivity=False)
        # section_extra (e.g. the per-ROI seed controls) goes LAST inside the details body.
        if title in section_extra:
            ex = tk.Frame(det.body, padx=2)
            ex.pack(fill="x", pady=(2, 0))
            section_extra[title](ex)
        # one always-visible row: composite sensitivity slider (cols 0-2) + Reset (col 3).
        srow = tk.Frame(sec, padx=2)
        srow.pack(fill="x", pady=(1, 0))
        if title in SECTION_SLIDERS:
            slabel = SECTION_SLIDERS[title]
            knobs = _SLIDER_KNOBS_BY_LABEL.get(slabel)
            if knobs:
                _add_composite_slider(srow, knobs, field_vars,
                                      reject=slabel in REJECT_SLIDER_LABELS, extra_tip=desc,
                                      labels=SLIDER_LABEL_OVERRIDES.get(slabel))
        rbtn = tk.Button(srow, text="Reset", font=("Segoe UI", 7), padx=4, pady=0,
                         command=lambda f=fields: _reset_section_fields(
                             cfg, f, field_vars, on_change, recompute))
        rbtn.grid(row=0, column=3, sticky="e", padx=(4, 0))
        Tooltip(rbtn, "Reset this stage's settings to the program defaults.")
        det.pack(fill="x", pady=(1, 0))                       # details below the slider
        out.append((sec, det))
    return out


def build_groups(parent, cfg, groups, field_vars: dict, on_change: Callable,
                 recompute: bool, opened=None,
                 sensitivity: bool = False) -> list[CollapsibleFrame]:
    """Build one CollapsibleFrame per (title, fields, description) in *groups*.

    *opened* is an optional set/list of titles to start expanded (default: all).
    *sensitivity* adds a 0-100 slider next to each guided float knob (see
    ``build_field_rows``).
    """
    out = []
    for title, fields, desc in groups:
        is_open = True if opened is None else (title in opened)
        cf = CollapsibleFrame(parent, title, description=desc, opened=is_open)
        cf.pack(fill="x", expand=True, pady=2)
        grid = tk.Frame(cf.body, padx=4, pady=2)
        grid.pack(fill="x")
        build_field_rows(grid, cfg, fields, field_vars, on_change, recompute,
                         sensitivity=sensitivity)
        out.append(cf)
    return out


# ---------------------------------------------------------------------------
# colour / alpha helpers + the per-layer panel
# ---------------------------------------------------------------------------
def rgb_to_hex(rgb) -> str:
    r, g, b = (int(c) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


class _AlphaControl:
    """A slider + entry that stay in sync, reporting [0, 1] to *on_change*."""

    def __init__(self, parent, init: float, on_change: Callable[[float], None]) -> None:
        self._on_change = on_change
        self._guard = False
        self.var = tk.DoubleVar(value=float(init))
        # shorter slider (was 90) so the wider layer-label column fits without growing the panel
        self.scale = ttk.Scale(parent, from_=0.0, to=1.0, variable=self.var,
                               command=self._from_scale, length=64)
        self.entry_var = tk.StringVar(value=f"{float(init):.2f}")
        self.entry = tk.Entry(parent, textvariable=self.entry_var, width=5)
        self.entry.bind("<Return>", self._from_entry)
        self.entry.bind("<FocusOut>", self._from_entry)

    def _from_scale(self, _v=None) -> None:
        if self._guard:
            return
        v = float(self.var.get())
        self._guard = True
        self.entry_var.set(f"{v:.2f}")
        self._guard = False
        self._on_change(v)

    def _from_entry(self, _evt=None) -> None:
        try:
            v = max(0.0, min(1.0, float(self.entry_var.get())))
        except ValueError:
            return
        self._guard = True
        self.var.set(v)
        self.entry_var.set(f"{v:.2f}")
        self._guard = False
        self._on_change(v)


def build_layers_panel(parent, cfg, show_vars: dict, on_change: Callable) -> None:
    """One row per overlay layer: ``[show] name [swatch] [alpha slider+entry]``.

    Colour/alpha edits write back to ``cfg.overlay`` and call *on_change* (a
    redraw-only callback). ``show_vars[key]`` holds each show toggle.
    """
    ov = cfg.overlay
    layer_defaults = getattr(cfg.gui, "layer_defaults", {}) or {}
    for i, (key, color_attr, alpha_attr, default) in enumerate(LAYER_PANEL_SPEC):
        sv = tk.BooleanVar(value=layer_defaults.get(key, default))
        show_vars[key] = sv
        # Slight gap above SABG+ to set the final result apart from the audit layers
        # (edge-rejected etc.). Applied to every widget in the row so it stays aligned.
        pad = (8, 1) if key == "sabg" else (1, 1)
        tk.Checkbutton(parent, variable=sv, command=on_change).grid(
            row=i, column=0, sticky="w", pady=pad)
        # width 16 fits the longest label ("candidate SABG+") without clipping the "+".
        tk.Label(parent, text=LAYER_LABELS[key], anchor="w", width=16).grid(
            row=i, column=1, sticky="w", pady=pad)

        # Short flat rectangle (was a full-height button) — lower-profile + more compact.
        swatch = tk.Frame(parent, width=22, height=10, relief="raised", bd=1,
                          bg=rgb_to_hex(getattr(ov, color_attr)), cursor="hand2")
        swatch.grid(row=i, column=2, padx=4, pady=pad)
        swatch.grid_propagate(False)

        def pick(_evt=None, ca=color_attr, sw=swatch):
            cur = getattr(ov, ca)
            res = colorchooser.askcolor(color=rgb_to_hex(cur), parent=parent)
            if res and res[0]:
                setattr(ov, ca, tuple(int(c) for c in res[0]))
                sw.configure(bg=rgb_to_hex(getattr(ov, ca)))
                on_change()

        swatch.bind("<Button-1>", pick)

        def set_alpha(v, aa=alpha_attr):
            setattr(ov, aa, float(v))
            on_change()

        ac = _AlphaControl(parent, getattr(ov, alpha_attr), set_alpha)
        ac.scale.grid(row=i, column=3, sticky="ew", padx=(4, 2), pady=pad)
        ac.entry.grid(row=i, column=4, padx=(0, 2), pady=pad)
    parent.columnconfigure(3, weight=1)


# ---------------------------------------------------------------------------
# section thumbnail picker
# ---------------------------------------------------------------------------
# section-list ordering offered by the picker (D7). "Scan order" is the file/scene
# order from Scan; the rest are convenience views. %SABG needs a results.csv.
SECTION_ORDER_MODES = ("Scan order", "Alias A–Z", "%SABG ↓")


def order_sections(entries, mode: str, out_dir=None):
    """Return *entries* reordered for the picker (same objects, so identity marks
    still match). Unknown/absent data falls back to scan order."""
    es = list(entries)
    if mode == "Alias A–Z":
        return sorted(es, key=lambda e: str(e.alias).lower())
    if mode == "%SABG ↓":
        pct = _results_pct(out_dir)
        if pct:
            return sorted(es, key=lambda e: pct.get(e.scene.key, -1.0), reverse=True)
    return es                                          # "Scan order" (default)


def results_available(out_dir) -> bool:
    """True when <out_dir>/results.csv exists (a full analysis has been run). Used to
    gate the "%SABG ↓" order mode, which needs those per-section percentages."""
    if not out_dir:
        return False
    from pathlib import Path
    return (Path(out_dir) / "results.csv").exists()


def sync_order_menu_state(option_menu, out_dir) -> None:
    """Grey out the "%SABG ↓" entry on a section-order OptionMenu unless a full
    analysis (results.csv) exists; without it that mode silently falls back to scan
    order, which is confusing. Matches the entry by substring so the ↓ arrow / any
    relabel can't break it."""
    try:
        menu = option_menu["menu"]
        state = "normal" if results_available(out_dir) else "disabled"
        end = menu.index("end")
        if end is None:
            return
        for i in range(end + 1):
            try:
                if "%SABG" in str(menu.entrycget(i, "label")):
                    menu.entryconfigure(i, state=state)
            except tk.TclError:
                pass
    except Exception:
        pass


def _results_pct(out_dir) -> dict:
    """{scene key -> pct_sabg} from <out_dir>/results.csv, or {} if unavailable."""
    if not out_dir:
        return {}
    import csv
    from pathlib import Path
    p = Path(out_dir) / "results.csv"
    if not p.exists():
        return {}
    out: dict = {}
    try:
        with open(p, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    out[row["key"]] = float(row["pct_sabg"])
                except (KeyError, ValueError, TypeError):
                    pass
    except Exception:
        return {}
    return out


class _PickerHandle:
    """Returned by :func:`thumbnail_picker`. Tracks each section's cell so the caller
    can mark the current section and step through them with the arrow keys."""
    _SEL_BG = "#dce9ff"
    _SEL_BORDER = "#1e6fff"

    def __init__(self, order, on_select):
        self.order = list(order)                       # entries in render order
        self.on_select = on_select
        self.cells: dict = {}                          # id(entry) -> (cell, label, button)
        self._base_bg = None
        self.current = None

    def _add(self, entry, cell, label, button) -> None:
        if self._base_bg is None:
            self._base_bg = cell.cget("background")
        self.cells[id(entry)] = (cell, label, button)

    def highlight(self, entry, scroll: bool = False) -> None:
        """Mark *entry*'s cell as current (border + tint + bold). *scroll* brings it into
        view — used for arrow-key stepping and the initial build, but NOT for a click, so
        clicking a section doesn't jerk the list to put that section on top."""
        self.current = entry
        for key, (cell, label, _btn) in self.cells.items():
            on = key == id(entry)
            bg = self._SEL_BG if on else self._base_bg
            try:
                # Keep the 2px border reserved always (colour it to match the cell when not
                # selected) so selecting a thumb doesn't shift it by the border width.
                cell.configure(background=bg, highlightthickness=2,
                               highlightbackground=self._SEL_BORDER if on else bg,
                               highlightcolor=self._SEL_BORDER if on else bg)
                label.configure(background=bg,
                                font=("Segoe UI", 7, "bold") if on
                                else ("Segoe UI", 7))
            except Exception:
                pass
        # Park keyboard focus on the current section's button so the <Up>/<Down>
        # bindings (attached per-button) actually fire; focus then follows the
        # selection on click, arrow-step, and initial build.
        rec = self.cells.get(id(entry))
        if rec is not None and rec[2] is not None:
            try:
                rec[2].focus_set()
            except Exception:
                pass
        if scroll:
            self._scroll_to(entry)

    def _scroll_to(self, entry) -> None:
        """Scroll the list MINIMALLY so *entry* is visible — only when it's off-screen, and
        just far enough to bring it into view (never yank it to the top)."""
        rec = self.cells.get(id(entry))
        if not rec:
            return
        cell = rec[0]
        try:                                           # interior -> canvas (ScrollFrame)
            canvas = cell.master.master
            cell.update_idletasks()
            total = cell.master.winfo_height() or 1
            view_h = canvas.winfo_height() or total
            view_top = canvas.yview()[0] * total
            view_bot = view_top + view_h
            c_top = cell.winfo_y()
            c_bot = c_top + cell.winfo_height()
            if c_top < view_top:                       # off the top -> show its top
                new_top = c_top
            elif c_bot > view_bot:                     # off the bottom -> show its bottom
                new_top = c_bot - view_h
            else:
                return                                 # already fully visible: don't move
            canvas.yview_moveto(max(0.0, new_top / total))
        except Exception:
            pass

    def step(self, delta: int) -> str:
        """Select the section *delta* away in the current order (wraps). Returns
        "break" so it can be used directly as a key binding."""
        if self.order:
            try:
                i = self.order.index(self.current)
            except ValueError:
                i = 0
            self.on_select(self.order[(i + delta) % len(self.order)])
            self._scroll_to(self.current)      # arrow-step keeps the new selection visible
        return "break"


def thumbnail_picker(parent, entries, on_select: Callable, photo_refs: list,
                     *, target_px: int = 150, selected=None) -> "_PickerHandle":
    """Render proportional section thumbnails into *parent* (a frame).

    *entries* are ``preview.SectionEntry`` (``.thumb_path``, ``.alias``,
    ``.skipped``). One shared integer subsample factor keeps thumbs proportional.
    PhotoImage refs are appended to *photo_refs* to keep them alive. Returns a
    :class:`_PickerHandle` so the caller can mark the current section and bind the
    arrow keys; the thumbnail buttons take focus and step prev/next with Up/Down.
    """
    handle = _PickerHandle(entries, on_select)
    have_thumbs = [e for e in entries if e.thumb_path.exists()]
    if not have_thumbs:
        tk.Label(parent, text="No thumbnails.\nRun Scan first.",
                 wraplength=160, fg="#a00").pack(pady=10)
        return handle
    # Load each PhotoImage once (to size the shared subsample factor) and reuse it.
    originals: dict = {}
    longest = 1
    for e in have_thumbs:
        try:
            img = tk.PhotoImage(file=str(e.thumb_path))
        except Exception:
            continue
        originals[e.thumb_path] = img
        longest = max(longest, img.width(), img.height())
    factor = max(1, -(-longest // target_px))          # ceil(longest / target_px)
    for e in entries:
        cell = tk.Frame(parent, padx=2, pady=3, highlightthickness=2)
        cell.configure(highlightbackground=cell.cget("background"))   # reserved, invisible border
        cell.pack(fill="x")
        button = None
        if e.thumb_path.exists():
            orig = originals.get(e.thumb_path)
            try:
                if orig is None:
                    raise OSError("thumb unreadable")
                img = orig.subsample(factor, factor)
                photo_refs.append(img)
                button = tk.Button(cell, image=img, relief="raised", takefocus=True,
                                   command=lambda en=e: on_select(en))
            except Exception:
                button = tk.Button(cell, text=e.alias, takefocus=True,
                                   command=lambda en=e: on_select(en))
            button.pack()
            button.bind("<Up>", lambda _ev: handle.step(-1))
            button.bind("<Down>", lambda _ev: handle.step(1))
        txt = e.alias + ("  (skip)" if e.skipped else "")
        lbl = tk.Label(cell, text=txt, font=("Segoe UI", 7),
                       fg="#888" if e.skipped else "#000")
        lbl.pack()
        handle._add(e, cell, lbl, button)
    if selected is not None:
        handle.highlight(selected, scroll=True)    # bring the initial selection into view
    return handle
