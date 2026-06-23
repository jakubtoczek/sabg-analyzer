"""Info and Config windows for the SABG Analyzer GUI.

Both reuse the shared settings widgets (``sabg_gui_widgets``) so the tuning UI is
identical to the Preview window:

  * ``InfoWindow``   -- a section thumbnail picker beside an in-app editor for
    ``sections.csv`` (the metadata each section's alias is built from). Edits save
    back to the CSV; a button still opens the file directly as a fallback.
  * ``ConfigWindow`` -- a two-tab editor over ``config.yaml``: the detection tuning
    panel (same groups + layer rows as Preview) and an "Other settings" tab for the
    non-detection blocks. Saves via ``preview.export_config``; a button still opens
    the file directly.

Opened lazily from ``sabg_gui.py`` (so the heavy imports load only on demand).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

import cv2
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from . import widgets as gw
from sabg_analyzer import metadata, preview
from sabg_analyzer.config import load_config


def _read_rgb(path) -> np.ndarray | None:
    """Read an image as RGB uint8, or None if it can't be read."""
    if path is None:
        return None
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return None if bgr is None else cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def _selectable(parent, text: str = "", **kw) -> tk.Entry:
    """A read-only Entry styled to read like a Label but with copy-selectable text.

    Flat, borderless and background-matched to *parent* so it looks like a label; the
    width tracks the content so it lays out like one. Update via `_set_selectable`."""
    e = tk.Entry(parent, relief="flat", borderwidth=0, highlightthickness=0,
                 readonlybackground=parent.cget("background"),
                 width=max(1, len(text)), **kw)
    e.insert(0, text)
    e.configure(state="readonly")
    return e


def _set_selectable(entry: tk.Entry, text: str) -> None:
    """Replace the text of a `_selectable` read-only Entry and resize it to fit."""
    entry.configure(state="normal")
    entry.delete(0, "end")
    entry.insert(0, text)
    entry.configure(state="readonly", width=max(1, len(text)))


def _open_file(path: Path) -> None:
    try:
        os.startfile(str(path))                      # Windows
    except AttributeError:                           # non-Windows fallback
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path)])


# ---------------------------------------------------------------------------
# Info: thumbnail picker + sections.csv table editor
# ---------------------------------------------------------------------------
_RO_COLS = ["file", "scene"]
_EDIT_COLS = ["analyze", "animal", "group", "tissue", "treatment", "day", "tag"]
# Customizable Info-table columns (gui.info_columns). `#` = 1-based scan index (derived,
# not saved); file/scene/analyze are always shown. Grid weights make `file` wide and the
# rest narrow (others default to 2); the same weights drive header + body so they align.
_ALL_COLS = ["#", "file", "scene", "analyze", "animal", "group", "tissue", "treatment", "day", "tag"]
_LOCKED_COLS = ("file", "scene", "analyze")
_COL_WEIGHT = {"#": 1, "file": 6, "scene": 1, "analyze": 1}


def _resolve_info_columns(requested) -> list[str]:
    """Validate a configured column order: keep known columns (in order, de-duped, drop
    unknowns) and force the always-shown columns in if a hand-edited config dropped them."""
    seen: set[str] = set()
    cols = [c for c in (requested or []) if c in _ALL_COLS and not (c in seen or seen.add(c))]
    for c in _LOCKED_COLS:
        if c not in seen:
            cols.append(c)
    return cols or list(_ALL_COLS)


