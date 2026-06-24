"""Normalize on-disk permissions for a series' files.

The Python pipeline writes through `fsutil` and already conforms to the project
policy (group `users`, 0664 files / 02775 setgid dirs). Tools that bypass
`fsutil` — the legacy Java/Perl jars, AceTree curation edits (especially over
sshfs from a remote Mac), manual edits — do not. This module re-applies the
policy after the fact so curation of a series can pass from one lab member to
another.

Shared by the CLI (`embryodb fix-permissions`), the GUI button, and the worker's
post-step hook so all three normalize identically. See the "File permission
policy" section of CLAUDE.md for the rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from . import fsutil
from .queries import series as q_series


@dataclass
class SeriesPermReport:
    name: str
    dirs: list[str] = field(default_factory=list)  # roots actually walked
    n_dirs: int = 0
    n_files: int = 0
    missing: bool = False  # series not found in the DB


def normalize_series(
    session: Session, name: str, *, dats_only: bool = False
) -> SeriesPermReport:
    """Re-apply the file-permission policy to one series' on-disk tree.

    By default normalizes both `annot_loc` and `image_loc` (deduped when they
    point at the same place). `dats_only=True` restricts to `<annot_loc>/dats`
    — the curation + extract outputs — which is the cheap path the worker uses
    after a legacy step writes there.
    """
    row = q_series.get_by_name(session, name)
    report = SeriesPermReport(name=name)
    if row is None:
        report.missing = True
        return report

    candidates: list[Path] = []
    if dats_only:
        if row.annot_loc:
            candidates.append(Path(row.annot_loc) / "dats")
    else:
        if row.annot_loc:
            candidates.append(Path(row.annot_loc))
        if row.image_loc:
            candidates.append(Path(row.image_loc))

    seen: set[Path] = set()
    for c in candidates:
        try:
            key = c.resolve()
        except OSError:
            key = c
        if key in seen or not c.exists():
            continue
        seen.add(key)
        nd, nf = fsutil.normalize_tree(c)
        report.dirs.append(str(c))
        report.n_dirs += nd
        report.n_files += nf
    return report
