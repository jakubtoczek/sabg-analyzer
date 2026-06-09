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
        self.interior.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # mousewheel only while the pointer is inside this region
        self.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>",
                                                            self._on_wheel))
        self.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event) -> None:
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
                 opened: bool = True, **kw) -> None:
        super().__init__(parent, relief="groove", borderwidth=1, **kw)
        self._title = title
        self._opened = bool(opened)
        self._btn = tk.Button(self, anchor="w", relief="flat", font=("Segoe UI", 9, "bold"),
                              command=self.toggle)
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
    """Wheel-zoom-to-cursor + middle/right-drag pan on one matplotlib axes."""

    def __init__(self, canvas, ax) -> None:
        self.canvas = canvas
        self.ax = ax
        self._home = None
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

# (title, fields, description) -- the detection groups, shared by Preview + Config.
DETECTION_GROUPS = [
    ("1. Tissue", TISSUE_FIELDS,
     "Separate stained tissue from glass / black mosaic gaps; clean and reclaim faint interior tissue."),
    ("2. Artifact / dark folds", ARTIFACT_FIELDS,
     "Flag dark, non-teal fold/debris pixels and exclude them from counting."),
    ("3. Fold (linear ridges)", FOLD_FIELDS,
     "Detect thin linear tissue folds and optionally drop them from the denominator."),
    ("4. SABG detection", DETECT_FIELDS,
     "Threshold the SABG score on tissue, then grow seeds into connected faint teal."),
    ("5. Edge-shadow rejection", EDGE_FIELDS,
     "Reject thin dark edge-shadow rims wrongly counted as positive."),
]

# Layers drawn in pipeline order: (key, colour attr, alpha attr, default show).
LAYER_SPEC = [
    ("nontissue", "nontissue_color", "nontissue_alpha", True),
    ("artifact", "artifact_color", "artifact_alpha", True),
    ("fold", "fold_color", "fold_alpha", True),
    ("sabg", "sabg_color", "sabg_alpha", True),
    ("edge_removed", "edge_color", "edge_alpha", True),
]
LAYER_LABELS = {"nontissue": "non-tissue", "artifact": "artifact", "fold": "fold",
                "sabg": "SABG+", "edge_removed": "edge-rejected"}

# ---------------------------------------------------------------------------
# "Slider setup" mode: one guided sensitivity bar per layer.
# Each knob = (section, attr, value@0, value@100, label). The slider runs
# 0 (detect LESS) -> 100 (detect MORE); value@0/value@100 bake in the direction
# (e.g. texture_min DROPS as sensitivity rises). The first knob is the layer's
# primary (simple mode); the rest appear only in "advanced (raw knobs)" mode.
# Mappings + ranges per the session-7 handover §8 (centred on the config defaults).
SLIDER_LAYERS = [
    ("tissue", [("tissue", "texture_min", 0.012, 0.001, "texture_min"),
                ("tissue", "bg_margin", 0.16, 0.04, "bg_margin")]),
    ("SABG+", [("threshold", "scale", 1.30, 0.50, "threshold.scale"),
               ("detection", "hyst_low_scale", 0.90, 0.20, "hyst_low_scale")]),
    ("artifact", [("artifact", "dark_level", 0.30, 0.60, "dark_level")]),
    ("fold", [("fold", "score_min", 0.15, 0.02, "score_min")]),
    ("edge-reject", [("edge", "teal_keep", 0.20, 0.02, "teal_keep")]),
]


def slider_to_value(v0: float, v100: float, s: float) -> float:
    """Sensitivity *s* in [0, 100] -> the knob value (linear v0..v100)."""
    return v0 + (v100 - v0) * (max(0.0, min(100.0, s)) / 100.0)


