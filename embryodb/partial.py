"""Python replacement for tools3's ``Partial.pl``.

Reads the ``partial_editing_code`` for a series from the embryoDB database
(not from the legacy ``embryoDB/<series>.xml``) and invokes the still-legacy
``partialCSV.jar`` to trim per-cell CSV tables under ``<annot_loc>/dats/``.

This is the first step in the legacy ``extract.sh`` chain to be ported to
Python. The underlying trimmer (``partialCSV.jar``) is unchanged — porting
its lineage-walk logic is part of the bigger v3 effort. What changes here:

- **Data source for the editing code**: read from the new DB (`Series
  .partial_editing_code`) instead of grepping the legacy XML's
  ``<checkedby>``. Series whose code was updated in the new GUI but never
  written back to the XML now pick up the correct value.
- **Validation grammar**: matches PartialCSV.java's own ``editArray`` parser
  (bare-time form gets ``P0:`` prepended), not Partial.pl's stricter regex.
  Sentinel values (``"n/a"``, empty, etc.) are treated as "no work to do"
  and exit cleanly so the rest of the extract chain continues.
- **Output placement**: ``partialCSV.jar`` writes to its cwd, so we run it
  from a scratch temp dir and move ``*<series>.csv`` into ``dats/``. This
  matches Partial.pl's net behaviour without its half-broken ``chdir``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import database
from .config import settings
from .queries import series as q_series


# Exit codes shared with the extract chain. Anything non-zero halts the chain
# (steps are joined with `&&`); 0 means "nothing to do" is fine to continue.
EXIT_OK = 0
EXIT_SERIES_NOT_FOUND = 2
EXIT_DATS_MISSING = 3
EXIT_JAR_MISSING = 4


@dataclass(frozen=True)
class PartialEditRule:
    cell_name: str
    time: int


def parse_editing_code(code: str | None) -> list[PartialEditRule] | None:
    """Parse a ``partial_editing_code`` string into edit rules.

    Format (matching ``PartialCSV.java`` ``editArray``):

    - Comma-separated entries.
    - Each entry: ``CellName:time``.
    - If the first character of the whole string is a digit, ``P0:`` is
      prepended (legacy bare-time form for a single entry).
    - ``time`` must parse as a non-negative integer.
    - Whitespace around entries is stripped.

    Returns the parsed rules, or ``None`` if the code is empty, a known
    sentinel, or syntactically invalid. ``None`` means "no work to do" — the
    caller should skip the trimming step but not treat this as an error.
    """
    if code is None:
        return None
    s = code.strip()
    if not s:
        return None
    # Sentinel values that mean "no editing has been done yet". Anything that
    # doesn't match the grammar below would also bail; the explicit checks
    # just keep the rejection message tidy.
    if s.lower() in {"n/a", "none", "na", "-"}:
        return None
    # Bare-time form: PartialCSV.java prepends 'P0:' when the first char is
    # a digit. Mirror that here. Note: this only makes a single-entry bare
    # form valid ("100" → "P0:100"); multi-entry bare forms like "100,200"
    # produce "P0:100,200" where the second entry lacks a colon and is
    # rejected below — same as the JAR (which throws AIOOBE).
    if s[0].isdigit():
        s = "P0:" + s
    rules: list[PartialEditRule] = []
    for raw in s.split(","):
        part = raw.strip()
        if not part or ":" not in part:
            return None
        cell, _, time_str = part.partition(":")
        cell = cell.strip()
        time_str = time_str.strip()
        if not cell or not time_str:
            return None
        try:
            t = int(time_str)
        except ValueError:
            return None
        if t < 0:
            return None
        rules.append(PartialEditRule(cell_name=cell, time=t))
    return rules


def canonicalize(rules: list[PartialEditRule]) -> str:
    """Render edit rules as the comma-separated ``CellName:time`` string
    that ``partialCSV.jar`` expects as its second argument."""
    return ",".join(f"{r.cell_name}:{r.time}" for r in rules)


def run_partial_for_series(
    series_name: str,
    *,
    java: str | None = None,
    tools4_dir: Path | None = None,
) -> int:
    """Apply ``partialCSV.jar`` trimming to one series. Returns the exit code.

    Returns ``EXIT_OK`` when:
      - The editing code is missing/invalid (no work to do — chain continues).
      - ``partialCSV.jar`` succeeds.

    Returns non-zero when:
      - The series isn't in the DB (``EXIT_SERIES_NOT_FOUND``).
      - ``annot_loc/dats/`` doesn't exist (``EXIT_DATS_MISSING``).
      - ``partialCSV.jar`` is missing (``EXIT_JAR_MISSING``).
      - ``partialCSV.jar`` exits non-zero (its own code is propagated).

    Output ``CD/CA/SCD/SCA<series>.csv`` files land in ``<annot_loc>/dats/``.
    """
    with database.session_scope() as session:
        row = q_series.get_by_name(session, series_name)
        if row is None:
            print(
                f"Partial: series {series_name!r} not found in DB",
                file=sys.stderr,
            )
            return EXIT_SERIES_NOT_FOUND
        code = row.partial_editing_code or ""
        annot_loc = row.annot_loc or ""

    rules = parse_editing_code(code)
    if rules is None:
        print(
            f"Partial: {series_name}: no valid partial_editing_code "
            f"(got {code!r}) — skipping"
        )
        return EXIT_OK

    dats = Path(annot_loc) / "dats"
    if not dats.is_dir():
        print(
            f"Partial: {series_name}: annot_loc/dats/ not found at {dats}",
            file=sys.stderr,
        )
        return EXIT_DATS_MISSING

    jar = Path(tools4_dir or settings.tools4_dir) / "partialCSV.jar"
    if not jar.exists():
        print(
            f"Partial: {series_name}: partialCSV.jar not found at {jar}",
            file=sys.stderr,
        )
        return EXIT_JAR_MISSING

    java_cmd = java or settings.java_command
    canonical = canonicalize(rules)
    print(f"Partial: {series_name}: trimming with code {canonical!r}")

    # PartialCSV.java opens the output FileOutputStream in cwd before
    # opening the input FileInputStream — if cwd == dats/, the OS truncates
    # the input file we then try to read. So we run from a scratch dir and
    # move the outputs into dats/ explicitly.
    with tempfile.TemporaryDirectory(prefix="embryodb-partial-") as tmp:
        proc = subprocess.run(
            [java_cmd, "-jar", str(jar), series_name, canonical],
            cwd=tmp,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            print(
                f"Partial: {series_name}: partialCSV.jar exited "
                f"{proc.returncode}",
                file=sys.stderr,
            )
            return proc.returncode

        # Outputs are 'CD<series>.csv', 'CA<series>.csv', 'SCD<series>.csv',
        # 'SCA<series>.csv' (and possibly nothing if the series has no
        # matching rows). Glob covers all four — and skips anything else.
        moved: list[str] = []
        for f in Path(tmp).glob(f"*{series_name}.csv"):
            target = dats / f.name
            shutil.move(str(f), str(target))
            moved.append(f.name)
        print(
            f"Partial: {series_name}: wrote {len(moved)} file(s) to {dats}: "
            + (", ".join(moved) if moved else "(none)")
        )

    return EXIT_OK


__all__ = [
    "EXIT_OK",
    "EXIT_SERIES_NOT_FOUND",
    "EXIT_DATS_MISSING",
    "EXIT_JAR_MISSING",
    "PartialEditRule",
    "canonicalize",
    "parse_editing_code",
    "run_partial_for_series",
]
