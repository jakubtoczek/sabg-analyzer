"""Minimal Tkinter GUI for the SABG Analyzer.

A thin front-end over the CLI (`python -m sabg_analyzer ...`). It does not import
the heavy pipeline; each action runs the CLI in a subprocess and streams its
output into the log pane, so the window stays responsive.

Launch:  python -m sabg_gui        (or double-click SABG_Analyzer.bat)

Buttons
  Data / Output  - browse to the .czi data folder and the output folder
  Scan           - detect sections, extract labels, write sections.csv
  Info           - (after scan) edit sections.csv in-app (thumbnail picker + table)
  Analyze        - (after scan) quantify %SABG, then (default) render the figures
                   in one timed pass; warns only if the animal-ID fields (or a
                   custom tag) are blank
  Export         - (after analyze) re-render the figures with the current config
  Stop/Resume    - Stop the running job; once stopped, Resume finishes the rest
                   (analyze + export), skipping sections already done
  Help           - open the README
  Config         - edit <output>/config.yaml in-app (detection tuning + settings)
  Log            - live, timestamped CLI output
"""

from __future__ import annotations

import csv
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

from sabg_analyzer import __version__   # cheap: package __init__ only defines the version

MAIN_DIR = Path(__file__).resolve().parent.parent   # repo root (holds sabg_analyzer, README, config)
DEFAULT_DATA = (MAIN_DIR.parent / "data").resolve()
DEFAULT_OUT = (MAIN_DIR.parent / "outputs").resolve()
README = MAIN_DIR / "README.md"
EXAMPLE_CFG = MAIN_DIR / "config.example.yaml"

# alias identity defaults (mirrors AliasParams; overridable via config.yaml)
DEFAULT_ID_FIELDS = ["animal", "group"]
DEFAULT_TAG_FIELD = "tag"
_SKIP_VALUES = {"no", "n", "false", "0", "skip", "off", "exclude"}

LIVE = "\x01"                       # sentinel: a self-replacing live progress line
DEFAULT_INFO_OPENS = ["sections", "labels"]
_INFO_TARGETS = {"sections": "sections.csv", "labels": "labels", "thumbs": "thumbs"}


