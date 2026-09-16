"""Stamp today's date as the version.

CalVer `YYYY.M.D` - no leading zeros, because PEP 440 normalises them away and
the file would then disagree with what pip reports. A second release on the
same day gets `.1`, `.2`, ...

The number lives in exactly one place, the package `__init__.py`; hatchling
reads it from there (`dynamic = ["version"]` in pyproject.toml), so packaging
and what the window title shows can never drift apart.

    python misc/bump.py            stamp today
    python misc/bump.py --check    report only, exit 1 if stale
    python misc/bump.py --tag      git-tag HEAD with the current version
    python misc/bump.py --test     self-check
"""
from __future__ import annotations

import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(r'^__version__ = "([^"]*)"$', re.M)


def version_file() -> Path:
    """The one `__init__.py` carrying a `__version__` line (src layout or flat)."""
    for p in sorted(ROOT.glob("src/*/__init__.py")) + sorted(ROOT.glob("*/__init__.py")):
        if LINE.search(p.read_text(encoding="utf-8")):
            return p
    sys.exit(f"no __init__.py with a __version__ line under {ROOT}")


def next_version(current: str, today: str) -> str:
    if current == today:
        return f"{today}.1"
    if current.startswith(f"{today}."):
        return f"{today}.{int(current.rsplit('.', 1)[1]) + 1}"
    return today


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return self_check()
    path = version_file()
    text = path.read_text(encoding="utf-8")
    current = LINE.search(text).group(1)

    if "--tag" in argv:
        subprocess.run(["git", "tag", f"v{current}"], cwd=ROOT, check=True)
        print(f"tagged v{current}")
        return 0

    d = datetime.date.today()
    today = f"{d.year}.{d.month}.{d.day}"
    if "--check" in argv:
        stale = not (current == today or current.startswith(f"{today}."))
        print(f"{current}{' (stale - run misc/bump.py)' if stale else ' (today)'}")
        return 1 if stale else 0

    new = next_version(current, today)
    path.write_text(text.replace(f'__version__ = "{current}"',
                                 f'__version__ = "{new}"', 1), encoding="utf-8")
    print(f"{current} -> {new}  ({path.relative_to(ROOT)})")
    return 0


def self_check() -> int:
    assert next_version("0.7.3", "2026.9.16") == "2026.9.16"        # first CalVer stamp
    assert next_version("2026.9.2", "2026.9.16") == "2026.9.16"     # a later day
    assert next_version("2026.9.16", "2026.9.16") == "2026.9.16.1"  # second today
    assert next_version("2026.9.16.1", "2026.9.16") == "2026.9.16.2"
    assert next_version("2026.9.16", "2026.9.1") == "2026.9.1"      # 16 is not "1."
    print("bump self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