def value_to_slider(v0: float, v100: float, cur: float) -> float:
    """Inverse of ``slider_to_value`` (knob value -> slider position 0..100)."""
    if v100 == v0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (cur - v0) / (v100 - v0)))

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
OUTPUT_FIELDS = [
    ("output", "debug", "bool", "debug", "Write debug/<alias>_compare.jpg (6-panel audit)."),
    ("output", "maps", "bool", "maps", "Write maps/<alias>_* (consumed by export)."),
    ("output", "keep_maps", "bool", "keep_maps", "Keep maps/ after export."),
    ("output", "export_on_analyze", "bool", "export_on_analyze", "Run export automatically after analyze."),
    ("output", "log_files", "bool", "log_files", "Log the files written at each step."),
    ("output", "run_log", "bool", "run_log", "Tee the console output to a timestamped log file."),
    ("output", "run_log_name", "str", "run_log_name", "strftime template for the run-log filename."),
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
    ("Progress", PROGRESS_FIELDS, "What the progress reporter prints."),
    ("GUI", GUI_FIELDS, "GUI-only knobs."),
    ("Alias", ALIAS_FIELDS, "How each section's short alias is built from sections.csv."),
]

# ---------------------------------------------------------------------------
# Export options. `Config.export` is a free-form dict, so these fields use
# section "" and are edited through a `DictObj` proxy (below) over the effective
# export dict; mirrors `export.ExportParams`.
# ---------------------------------------------------------------------------
EXPORT_FOV_FIELDS = [
    ("", "n_fov", "int", "n_fov", "Number of representative FOV crops per section."),
    ("", "fov_um", "float", "fov_um", "FOV side length (µm)."),
    ("", "min_tissue_frac", "float", "min_tissue_frac",
     "Min tissue fraction (0-1) a FOV must contain; FOVs are picked close to the section average."),
    ("", "scalebar_um", "float", "scalebar_um", "FOV scale-bar length (µm)."),
    ("", "scalebar_label", "bool", "scalebar_label", "Draw the FOV scale-bar label text."),
    ("", "wb", "bool", "wb", "Write white-balanced FOV figures."),
    ("", "raw", "bool", "raw", "Write original-colour FOV figures."),
    ("", "plain", "bool", "plain", "Write the clean FOV image without overlay."),
    ("", "qc_overlay", "bool", "qc_overlay", "Write a FOV copy with the SABG+/artifact overlay."),
    ("", "formats", "list", "formats", "FOV output formats (comma separated, e.g. jpg, png)."),
]
EXPORT_SECTION_FIELDS = [
    ("", "section_figures", "bool", "section_figures", "Render whole-section overlay figures."),
    ("", "section_um_per_px", "float", "section_um_per_px", "Section-figure resolution (µm/px)."),
    ("", "sec_variants", "list", "sec_variants",
     "Section figure variants (comma separated: raw, wb_scalebar, wb_overlay_fov_scalebar)."),
    ("", "sec_formats", "list", "sec_formats", "Section figure formats (comma separated)."),
    ("", "sec_scalebar_um", "float", "sec_scalebar_um", "Section scale-bar length (µm)."),
    ("", "sec_scalebar_adaptive", "bool", "sec_scalebar_adaptive",
     "Snap the section scale bar to a nice value near sec_scalebar_um."),
    ("", "sec_scalebar_label", "bool", "sec_scalebar_label", "Draw the section scale-bar label."),
]
EXPORT_GROUPS = [
    ("FOV crops", EXPORT_FOV_FIELDS,
     "Representative full-resolution FOV crops (≥ min_tissue_frac tissue, near the section mean)."),
    ("Section figures", EXPORT_SECTION_FIELDS,
     "Whole-section overlay figures rendered from the maps."),
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
                     recompute: bool) -> None:
    """Lay out label+editor rows for *fields* into *parent* (a frame).

    ``field_vars[(section, attr)]`` is populated with the tk.Variable and
    ``on_change(section, attr, kind, recompute)`` is invoked on every edit.
    """
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
        field_vars[(section, attr)] = var
        Tooltip(w, tip)
    parent.columnconfigure(1, weight=1)


def apply_field(cfg, section: str, attr: str, kind: str, var: tk.Variable) -> bool:
    """Read *var*, coerce by *kind*, set on the config. Returns True on success."""
    obj = getattr(cfg, section) if section else cfg
    try:
        val = parse_field(kind, var.get())
    except (ValueError, tk.TclError):
        return False                          # mid-typing; ignore until valid
    setattr(obj, attr, val)
    return True