class InfoWindow(tk.Toplevel):
    def __init__(self, master, data_dir: str, out_dir: str, config_path: str) -> None:
        super().__init__(master)
        self.title("SABG Info — labels & sections.csv")
        self.geometry("1320x780")
        self.minsize(1000, 580)
        self.data_dir = data_dir
        self.out_dir = Path(out_dir)
        self.config_path = config_path
        self.csv_path = self.out_dir / "sections.csv"
        self._build()

    def _build(self) -> None:
        for w in list(self.children.values()):
            w.destroy()
        self.cfg = load_config(
            self.config_path if Path(self.config_path).exists() else None)
        self._photo_refs: list[tk.PhotoImage] = []
        self.cell_vars: dict[tuple[int, str], tk.Variable] = {}
        self._row_cells: dict[tuple[str, str], list] = {}  # all cell widgets per row (band + scroll)
        self._hl_cells: list[tk.Widget] = []             # currently-highlighted row cells
        self._hl_base: dict[tk.Widget, str] = {}         # widget -> its un-highlighted bg
        self.entries: list = []
        self.cur_idx = 0
        self._picker = None                           # picker handle (marker + arrow nav)
        self.order_mode = tk.StringVar(value=gw.SECTION_ORDER_MODES[0])
        self._raw = {"label": None, "thumb": None}   # raw RGB per viewer tab
        self._rot = {"label": 0, "thumb": 0}          # 90° rotations per tab
        self.figs = {}; self.axes = {}; self.canvases = {}; self.navs = {}

        if not self.csv_path.exists():
            tk.Label(self, text=f"No sections.csv in\n{self.out_dir}\n\nRun Scan first.",
                     fg="#a00", justify="center").pack(expand=True)
            return
        self.df = metadata._read_table(self.csv_path)
        for col in _RO_COLS + _EDIT_COLS:            # tolerate older/short headers
            if col not in self.df.columns:
                self.df[col] = ""
        self._cols = _resolve_info_columns(getattr(self.cfg.gui, "info_columns", None))
        self._build_layout()

    def _build_layout(self) -> None:
        # left: picker | centre: viewer | right: big table editor -- resizable via sashes.
        paned = tk.PanedWindow(self, orient="horizontal", sashrelief="raised", sashwidth=6)
        paned.pack(fill="both", expand=True)

        left = tk.Frame(paned)
        tk.Label(left, text="Sections", font=("Segoe UI", 9, "bold")).pack(pady=(6, 2))
        orow = tk.Frame(left)
        orow.pack(fill="x", padx=4)
        tk.Label(orow, text="order", font=("Segoe UI", 8)).pack(side="left")
        self._order_menu = ttk.OptionMenu(
            orow, self.order_mode, gw.SECTION_ORDER_MODES[0], *gw.SECTION_ORDER_MODES,
            command=lambda _v: self._populate_picker())
        self._order_menu.pack(side="left", fill="x", expand=True)
        gw.sync_order_menu_state(self._order_menu, self.out_dir)   # grey %SABG if no results.csv
        self._pick = gw.ScrollFrame(left)
        self._pick.pack(fill="both", expand=True)
        try:
            self.entries = preview.list_sections(self.data_dir, self.out_dir, self.cfg)
        except Exception as exc:
            self.entries = []
            tk.Label(self._pick.interior, text=f"(picker error: {exc})",
                     wraplength=160, fg="red").pack()
        self._populate_picker()

        center = tk.Frame(paned)
        self._build_viewer(center)

        right = tk.Frame(paned)
        btns = tk.Frame(right, padx=6, pady=6)
        btns.pack(fill="x")
        tk.Button(btns, text="💾  Save sections.csv", font=("Segoe UI", 9, "bold"),
                  command=self.on_save).pack(side="left")
        tk.Button(btns, text="Open", command=lambda: _open_file(self.csv_path)).pack(side="left", padx=6)
        tk.Button(btns, text="Reload", command=self._reload).pack(side="left")
        tk.Button(btns, text="Uncheck all", command=lambda: self._set_all_analyze(False)).pack(side="left", padx=(12, 2))
        tk.Button(btns, text="Check all", command=lambda: self._set_all_analyze(True)).pack(side="left")
        tk.Button(btns, text="?", width=2, command=self._show_help).pack(side="right")
        tk.Label(right, text="Tick 'analyze' to include a section; fill the identity "
                 "columns the alias is built from.", fg="#666", justify="left",
                 wraplength=580, font=("Segoe UI", 8)).pack(fill="x", padx=8)
        hdr = tk.Frame(right)                   # pinned column header (never scrolls)
        hdr.pack(fill="x", padx=(0, 16))        # pad ~ scrollbar width so columns line up
        self._build_table_header(hdr)
        self.table = gw.ScrollFrame(right)
        self.table.pack(fill="both", expand=True)
        self._build_table(self.table.interior)

        paned.add(left, minsize=120, width=168, stretch="never")
        paned.add(center, minsize=300, stretch="always")
        paned.add(right, minsize=320, width=600, stretch="never")

        if self.entries:
            self._show_section(0)

    # -- label / thumb viewer ----------------------------------------------
    def _build_viewer(self, center: tk.Frame) -> None:
        bar = tk.Frame(center)
        bar.pack(fill="x")
        tk.Button(bar, text="◀ prev", command=lambda: self._step(-1)).pack(side="left", padx=2, pady=2)
        tk.Button(bar, text="next ▶", command=lambda: self._step(1)).pack(side="left", padx=2)
        self.view_title = _selectable(bar, "—", font=("Segoe UI", 9, "bold"))
        self.view_title.pack(side="left", padx=10)
        tk.Button(bar, text="reset", command=self._reset_view).pack(side="right", padx=2)
        tk.Button(bar, text="zoom −", command=lambda: self._zoom_btn(1.25)).pack(side="right", padx=2)
        tk.Button(bar, text="zoom +", command=lambda: self._zoom_btn(0.8)).pack(side="right", padx=2)
        tk.Button(bar, text="↻ 90°", command=lambda: self._rotate(1)).pack(side="right", padx=2)
        tk.Button(bar, text="↺ 90°", command=lambda: self._rotate(-1)).pack(side="right", padx=2)

        self.nb = ttk.Notebook(center)
        self.nb.pack(fill="both", expand=True)
        for tabkey, label in (("label", "Label"), ("thumb", "Thumbs")):
            tab = tk.Frame(self.nb)
            self.nb.add(tab, text=label)
            fig = Figure(figsize=(5, 5), tight_layout=True)
            ax = fig.add_subplot(111); ax.set_axis_off()
            canvas = FigureCanvasTkAgg(fig, master=tab)
            canvas.get_tk_widget().pack(fill="both", expand=True)
            self.figs[tabkey] = fig; self.axes[tabkey] = ax
            self.canvases[tabkey] = canvas
            self.navs[tabkey] = gw.CanvasNav(canvas, ax)

    def _label_path(self, entry) -> Path | None:
        p = self.out_dir / "labels" / f"{entry.scene.file_stem}_label.png"
        return p if p.exists() else None

    def _cur_tab(self) -> str:
        try:
            return "label" if self.nb.index(self.nb.select()) == 0 else "thumb"
        except tk.TclError:
            return "label"

    def _populate_picker(self) -> None:
        """(Re)build the left thumbnail list in the chosen order. The table keeps the
        scan order; only the picker display reorders (same entry objects, so the
        current-section marker still matches by identity)."""
        if not hasattr(self, "_pick"):
            return
        for w in self._pick.interior.winfo_children():
            w.destroy()
        self._photo_refs.clear()
        if not self.entries:
            self._picker = None
            return
        ordered = gw.order_sections(self.entries, self.order_mode.get(), self.out_dir)
        cur = self.entries[self.cur_idx] if 0 <= self.cur_idx < len(self.entries) else None
        numbers = {e.scene.key: i + 1 for i, e in enumerate(self.entries)}  # scan-order #
        self._picker = gw.thumbnail_picker(
            self._pick.interior, ordered, self._on_pick, self._photo_refs,
            selected=cur, numbers=numbers)
        if getattr(self, "_order_menu", None) is not None:
            gw.sync_order_menu_state(self._order_menu, self.out_dir)

    def _on_pick(self, entry) -> None:
        for i, e in enumerate(self.entries):
            if e.scene.key == entry.scene.key:
                self._show_section(i)
                return

    def _step(self, d: int) -> None:
        if self._picker is not None and self._picker.order:
            self._picker.step(d)              # follow the chosen list order
        elif self.entries:
            self._show_section((self.cur_idx + d) % len(self.entries))

    def _show_section(self, idx: int) -> None:
        if not self.entries:
            return
        self.cur_idx = idx % len(self.entries)
        entry = self.entries[self.cur_idx]
        if self._picker is not None:
            self._picker.highlight(entry)             # mark the current section in the list
        # labels are scanned sideways: start them at the configured CCW quarter-turn
        # (default 1 = upright); the rotate buttons still compose on top of this.
        self._rot = {"label": int(self.cfg.gui.label_rotate_quarter_turns) % 4, "thumb": 0}
        _set_selectable(
            self.view_title, f"{entry.alias}   [{self.cur_idx + 1}/{len(self.entries)}]")
        self._raw["label"] = _read_rgb(self._label_path(entry))
        self._raw["thumb"] = _read_rgb(
            entry.thumb_path if entry.thumb_path.exists() else None)
        self._redraw_tab("label")
        self._redraw_tab("thumb")
        self._focus_section(entry)

    def _redraw_tab(self, tabkey: str) -> None:
        ax = self.axes[tabkey]
        ax.clear(); ax.set_axis_off()
        img = self._raw[tabkey]
        if img is None:
            ax.text(0.5, 0.5, "(no image — run Scan)", ha="center", va="center",
                    color="#999", fontsize=11)
        else:
            ax.imshow(np.rot90(img, self._rot[tabkey]) if self._rot[tabkey] else img)
        self.navs[tabkey].clear_home()
        self.canvases[tabkey].draw_idle()
        self.navs[tabkey].set_home()

    def _rotate(self, direction: int) -> None:
        t = self._cur_tab()
        self._rot[t] = (self._rot[t] + direction) % 4
        self._redraw_tab(t)

    def _zoom_btn(self, factor: float) -> None:
        ax = self.axes[self._cur_tab()]
        x0, x1 = ax.get_xlim(); y0, y1 = ax.get_ylim()
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        ax.set_xlim(cx - (cx - x0) * factor, cx + (x1 - cx) * factor)
        ax.set_ylim(cy - (cy - y0) * factor, cy + (y1 - cy) * factor)
        self.canvases[self._cur_tab()].draw_idle()

    def _reset_view(self) -> None:
        self.navs[self._cur_tab()].reset()

    def _apply_col_weights(self, frame: tk.Frame) -> None:
        # Same per-column weights on header + body so they stay aligned; `file` (6) is wide,
        # `scene`/`#`/`analyze` (1) narrow, identity columns (2) medium. uniform keeps the two
        # grids proportional. ponytail: weight-aligned, not pixel-perfect.
        for c, name in enumerate(self._cols):
            frame.columnconfigure(c, weight=_COL_WEIGHT.get(name, 2), uniform="tbl")

    def _build_table_header(self, parent: tk.Frame) -> None:
        """Pinned column header (its own frame above the scroll area, so it never scrolls)."""
        for c, name in enumerate(self._cols):
            tk.Label(parent, text=name, font=("Segoe UI", 8, "bold"),
                     borderwidth=1, relief="groove", padx=3).grid(row=0, column=c, sticky="ew")
        self._apply_col_weights(parent)

    def _build_table(self, parent: tk.Frame) -> None:
        for i, (_, r) in enumerate(self.df.iterrows(), start=1):
            file_v, scene_v = str(r["file"]), str(r["scene"])
            row_widgets = []
            for c, col in enumerate(self._cols):
                if col in ("#", "file", "scene"):
                    val = str(i) if col == "#" else (file_v if col == "file" else scene_v)
                    w = _selectable(parent, val, font=("Consolas", 8))
                    w.grid(row=i, column=c, sticky="w", padx=3)
                elif col == "analyze":
                    var = tk.BooleanVar(
                        value=not metadata.section_skipped({"analyze": r.get("analyze", "yes")}))
                    w = tk.Checkbutton(parent, variable=var)
                    w.grid(row=i, column=c)
                    self.cell_vars[(i, col)] = var
                else:
                    var = tk.StringVar(value=str(r.get(col, "")))
                    w = tk.Entry(parent, textvariable=var, width=10)
                    w.grid(row=i, column=c, sticky="ew", padx=1)
                    self.cell_vars[(i, col)] = var
                row_widgets.append(w)
            self._row_cells[(file_v, scene_v)] = row_widgets
        self._apply_col_weights(parent)

    @staticmethod
    def _bg_opt(w) -> str:
        # State-aware: a readonly Entry only shows `readonlybackground`; normal-state
        # widgets (Checkbutton, editable Entry) show `background`. Picking the right one
        # makes the highlight render across the whole row, not just the flat readonly cells.
        try:
            if str(w.cget("state")) == "readonly" and "readonlybackground" in w.keys():
                return "readonlybackground"
        except tk.TclError:
            pass
        return "background" if "background" in w.keys() else "bg"

    def _highlight_row(self, cells) -> None:
        """Persistently highlight the selected row (yellow band across every cell) and clear the
        previously-highlighted row. Persistent (not a brief flash) so the selected section is
        always visible; bases are captured once so colours can't drift."""
        keep = set(cells)
        for w in self._hl_cells:                # un-highlight the old row
            if w not in keep:
                try:
                    w.configure(**{self._bg_opt(w): self._hl_base.get(w, "white")})
                except tk.TclError:
                    pass
        for w in cells:
            opt = self._bg_opt(w)
            self._hl_base.setdefault(w, w.cget(opt))
            try:
                w.configure(**{opt: "#fff3b0"})
            except tk.TclError:
                pass
        self._hl_cells = list(cells)

    def _focus_section(self, entry) -> None:
        key = (entry.scene.file_stem, str(entry.scene.scene_index))
        cells = self._row_cells.get(key)
        if not cells:
            return
        self._highlight_row(cells)              # persistent yellow band across the whole row
        scene_cell = cells[self._cols.index("scene")]   # scroll the selected row into view
        self.table.canvas.update_idletasks()
        total = max(1, self.table.interior.winfo_height())
        self.table.canvas.yview_moveto(max(0.0, scene_cell.winfo_y() / total))

    def _set_all_analyze(self, value: bool) -> None:
        """Tick / untick every section's analyze box at once (bulk include/exclude)."""
        for (_i, col), var in self.cell_vars.items():
            if col == "analyze":
                var.set(value)

    def on_save(self) -> None:
        for (i, col), var in self.cell_vars.items():
            ridx = self.df.index[i - 1]
            if col == "analyze":
                self.df.at[ridx, col] = "yes" if var.get() else "no"
            else:
                self.df.at[ridx, col] = var.get()
        try:
            self.df.to_csv(self.csv_path, index=False)
            messagebox.showinfo("Saved", f"Wrote {self.csv_path}", parent=self)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)

    def _reload(self) -> None:
        self._build()

    def _show_help(self) -> None:
        gw.help_popup(self, "Info — help", [
            ("Sections list",
             "Click a thumbnail on the left to load that section in the viewer and "
             "jump to its row in the table. prev/next steps through sections."),
            ("Label / Thumbs viewer",
             "Two tabs — the slide Label image (what identifies a slide) and the "
             "section Thumbnail. Wheel zooms to the cursor; middle/right-drag pans; "
             "the toolbar rotates 90° left/right, zooms in/out, and resets the view."),
            ("Resolution",
             "Labels and thumbs are shown at the resolution Scan saved them; zoom in "
             "for detail (they can't be re-read finer from the CZI)."),
            ("Table",
             "Edit the identity columns the alias is built from and tick 'analyze' to "
             "include a section. 'Save sections.csv' writes your edits back."),
        ])