def _seed_paths() -> tuple[str, str]:
    """Initial (data, out) folders from a `paths` block in <DEFAULT_OUT>/config.yaml or
    the example config, else the built-in defaults. Lets a user pin their folders once."""
    import yaml
    for src in (DEFAULT_OUT / "config.yaml", EXAMPLE_CFG):
        try:
            if src.exists():
                pa = (yaml.safe_load(src.read_text(encoding="utf-8")) or {}).get("paths") or {}
                data, out = pa.get("data_dir") or "", pa.get("out_dir") or ""
                if data or out:
                    return data or str(DEFAULT_DATA), out or str(DEFAULT_OUT)
        except Exception:
            pass
    return str(DEFAULT_DATA), str(DEFAULT_OUT)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.running = False
        self.q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        # Single per-session log file (mirrors this pane); opened lazily in the
        # output folder on the first run, buffering anything logged before then.
        self._session_stamp = time.strftime("%Y%m%d-%H%M%S")
        self._log_fh = None
        self._log_buffer: list[str] = []
        self._proc = None                # the running CLI subprocess (for Stop)
        self._stopped = False            # a run was stopped -> offer Resume even if
                                         # no section finished yet (nothing checkpointed)
        root.title(f"SABG Analyzer {__version__}")
        root.geometry("880x580")
        root.minsize(740, 470)

        data0, out0 = _seed_paths()         # config.paths overrides the built-in defaults
        self.data_var = tk.StringVar(value=data0)
        self.out_var = tk.StringVar(value=out0)

        self._build_paths()
        self._build_actions()
        self._build_status()       # bottom bar (packed before the log fills the rest)
        self._build_log()

        self.out_var.trace_add("write", lambda *_: self._refresh_state())
        self._refresh_state()
        self._log(f"=== session {time.strftime('%Y-%m-%d %H:%M:%S %z')} ===", stamp=False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_queue)

    # -- layout ------------------------------------------------------------
    def _build_paths(self) -> None:
        frm = tk.Frame(self.root, padx=10, pady=8)
        frm.pack(fill="x")
        for r, (label, var) in enumerate((("Data folder", self.data_var),
                                          ("Output folder", self.out_var))):
            tk.Label(frm, text=label, width=12, anchor="w").grid(row=r, column=0, sticky="w")
            tk.Entry(frm, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6, pady=2)
            tk.Button(frm, text="Browse…",
                      command=lambda v=var: self._browse(v)).grid(row=r, column=2)
        frm.columnconfigure(1, weight=1)

    def _build_actions(self) -> None:
        frm = tk.Frame(self.root, padx=10, pady=2)
        frm.pack(fill="x")
        self.btn: dict[str, tk.Button] = {}

        left = tk.Frame(frm)
        left.pack(side="left")
        right = tk.Frame(frm)               # utility buttons, pushed to the right
        right.pack(side="right")

        for name, cmd in (("Scan", self.on_scan), ("Info", self.on_info),
                          ("Preview", self.on_preview), ("Analyze", self.on_analyze),
                          ("Export", self.on_export)):
            b = tk.Button(left, text=name, width=9, command=cmd)
            b.pack(side="left", padx=3, pady=4)
            self.btn[name] = b
        # One toggle: "Stop" while a run is in progress, "Resume" when a stopped /
        # partial run can be continued (covers analyze + its bundled export).
        b = tk.Button(left, text="Stop", width=9, command=self.on_stop_resume)
        b.pack(side="left", padx=3, pady=4)
        self.btn["StopResume"] = b

        tk.Label(right, text=f"v{__version__}", fg="#888",
                 font=("Consolas", 8)).pack(side="left", padx=(0, 6))
        for name, cmd in (("Help", self.on_help), ("Config", self.on_config)):
            b = tk.Button(right, text=name, width=9, command=cmd)
            b.pack(side="left", padx=3, pady=4)
            self.btn[name] = b

    def _build_status(self) -> None:
        self.status = tk.Label(self.root, text="idle", anchor="w", relief="sunken",
                               font=("Consolas", 9), padx=8)
        self.status.pack(side="bottom", fill="x")

    def _build_log(self) -> None:
        frm = tk.Frame(self.root, padx=10, pady=6)
        frm.pack(fill="both", expand=True)
        tk.Label(frm, text="Log", anchor="w").pack(fill="x")
        self.log = scrolledtext.ScrolledText(frm, height=18, state="disabled",
                                             font=("Consolas", 9), bg="#101418", fg="#d6dde6")
        self.log.pack(fill="both", expand=True)

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text or "idle")

    # -- helpers -----------------------------------------------------------
    def _browse(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory(initialdir=var.get() or str(MAIN_DIR))
        if d:
            var.set(str(Path(d)))

    def _log(self, text: str, stamp: bool = True) -> None:
        prefix = time.strftime("[%H:%M:%S] ") if stamp else ""
        line = prefix + (text if text.endswith("\n") else text + "\n")
        self.log.configure(state="normal")
        self.log.insert("end", line)
        self.log.see("end")
        self.log.configure(state="disabled")
        self._persist(line)

    def _persist(self, line: str) -> None:
        """Append a log line to the session file (or buffer it until one opens)."""
        if self._log_fh is None:
            self._log_buffer.append(line)
            return
        try:
            self._log_fh.write(line)
            self._log_fh.flush()
        except Exception:
            pass

    def _ensure_session_log(self) -> None:
        """Open the single session log in the current output folder (once)."""
        if self._log_fh is not None:
            return
        try:
            out = Path(self.out_var.get())
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"session_{self._session_stamp}.log"
            self._log_fh = open(path, "a", encoding="utf-8")
            for buffered in self._log_buffer:
                self._log_fh.write(buffered)
            self._log_buffer.clear()
            self._log_fh.flush()
            self._log(f"[gui] session log: {path}")
        except Exception as exc:               # pragma: no cover - defensive
            self._log_fh = None
            self._log(f"[gui] could not open session log: {exc}")

    def _on_close(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
        self.root.destroy()

    def _sections_csv(self) -> Path:
        return Path(self.out_var.get()) / "sections.csv"

    def _results_csv(self) -> Path:
        return Path(self.out_var.get()) / "results.csv"

    def _refresh_state(self) -> None:
        scanned = self._sections_csv().exists()
        analyzed = self._results_csv().exists()
        busy = self.running

        def en(b): return "disabled" if busy else ("normal" if b else "disabled")
        self.btn["Scan"].configure(state="disabled" if busy else "normal")
        self.btn["Config"].configure(state="disabled" if busy else "normal")
        self.btn["Info"].configure(state=en(scanned))
        self.btn["Analyze"].configure(state=en(scanned))
        self.btn["Export"].configure(state=en(analyzed))
        # Stop while running; otherwise Resume (enabled once a partial run exists).
        sr = self.btn["StopResume"]
        if busy:
            sr.configure(text="Stop", state="normal")
        else:
            sr.configure(text="Resume",
                         state="normal" if (scanned and (analyzed or self._stopped))
                         else "disabled")

    # -- subprocess plumbing ----------------------------------------------
    def _run(self, cli_args: list[str], done=None) -> None:
        """Run `python -m sabg_analyzer <cli_args>` in a worker thread."""
        if self.running:
            return
        self.running = True
        self._stopped = False            # starting a fresh run clears the stop flag
        self._ensure_session_log()
        # The GUI keeps the single session log; tell the CLI not to add its own.
        cli_args = [*cli_args, "--no-run-log"]
        self._refresh_state()
        self._set_status("running…")
        self._log(f"$ python -m sabg_analyzer {' '.join(cli_args)}")

        def worker():
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-u", "-m", "sabg_analyzer", *cli_args],
                    cwd=str(MAIN_DIR), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                    encoding="utf-8", errors="replace")
                self._proc = proc
                for line in proc.stdout:            # type: ignore[union-attr]
                    self.q.put(("line", line.rstrip("\n")))
                proc.wait()
                self.q.put(("done", (proc.returncode, done)))
            except Exception as exc:                # pragma: no cover - defensive
                self.q.put(("line", f"[gui] error: {exc}"))
                self.q.put(("done", (1, done)))
            finally:
                self._proc = None

        threading.Thread(target=worker, daemon=True).start()

    def on_stop(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._stopped = True      # enable Resume even if no section finished yet
        self._log("[gui] stop requested - terminating the current run…")
        try:
            proc.terminate()      # finished sections are checkpointed; Resume continues
        except Exception as exc:
            self._log(f"[gui] could not stop: {exc}")

    def on_stop_resume(self) -> None:
        """One button: Stop the current run, or Resume a stopped/partial one."""
        if self.running:
            self.on_stop()
        else:
            self.on_continue()

    def on_continue(self) -> None:
        """Re-run analyze skipping sections already in results.csv (resume).

        Analyze now bundles export, so this resumes both phases: finished sections
        (in results.csv) are skipped and their figures (if already on disk) are not
        re-rendered."""
        args = ["analyze", "--data", self.data_var.get(),
                "--out", self.out_var.get(), "--progress", "--continue"]
        cfg = Path(self.out_var.get()) / "config.yaml"
        if cfg.exists():
            args += ["--config", str(cfg)]
        self._run(args, done=lambda rc: self._refresh_state())

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    line = str(payload)
                    if line.startswith(LIVE):
                        self._set_status(line[1:].strip())   # live: status bar only
                    else:
                        self._log(line)
                else:
                    rc, done = payload                # type: ignore[misc]
                    self.running = False
                    self._set_status(f"done (exit {rc})")
                    self._log(f"[exit {rc}]")
                    self._refresh_state()
                    if done:
                        done(rc)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    # -- button actions ----------------------------------------------------
    def on_config(self) -> None:
        """Open the in-app Config editor (detection tuning + other settings)."""
        out = Path(self.out_var.get())
        out.mkdir(parents=True, exist_ok=True)
        cfg = out / "config.yaml"
        try:
            from .info_config import ConfigWindow
            ConfigWindow(self.root, str(cfg), str(EXAMPLE_CFG))
            self._log("[gui] opened Config window")
        except Exception as exc:
            self._log(f"[gui] config window error: {exc}; opening the file instead")
            self._open_config_file()

    def _open_config_file(self) -> None:
        """Fallback: create config.yaml from the example if missing, then open it."""
        out = Path(self.out_var.get())
        out.mkdir(parents=True, exist_ok=True)
        cfg = out / "config.yaml"
        if not cfg.exists():
            if EXAMPLE_CFG.exists():
                shutil.copyfile(EXAMPLE_CFG, cfg)
                self._log(f"[gui] created {cfg} from config.example.yaml")
            else:
                cfg.write_text("# SABG Analyzer config\n", encoding="utf-8")
        self._open(cfg)

    def on_preview(self) -> None:
        """Open the in-process Preview/Tune window (live tuning on a ROI)."""
        out = Path(self.out_var.get())
        out.mkdir(parents=True, exist_ok=True)
        cfg = out / "config.yaml"
        if not cfg.exists() and EXAMPLE_CFG.exists():
            shutil.copyfile(EXAMPLE_CFG, cfg)
            self._log(f"[gui] created {cfg} from config.example.yaml")
        try:
            from .preview_gui import PreviewWindow
            PreviewWindow(self.root, self.data_var.get(), str(out), str(cfg))
            self._log("[gui] opened Preview/Tune window")
        except Exception as exc:
            messagebox.showerror(
                "Preview unavailable",
                f"Could not open the preview window:\n{exc}\n\n"
                "It needs matplotlib (see requirements.txt).")
            self._log(f"[gui] preview error: {exc}")

    def on_help(self) -> None:
        self._open(README)

    def on_scan(self) -> None:
        self._run(["scan", "--data", self.data_var.get(), "--out", self.out_var.get()],
                  done=lambda rc: self._refresh_state())

    def on_info(self) -> None:
        """Open the in-app Info editor (thumbnail picker + sections.csv table)."""
        out = Path(self.out_var.get())
        cfg = out / "config.yaml"
        try:
            from .info_config import InfoWindow
            InfoWindow(self.root, self.data_var.get(), str(out), str(cfg))
            self._log("[gui] opened Info window")
        except Exception as exc:
            self._log(f"[gui] info window error: {exc}; opening files instead")
            self._open_info_targets()

    def _open_info_targets(self) -> None:
        """Fallback: open the configured info targets (sections.csv, labels, …)."""
        out = Path(self.out_var.get())
        wanted = self._info_opens()
        opened = []
        for key in wanted:
            rel = _INFO_TARGETS.get(key)
            if not rel:
                continue
            target = out / rel
            if target.exists():
                self._open(target)
                opened.append(rel)
        if opened:
            self._log(f"[gui] info opened: {', '.join(opened)}")
        else:
            self._log(f"[gui] nothing to open for {wanted} (run Scan first?)")

    def on_analyze(self) -> None:
        if not self._identity_ok():
            ok = messagebox.askyesno(
                "Animal ID not filled",
                "Some sections to be analysed have no animal-identification "
                "fields and no custom tag in sections.csv.\n\n"
                "Their alias will fall back to the raw scene id.\n\n"
                "Run analyze anyway?")
            if not ok:
                return
        args = ["analyze", "--data", self.data_var.get(),
                "--out", self.out_var.get(), "--progress"]
        cfg = Path(self.out_var.get()) / "config.yaml"
        if cfg.exists():
            args += ["--config", str(cfg)]
        self._run(args, done=lambda rc: self._refresh_state())

    def on_export(self) -> None:
        args = ["export", "--data", self.data_var.get(), "--out", self.out_var.get()]
        cfg = Path(self.out_var.get()) / "config.yaml"
        if cfg.exists():
            args += ["--config", str(cfg)]
        self._run(args)

    # -- config-driven helpers --------------------------------------------
    def _read_config(self) -> dict:
        cfg = Path(self.out_var.get()) / "config.yaml"
        if cfg.exists():
            try:
                import yaml
                return yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
        return {}

    def _alias_cfg(self) -> tuple[list[str], str]:
        """(identification fields, tag field) — from config.yaml if present."""
        a = self._read_config().get("alias") or {}
        fields = a.get("fields") or list(DEFAULT_ID_FIELDS)
        tagf = a.get("tag_field") or DEFAULT_TAG_FIELD
        return fields, tagf

    def _info_opens(self) -> list[str]:
        g = self._read_config().get("gui") or {}
        return g.get("info_opens") or list(DEFAULT_INFO_OPENS)

    def _identity_ok(self) -> bool:
        """True unless a to-be-analyzed section lacks both a tag and the
        identification fields. Optional fields being blank does NOT warn."""
        sc = self._sections_csv()
        try:
            with sc.open(newline="", encoding="utf-8-sig") as fh:
                sample = fh.readline()
                fh.seek(0)
                sep = ";" if sample.count(";") > sample.count(",") else ","
                rows = list(csv.DictReader(fh, delimiter=sep))
        except OSError:
            return True
        if not rows:
            return True
        fields, tagf = self._alias_cfg()
        for row in rows:
            if str(row.get("analyze", "yes")).strip().lower() in _SKIP_VALUES:
                continue
            if str(row.get(tagf, "")).strip():
                continue
            if any(not str(row.get(f, "")).strip() for f in fields):
                return False
        return True

    def _open(self, path: Path) -> None:
        try:
            os.startfile(str(path))                  # Windows
        except AttributeError:                       # non-Windows fallback
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(path)])
        except OSError as exc:
            self._log(f"[gui] could not open {path}: {exc}")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