def build_groups(parent, cfg, groups, field_vars: dict, on_change: Callable,
                 recompute: bool, opened=None) -> list[CollapsibleFrame]:
    """Build one CollapsibleFrame per (title, fields, description) in *groups*.

    *opened* is an optional set/list of titles to start expanded (default: all).
    """
    out = []
    for title, fields, desc in groups:
        is_open = True if opened is None else (title in opened)
        cf = CollapsibleFrame(parent, title, description=desc, opened=is_open)
        cf.pack(fill="x", expand=True, pady=2)
        grid = tk.Frame(cf.body, padx=4, pady=2)
        grid.pack(fill="x")
        build_field_rows(grid, cfg, fields, field_vars, on_change, recompute)
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
        self.scale = ttk.Scale(parent, from_=0.0, to=1.0, variable=self.var,
                               command=self._from_scale, length=90)
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
    for i, (key, color_attr, alpha_attr, default) in enumerate(LAYER_SPEC):
        sv = tk.BooleanVar(value=default)
        show_vars[key] = sv
        tk.Checkbutton(parent, variable=sv, command=on_change).grid(
            row=i, column=0, sticky="w")
        tk.Label(parent, text=LAYER_LABELS[key], anchor="w", width=12).grid(
            row=i, column=1, sticky="w")

        swatch = tk.Button(parent, width=2, relief="raised",
                           bg=rgb_to_hex(getattr(ov, color_attr)))

        def pick(ca=color_attr, btn=swatch):
            cur = getattr(ov, ca)
            res = colorchooser.askcolor(color=rgb_to_hex(cur), parent=parent)
            if res and res[0]:
                setattr(ov, ca, tuple(int(c) for c in res[0]))
                btn.configure(bg=rgb_to_hex(getattr(ov, ca)))
                on_change()

        swatch.configure(command=pick)
        swatch.grid(row=i, column=2, padx=4)

        def set_alpha(v, aa=alpha_attr):
            setattr(ov, aa, float(v))
            on_change()

        ac = _AlphaControl(parent, getattr(ov, alpha_attr), set_alpha)
        ac.scale.grid(row=i, column=3, sticky="ew", padx=(4, 2))
        ac.entry.grid(row=i, column=4, padx=(0, 2))
    parent.columnconfigure(3, weight=1)


# ---------------------------------------------------------------------------
# section thumbnail picker
# ---------------------------------------------------------------------------
def thumbnail_picker(parent, entries, on_select: Callable, photo_refs: list,
                     *, target_px: int = 150) -> None:
    """Render proportional section thumbnails into *parent* (a frame).

    *entries* are ``preview.SectionEntry`` (``.thumb_path``, ``.alias``,
    ``.skipped``). One shared integer subsample factor keeps thumbs proportional.
    PhotoImage refs are appended to *photo_refs* to keep them alive.
    """
    have_thumbs = [e for e in entries if e.thumb_path.exists()]
    if not have_thumbs:
        tk.Label(parent, text="No thumbnails.\nRun Scan first.",
                 wraplength=160, fg="#a00").pack(pady=10)
        return
    longest = 1
    for e in have_thumbs:
        try:
            img = tk.PhotoImage(file=str(e.thumb_path))
            longest = max(longest, img.width(), img.height())
        except Exception:
            pass
    factor = max(1, -(-longest // target_px))          # ceil(longest / target_px)
    for e in entries:
        cell = tk.Frame(parent, padx=2, pady=3)
        cell.pack(fill="x")
        if e.thumb_path.exists():
            try:
                img = tk.PhotoImage(file=str(e.thumb_path)).subsample(factor, factor)
                photo_refs.append(img)
                tk.Button(cell, image=img, relief="raised",
                          command=lambda en=e: on_select(en)).pack()
            except Exception:
                tk.Button(cell, text=e.alias,
                          command=lambda en=e: on_select(en)).pack()
        txt = e.alias + ("  (skip)" if e.skipped else "")
        tk.Label(cell, text=txt, font=("Segoe UI", 7),
                 fg="#888" if e.skipped else "#000").pack()
