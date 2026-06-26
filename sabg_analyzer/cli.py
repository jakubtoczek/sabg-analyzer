"""Command-line interface.

    python -m sabg_analyzer scan    --data ..\\data --out outputs
    python -m sabg_analyzer analyze --data ..\\data --out outputs [--config config.yaml]
                                    [--metadata outputs\\sections.csv] [--scene FILE:IDX]
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from pathlib import Path

from . import __version__
from .config import load_config
from .metadata import load_metadata
from .progress import LIVE
from . import pipeline


class _Tee:
    """Duplicate stdout to a file, but drop the self-replacing live progress
    lines (those starting with ``\\r`` or the ``LIVE`` sentinel) from the file."""

    def __init__(self, real, fh):
        self._real, self._fh = real, fh

    def write(self, s):
        self._real.write(s)
        if not (s[:1] == "\r" or s[:1] == LIVE):
            try:
                self._fh.write(s)
            except Exception:
                pass

    def flush(self):
        self._real.flush()
        try:
            self._fh.flush()
        except Exception:
            pass

    def isatty(self):
        return self._real.isatty()

    def __getattr__(self, name):
        return getattr(self._real, name)


@contextlib.contextmanager
def _run_log(out_dir, out_params):
    """Tee console output to a timestamped log file in *out_dir* (if enabled)."""
    if not getattr(out_params, "run_log", True):
        yield
        return
    fh = None
    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        fh = open(out / time.strftime(out_params.run_log_name), "a", encoding="utf-8")
    except Exception:
        yield
        return
    real = sys.stdout
    sys.stdout = _Tee(real, fh)
    try:
        print(f"# SABG Analyzer run log - {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
        yield
    finally:
        sys.stdout = real
        try:
            fh.close()
        except Exception:
            pass


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", default="../data", help="folder with .czi files")
    p.add_argument("--out", default="../outputs",
                   help="output folder (kept outside the code repo)")
    p.add_argument("--no-run-log", action="store_true",
                   help="don't write the per-run log file (the GUI keeps a single "
                        "session log instead)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sabg_analyzer",
        description="Quantify SA-beta-Gal positive area in brightfield CZI WSIs.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="detect sections, extract labels, write metadata template")
    _add_common(s)

    a = sub.add_parser("analyze", help="quantify %SABG per section")
    _add_common(a)
    a.add_argument("--config", default=None, help="YAML config with overrides")
    a.add_argument("--metadata", default=None, help="filled sections.csv to join")
    a.add_argument("--scene", default=None, help="only this scene, e.g. 2026_05_29__10215:0")
    a.add_argument("--no-progress", action="store_true", help="disable the progress/ETA bar")
    a.add_argument("--progress", action="store_true",
                   help="force the progress/ETA report on even when output is piped (e.g. GUI)")
    a.add_argument("--full-debug", action="store_true",
                   help="also write large standalone heatmaps (default: compact)")
    a.add_argument("--continue", "--reuse", dest="continue_run", action="store_true",
                   help="resume: skip sections already in results.csv and append the rest")
    a.add_argument("--no-cache", dest="no_cache", action="store_true",
                   help="ignore the per-section analysis cache (out/cache/) and recompute "
                        "every section even when its config is unchanged")
    a.add_argument("--export", dest="export_on_analyze", default=None,
                   action=argparse.BooleanOptionalAction,
                   help="run figure export after analyze (default on; --no-export keeps "
                        "the section overlays but skips the per-FOV crops)")

    e = sub.add_parser("export", help="export representative full-res FOV figures")
    _add_common(e)
    e.add_argument("--config", default=None, help="YAML config (reads its `export:` block)")
    # CLI flags default to None so an explicit flag overrides config; otherwise
    # the config's `export:` block (or the built-in default) is used.
    e.add_argument("--fov-um", type=float, default=None, help="FOV side in micrometres")
    e.add_argument("--scalebar-um", type=float, default=None, help="scale bar length (µm)")
    e.add_argument("--scalebar-label", default=None, action=argparse.BooleanOptionalAction,
                   help="draw the '100 µm' text (use --no-scalebar-label for a bare bar)")
    e.add_argument("--n", type=int, default=None, help="FOVs per section")
    e.add_argument("--min-tissue", type=float, default=None,
                   help="min tissue fraction for a FOV (0-1)")
    e.add_argument("--wb", default=None, action=argparse.BooleanOptionalAction,
                   help="write white-balanced figures")
    e.add_argument("--raw", default=None, action=argparse.BooleanOptionalAction,
                   help="write original-colour figures")
    e.add_argument("--plain", default=None, action=argparse.BooleanOptionalAction,
                   help="write the clean image without overlay")
    e.add_argument("--qc-overlay", dest="qc_overlay", default=None,
                   action=argparse.BooleanOptionalAction,
                   help="write a copy with the SABG+/artifact overlay burned in")
    e.add_argument("--formats", nargs="+", default=None, metavar="FMT",
                   help="output formats: tif and/or png (default tif)")
    e.add_argument("--scene", default=None, help="only this scene, e.g. 2026_05_29__10215:0")
    e.add_argument("--continue", "--reuse", dest="continue_run", action="store_true",
                   help="resume: skip scenes whose figures already exist on disk")

    return parser


def main(argv: list[str] | None = None) -> int:
    # Emit UTF-8 so non-ASCII (e.g. "µm") survives the GUI's UTF-8 pipe read and
    # the UTF-8 log file (Windows defaults stdout to cp1252 -> "µ" becomes "?").
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = build_parser().parse_args(argv)
    log_cfg = load_config(getattr(args, "config", None))
    if getattr(args, "no_run_log", False):
        log_cfg.output.run_log = False
    with _run_log(args.out, log_cfg.output):
        return _dispatch(args)


def _dispatch(args) -> int:
    if args.command == "scan":
        cfg = load_config(None)
        pipeline.scan(args.data, args.out, cfg)
        return 0

    if args.command == "analyze":
        cfg = load_config(args.config)
        if args.full_debug:
            cfg.full_debug = True
        if args.export_on_analyze is not None:
            cfg.output.export_on_analyze = args.export_on_analyze
        metadata = None
        if args.metadata:
            metadata = load_metadata(args.metadata)
        elif (default_md := Path(args.out) / "sections.csv").exists():
            metadata = load_metadata(default_md)
        show_progress = None
        if args.no_progress:
            show_progress = False
        elif args.progress:
            show_progress = True
        pipeline.analyze(args.data, args.out, cfg, metadata, only_scene=args.scene,
                         show_progress=show_progress,
                         continue_run=getattr(args, "continue_run", False),
                         use_cache=not getattr(args, "no_cache", False))
        return 0

    if args.command == "export":
        from .export import build_params, export
        cfg = load_config(args.config)
        # precedence: CLI flag (if given) > config `export:` block > dataclass default.
        # build_params drops None overrides, so an absent flag defers to the config.
        p = build_params(
            cfg,
            fov_um=args.fov_um, scalebar_um=args.scalebar_um,
            scalebar_label=args.scalebar_label, n_fov=args.n,
            min_tissue_frac=args.min_tissue, wb=args.wb, raw=args.raw,
            plain=args.plain, qc_overlay=args.qc_overlay,
            formats=(tuple(args.formats) if args.formats is not None else None),
        )
        export(args.data, args.out, p, cfg, only_scene=args.scene,
               resume=getattr(args, "continue_run", False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
