"""Section metadata: emit the template, join user-filled labels, derive aliases.

`scan` writes ``sections.csv`` with one row per detected tissue section and
blank label columns. The user reads each slide's extracted ``Label`` image (and
the scene thumbnail), fills in the columns, and `analyze` joins them onto the
results and uses them to build a short, human-readable **alias** for each
section (used in ``results.csv`` and every output filename).

Columns the user fills:
  animal, group, tissue, treatment, day  - identification / grouping
  analyze                                - "yes" (default) / "no" to skip a section
  tag                                    - optional custom alias (overrides the default)
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

from .czi_io import SceneInfo

# Columns joined onto results / used for the alias.
LABEL_COLUMNS = ["animal", "group", "tissue", "treatment", "day", "tag"]
# Whether a section should be analysed at all.
CONTROL_COLUMNS = ["analyze"]

TEMPLATE_COLUMNS = [
    "file", "scene", "scene_thumb", "label_thumb",
    "animal", "group", "tissue", "treatment", "day",
    "analyze", "tag",
]

_SKIP_VALUES = {"no", "n", "false", "0", "skip", "off", "exclude"}


def _sniff_sep(path: str | Path) -> str:
    """Guess the CSV field delimiter (``,`` or ``;``) from the header line.

    The app writes ``,`` but a French/German-locale Excel re-saves with ``;``;
    we read either. We pick whichever of ``;`` / ``,`` / TAB splits the header
    into the most fields, defaulting to ``,``.
    """
    try:
        with open(path, "rb") as fh:
            first = fh.readline().decode("utf-8", "replace")
    except OSError:
        return ","
    counts = {sep: first.count(sep) for sep in (",", ";", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _read_table(path: str | Path) -> pd.DataFrame:
    """Read a metadata CSV tolerant of ``,``/``;`` delimiters, a UTF-8 BOM, and
    the cp1252/latin-1 encodings Excel sometimes writes. All cells are strings."""
    sep = _sniff_sep(path)
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(path, sep=sep, dtype=str, encoding=enc).fillna("")
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, sep=sep, dtype=str, encoding="latin-1").fillna("")


def write_sections_template(
    scenes: list[SceneInfo],
    out_csv: str | Path,
    thumb_rel: dict[str, str],
    label_rel: dict[str, str],
) -> Path:
    """Write the blank metadata template (preserving existing entries if present)."""
    out_csv = Path(out_csv)
    rows = [{
        "file": s.file_stem,
        "scene": s.scene_index,
        "scene_thumb": thumb_rel.get(s.slug, ""),
        "label_thumb": label_rel.get(s.file_stem, ""),
        "animal": "", "group": "", "tissue": "", "treatment": "", "day": "",
        "analyze": "yes", "tag": "",
    } for s in scenes]
    new = pd.DataFrame(rows, columns=TEMPLATE_COLUMNS)

    if out_csv.exists():  # keep anything the user already filled in
        old = _read_table(out_csv)
        if {"file", "scene"} <= set(old.columns):
            keyed = {(r["file"], str(r["scene"])): r for _, r in old.iterrows()}
            for i, r in new.iterrows():
                prev = keyed.get((r["file"], str(r["scene"])))
                if prev is None:
                    continue
                for c in LABEL_COLUMNS + CONTROL_COLUMNS:
                    if str(prev.get(c, "")).strip():
                        new.at[i, c] = prev[c]
        else:  # malformed / unreadable header -> don't crash, rewrite fresh
            print(f"  ! {out_csv.name} has no 'file'/'scene' columns "
                  f"(delimiter problem?) - rewriting a fresh template")

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    new.to_csv(out_csv, index=False)
    return out_csv


def load_metadata(csv_path: str | Path) -> dict[tuple[str, int], dict[str, str]]:
    """Return ``{(file_stem, scene): {label cols + analyze}}``."""
    df = _read_table(csv_path)
    if "file" not in df.columns:
        print(f"  ! {Path(csv_path).name}: no 'file' column (delimiter problem?) "
              f"- metadata ignored")
        return {}
    out: dict[tuple[str, int], dict[str, str]] = {}
    for _, r in df.iterrows():
        try:
            key = (r["file"], int(float(r["scene"])))
        except (ValueError, KeyError):
            continue
        out[key] = {c: r.get(c, "") for c in LABEL_COLUMNS + CONTROL_COLUMNS}
    return out


def section_skipped(md_row: dict[str, str] | None) -> bool:
    """True if the section's ``analyze`` cell says to skip it."""
    if not md_row:
        return False
    return str(md_row.get("analyze", "yes")).strip().lower() in _SKIP_VALUES


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------
def _sanitize(s: str) -> str:
    """Filesystem-/CSV-safe token."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(s).strip())
    return s.strip("-_.")


def build_aliases(scenes: list[SceneInfo],
                  metadata: dict[tuple[str, int], dict[str, str]] | None,
                  ap) -> dict[str, str]:
    """Map each ``scene.key`` to a short alias.

    ``ap`` is duck-typed (``AliasParams``): ``fields`` (always included),
    ``optional`` (added only to break collisions), ``spacer``, ``tag_field``.
    A non-empty tag overrides the default. With no usable info the alias falls
    back to ``scene.slug`` (so output still works before the CSV is filled).
    """
    metadata = metadata or {}
    sp = ap.spacer

    def md_of(s: SceneInfo) -> dict[str, str]:
        return metadata.get((s.file_stem, s.scene_index), {}) or {}

    def join(fields, md):
        toks = [_sanitize(md.get(f, "")) for f in fields]
        return sp.join(t for t in toks if t)

    # base alias per scene (before collision handling)
    base: dict[str, str] = {}
    is_tag: dict[str, bool] = {}
    for s in scenes:
        md = md_of(s)
        tag = _sanitize(md.get(ap.tag_field, ""))
        if tag:
            base[s.key], is_tag[s.key] = tag, True
        else:
            base[s.key], is_tag[s.key] = join(ap.fields, md), False

    # which base values are shared by >1 (default-built) scene?
    shared: dict[str, int] = defaultdict(int)
    for s in scenes:
        if not is_tag[s.key] and base[s.key]:
            shared[base[s.key]] += 1

    alias: dict[str, str] = {}
    for s in scenes:
        b = base[s.key]
        if is_tag[s.key]:
            alias[s.key] = b or s.slug
        elif not b:
            alias[s.key] = s.slug                       # no info -> slug
        elif shared[b] > 1:
            opt = join(ap.optional, md_of(s))           # disambiguate with optional
            alias[s.key] = sp.join([b, opt]) if opt else b
        else:
            alias[s.key] = b

    # guarantee global uniqueness (numeric suffix for any remaining duplicates)
    counts: dict[str, int] = defaultdict(int)
    for s in scenes:
        counts[alias[s.key]] += 1
    seen: dict[str, int] = defaultdict(int)
    for s in scenes:
        a = alias[s.key]
        if counts[a] > 1:
            seen[a] += 1
            alias[s.key] = f"{a}{sp}{seen[a]}"
    return alias
