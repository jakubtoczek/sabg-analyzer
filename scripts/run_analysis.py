#!/usr/bin/env python
"""Convenience wrapper: scan (if needed) then analyze, with default paths.

    python scripts/run_analysis.py            # scan + analyze ../data -> outputs
    python scripts/run_analysis.py --config config.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sabg_analyzer.config import load_config           # noqa: E402
from sabg_analyzer.metadata import load_metadata        # noqa: E402
from sabg_analyzer import pipeline                       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="../data")
    ap.add_argument("--out", default="../outputs")
    ap.add_argument("--config", default=None)
    ap.add_argument("--skip-scan", action="store_true")
    args = ap.parse_args()

    sections = Path(args.out) / "sections.csv"
    if not args.skip_scan or not sections.exists():
        pipeline.scan(args.data, args.out, load_config(None))

    cfg = load_config(args.config)
    metadata = load_metadata(sections) if sections.exists() else None
    pipeline.analyze(args.data, args.out, cfg, metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