# ---------------------------------------------------------------------------
# Config: two-tab editor over config.yaml
# ---------------------------------------------------------------------------
class ConfigWindow(tk.Toplevel):
    def __init__(self, master, config_path: str, example_path: str | None = None) -> None:
        super().__init__(master)
        self.title("SABG Config — config.yaml")
        self.geometry("520x780")
        self.minsize(460, 560)

        self.cfg_path = Path(config_path)
        self.example_path = example_path
        self.field_vars: dict[tuple[str, str], tk.Variable] = {}
        self.show_vars: dict[str, tk.BooleanVar] = {}
        self._build()

    def _build(self) -> None:
        src = self.cfg_path if self.cfg_path.exists() else (
            Path(self.example_path) if self.example_path
            and Path(self.example_path).exists() else None)
        self.cfg = load_config(str(src) if src else None)
        self.field_vars.clear()
        self.show_vars.clear()

        btns = tk.Frame(self, padx=6, pady=6)
        btns.pack(fill="x")
        tk.Button(btns, text="💾  Save config.yaml", font=("Segoe UI", 9, "bold"),
                  command=self.on_save).pack(side="left")
        tk.Button(btns, text="Open config.yaml",
                  command=lambda: _open_file(self.cfg_path)).pack(side="left", padx=6)
        tk.Button(btns, text="Reload", command=self._reload).pack(side="left")
        tk.Button(btns, text="?", width=2, command=self._show_help).pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        t1 = gw.ScrollFrame(nb)
        nb.add(t1, text="Detection tuning")
        # Layers at the TOP of the tab (Jakub expects the overlay layers up there);
        # fill="x" (no expand) so the frame doesn't balloon and leave a gap.
        lay = tk.LabelFrame(t1.interior, text="Overlay layers (colour / alpha)",
                            padx=4, pady=2)
        lay.pack(fill="x", pady=2)
        gw.build_layers_panel(lay, self.cfg, self.show_vars, lambda: None)
        gw.build_groups(t1.interior, self.cfg, gw.DETECTION_GROUPS, self.field_vars,
                        self._on_field, recompute=False,
                        opened={"1. Tissue", "2. SABG detection"})

        t2 = gw.ScrollFrame(nb)
        nb.add(t2, text="Other settings")
        gw.build_groups(t2.interior, self.cfg, gw.OTHER_GROUPS, self.field_vars,
                        self._on_field, recompute=False)
        colf = tk.LabelFrame(t2.interior, text="Info table columns", padx=4, pady=2)
        colf.pack(fill="x", pady=2)
        self._info_cols = _resolve_info_columns(getattr(self.cfg.gui, "info_columns", None))
        self._build_info_col_editor(colf)

        # Export tab: bridge the free-form Config.export dict via a DictObj proxy.
        from sabg_analyzer.pipeline import _export_snapshot
        self._export_dict = _export_snapshot(self.cfg.export)   # defaults + overrides
        self._export_obj = gw.DictObj(self._export_dict)
        self.export_vars: dict[tuple[str, str], tk.Variable] = {}
        t3 = gw.ScrollFrame(nb)
        nb.add(t3, text="Export")
        gw.build_groups(t3.interior, self._export_obj, gw.EXPORT_GROUPS,
                        self.export_vars, self._on_export_field, recompute=False)

    def _reload(self) -> None:
        for w in list(self.children.values()):
            w.destroy()
        self._build()

    def _on_field(self, section, attr, kind, _recompute) -> None:
        gw.apply_field(self.cfg, section, attr, kind, self.field_vars[(section, attr)])

    def _on_export_field(self, section, attr, kind, _recompute) -> None:
        gw.apply_field(self._export_obj, section, attr, kind,
                       self.export_vars[(section, attr)])

    # -- Info-table column editor (gui.info_columns) -----------------------
    def _build_info_col_editor(self, parent: tk.Frame) -> None:
        """Add / remove / reorder the Info table's columns. file/scene/analyze are locked
        (always shown). Reopen the Info window to see changes after Save."""
        row = tk.Frame(parent); row.pack(fill="x")
        self._col_list = tk.Listbox(row, height=7, exportselection=False)
        self._col_list.pack(side="left", fill="x", expand=True)
        side = tk.Frame(row); side.pack(side="left", padx=4)
        tk.Button(side, text="▲", width=3, command=lambda: self._move_info_col(-1)).pack()
        tk.Button(side, text="▼", width=3, command=lambda: self._move_info_col(1)).pack()
        tk.Button(side, text="Remove", command=self._remove_info_col).pack(pady=(4, 0))
        addrow = tk.Frame(parent); addrow.pack(fill="x", pady=(2, 0))
        tk.Label(addrow, text="add:").pack(side="left")
        self._col_add = tk.StringVar(value="")
        self._col_add_menu = ttk.OptionMenu(addrow, self._col_add, "")
        self._col_add_menu.pack(side="left")
        tk.Label(parent, text="file, scene, analyze always shown · '#' = scan index · "
                 "reopen Info to apply", fg="#666", font=("Segoe UI", 8)).pack(anchor="w")
        self._refresh_info_col_editor()

    def _refresh_info_col_editor(self) -> None:
        self._col_list.delete(0, "end")
        for c in self._info_cols:
            self._col_list.insert("end", c + ("  (locked)" if c in _LOCKED_COLS else ""))
        menu = self._col_add_menu["menu"]; menu.delete(0, "end")
        avail = [c for c in _ALL_COLS if c not in self._info_cols]
        for c in avail:
            menu.add_command(label=c, command=lambda v=c: self._add_info_col(v))
        self._col_add.set(avail[0] if avail else "")
        self.cfg.gui.info_columns = list(self._info_cols)   # kept in sync for Save

    def _move_info_col(self, d: int) -> None:
        sel = self._col_list.curselection()
        if not sel:
            return
        i = sel[0]; j = i + d
        if 0 <= j < len(self._info_cols):
            self._info_cols[i], self._info_cols[j] = self._info_cols[j], self._info_cols[i]
            self._refresh_info_col_editor()
            self._col_list.selection_set(j)

    def _remove_info_col(self) -> None:
        sel = self._col_list.curselection()
        if not sel:
            return
        c = self._info_cols[sel[0]]
        if c in _LOCKED_COLS:
            messagebox.showinfo("Columns", f"'{c}' is always shown.", parent=self)
            return
        self._info_cols.pop(sel[0]); self._refresh_info_col_editor()

    def _add_info_col(self, name: str) -> None:
        if name and name not in self._info_cols:
            self._info_cols.append(name); self._refresh_info_col_editor()

    def on_save(self) -> None:
        try:
            self.cfg.export = dict(self._export_dict)   # fold the Export tab back in
            self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
            preview.export_config(self.cfg, self.cfg_path)
            messagebox.showinfo("Saved", f"Wrote {self.cfg_path}", parent=self)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)

    def _show_help(self) -> None:
        gw.help_popup(self, "Config — help", [
            ("Detection tuning",
             "The same pipeline-ordered stages as the Preview (tissue → artifact → "
             "fold → SABG detection → edge). Hover any field for its tooltip. The "
             "Overlay layers control the colours/alpha used in figures."),
            ("Other settings",
             "Canvas sizing (µm/px + px caps), which artifacts analyze/export write "
             "(incl. maps_um_per_px, keep_maps), progress, GUI knobs and how the alias "
             "is built from sections.csv."),
            ("Export",
             "How `export` builds figures. FOV crops: each qualifying FOV (≥ "
             "min_tissue_frac tissue, near the section mean %SABG) is written in the "
             "colour bases you enable (raw and/or white-balanced) × the variants (plain "
             "and/or qc_overlay) × formats — so wb+plain and raw+qc gives 2 files per "
             "FOV. Section figures: one file per sec_variants entry, each a set of "
             "underscore-joined tokens (base raw|wb, overlay overlay|overlaysabg, fov "
             "boxes, scalebar), e.g. wb_overlay_fov_scalebar."),
            ("Save / Reload",
             "Save writes the full config.yaml (every block, directly re-loadable). "
             "Reload re-reads config.yaml, discarding unsaved edits. Per-scene overrides "
             "live under scenes.<file:idx> and are preserved."),
        ])
