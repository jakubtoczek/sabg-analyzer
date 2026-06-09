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

import sabg_gui_widgets as gw
from sabg_analyzer import metadata, preview
from sabg_analyzer.config import load_config


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


class InfoWindow(tk.Toplevel):
    def __init__(self, master, data_dir: str, out_dir: str, config_path: str) -> None:
        super().__init__(master)
        self.title("SABG Info — sections.csv")
        self.geometry("1100x720")
        self.minsize(820, 520)

        self.data_dir = data_dir
        self.out_dir = Path(out_dir)
        self.csv_path = self.out_dir / "sections.csv"
        self.cfg = load_config(config_path if Path(config_path).exists() else None)

        self._photo_refs: list[tk.PhotoImage] = []
        self.cell_vars: dict[tuple[int, str], tk.Variable] = {}
        self._row_anchor: dict[tuple[str, str], tk.Widget] = {}

        if not self.csv_path.exists():
            tk.Label(self, text=f"No sections.csv in\n{self.out_dir}\n\nRun Scan first.",
                     fg="#a00", justify="center").pack(expand=True)
            return
        self.df = metadata._read_table(self.csv_path)
        for col in _RO_COLS + _EDIT_COLS:            # tolerate older/short headers
            if col not in self.df.columns:
                self.df[col] = ""

        self._build_layout()

    def _build_layout(self) -> None:
        # left: thumbnail picker | right: table editor + buttons
        left = tk.Frame(self, width=200)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="Sections", font=("Segoe UI", 9, "bold")).pack(pady=(6, 2))
        pick = gw.ScrollFrame(left)
        pick.pack(fill="both", expand=True)
        try:
            entries = preview.list_sections(self.data_dir, self.out_dir, self.cfg)
            gw.thumbnail_picker(pick.interior, entries, self._focus_section,
                                self._photo_refs)
        except Exception as exc:
            tk.Label(pick.interior, text=f"(picker error: {exc})",
                     wraplength=170, fg="red").pack()

        right = tk.Frame(self)
        right.pack(side="left", fill="both", expand=True)
        btns = tk.Frame(right, padx=6, pady=6)
        btns.pack(fill="x")
        tk.Button(btns, text="💾  Save sections.csv", font=("Segoe UI", 9, "bold"),
                  command=self.on_save).pack(side="left")
        tk.Button(btns, text="Open sections.csv", command=lambda: _open_file(self.csv_path)).pack(side="left", padx=6)
        tk.Button(btns, text="Reload", command=self._reload).pack(side="left")
        tk.Label(right, text="Tick 'analyze' to include a section; fill the identity "
                 "columns the alias is built from.", fg="#666",
                 font=("Segoe UI", 8)).pack(fill="x", padx=8)

        self.table = gw.ScrollFrame(right)
        self.table.pack(fill="both", expand=True)
        self._build_table(self.table.interior)

    def _build_table(self, parent: tk.Frame) -> None:
        cols = _RO_COLS + _EDIT_COLS
        for c, name in enumerate(cols):
            tk.Label(parent, text=name, font=("Segoe UI", 8, "bold"),
                     borderwidth=1, relief="groove", padx=3).grid(
                         row=0, column=c, sticky="ew")
        for i, (_, r) in enumerate(self.df.iterrows(), start=1):
            file_v, scene_v = str(r["file"]), str(r["scene"])
            tk.Label(parent, text=file_v, anchor="w", font=("Consolas", 8)).grid(
                row=i, column=0, sticky="w", padx=3)
            anchor = tk.Label(parent, text=scene_v, anchor="w", font=("Consolas", 8))
            anchor.grid(row=i, column=1, sticky="w", padx=3)
            self._row_anchor[(file_v, scene_v)] = anchor
            for c, col in enumerate(_EDIT_COLS, start=len(_RO_COLS)):
                if col == "analyze":
                    var = tk.BooleanVar(
                        value=not metadata.section_skipped({"analyze": r.get("analyze", "yes")}))
                    tk.Checkbutton(parent, variable=var).grid(row=i, column=c)
                else:
                    var = tk.StringVar(value=str(r.get(col, "")))
                    tk.Entry(parent, textvariable=var, width=10).grid(
                        row=i, column=c, sticky="ew", padx=1)
                self.cell_vars[(i, col)] = var
        for c in range(len(cols)):
            parent.columnconfigure(c, weight=1)

    def _focus_section(self, entry) -> None:
        key = (entry.scene.file_stem, str(entry.scene.scene_index))
        w = self._row_anchor.get(key)
        if w is None:
            return
        self.table.canvas.update_idletasks()
        total = max(1, self.table.interior.winfo_height())
        self.table.canvas.yview_moveto(max(0.0, w.winfo_y() / total))
        old = w.cget("background")
        w.configure(background="#fff3b0")
        self.after(1200, lambda: w.configure(background=old))

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
        for w in list(self.children.values()):
            w.destroy()
        self.__init__(self.master, self.data_dir, str(self.out_dir),
                      str(self.out_dir / "config.yaml"))


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
        src = self.cfg_path if self.cfg_path.exists() else (
            Path(example_path) if example_path and Path(example_path).exists() else None)
        self.cfg = load_config(str(src) if src else None)
        self.field_vars: dict[tuple[str, str], tk.Variable] = {}
        self.show_vars: dict[str, tk.BooleanVar] = {}

        btns = tk.Frame(self, padx=6, pady=6)
        btns.pack(fill="x")
        tk.Button(btns, text="💾  Save config.yaml", font=("Segoe UI", 9, "bold"),
                  command=self.on_save).pack(side="left")
        tk.Button(btns, text="Open config.yaml",
                  command=lambda: _open_file(self.cfg_path)).pack(side="left", padx=6)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        t1 = gw.ScrollFrame(nb)
        nb.add(t1, text="Detection tuning")
        gw.build_groups(t1.interior, self.cfg, gw.DETECTION_GROUPS, self.field_vars,
                        self._on_field, recompute=False,
                        opened={"1. Tissue", "4. SABG detection"})
        lay = tk.LabelFrame(t1.interior, text="Overlay layers (colour / alpha)",
                            padx=4, pady=2)
        lay.pack(fill="x", expand=True, pady=2)
        gw.build_layers_panel(lay, self.cfg, self.show_vars, lambda: None)

        t2 = gw.ScrollFrame(nb)
        nb.add(t2, text="Other settings")
        gw.build_groups(t2.interior, self.cfg, gw.OTHER_GROUPS, self.field_vars,
                        self._on_field, recompute=False)

    def _on_field(self, section, attr, kind, _recompute) -> None:
        gw.apply_field(self.cfg, section, attr, kind, self.field_vars[(section, attr)])

    def on_save(self) -> None:
        try:
            self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
            preview.export_config(self.cfg, self.cfg_path)
            messagebox.showinfo("Saved", f"Wrote {self.cfg_path}", parent=self)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)
