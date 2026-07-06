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

import grp
import os
import stat
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


# --- read-only audit --------------------------------------------------------

# The "potentially modifiable" subtrees under a series root, relative to
# annot_loc / image_loc. Deliberately EXCLUDES tif/ tifR/ — the raw-image trees
# hold tens of thousands of files each, so walking them across many series would
# blow up (CLAUDE.md: never enumerate the image tree wholesale). These four are
# the curation + analysis outputs a lab member re-saves during a handoff.
AUDIT_SUBPATHS: tuple[str, ...] = ("dats", "matlab", "MLtemp", "matlabParams")


@dataclass
class PermIssue:
    """One on-disk entry that violates the file-permission policy."""

    path: str
    is_dir: bool
    problems: list[str] = field(default_factory=list)


@dataclass
class SeriesAuditReport:
    name: str
    roots: list[str] = field(default_factory=list)  # subtrees actually scanned
    n_dirs: int = 0
    n_files: int = 0
    issues: list[PermIssue] = field(default_factory=list)
    missing: bool = False  # series not found in the DB
    no_paths: bool = False  # series exists but none of the target subpaths do

    @property
    def n_issues(self) -> int:
        return len(self.issues)


def _target_gid(group: str = fsutil.DEFAULT_GROUP) -> int | None:
    """gid the policy wants files to carry. Unlike `fsutil.resolve_gid` this
    does NOT require the current user to be a member — an audit only compares,
    it never chgrps."""
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        return None


def _entry_problems(path: Path, *, is_dir: bool, gid_want: int | None) -> list[str]:
    """Compare one entry against the policy (group `users`, group-writable,
    setgid on dirs). Returns a list of human-readable problems, empty if clean.

    ACL presence (the `+` in `ls -l`) is intentionally NOT checked: the GPFS
    mounts set a default ACL on every entry, so it would flag everything and
    signal nothing.
    """
    st = path.lstat()  # never follow symlinks
    mode = stat.S_IMODE(st.st_mode)
    problems: list[str] = []
    if gid_want is not None and st.st_gid != gid_want:
        problems.append(f"group={st.st_gid} (want {gid_want})")
    if not (mode & 0o020):
        problems.append("not group-writable")
    if is_dir and not (mode & 0o2000):
        problems.append("no setgid")
    if mode & 0o002:
        # Over-permissive rather than blocking, but the policy is group-write
        # only; flag so fix-permissions tightens it to 0664.
        problems.append("world-writable")
    return problems


def _audit_tree(root: Path, gid_want: int | None, report: SeriesAuditReport) -> None:
    """Stat every entry under `root` (a file or a dir) against the policy.

    Bounded by design — `root` is one of AUDIT_SUBPATHS, never a raw-image tree.
    """
    if root.is_symlink():
        return
    if root.is_file():
        report.n_files += 1
        probs = _entry_problems(root, is_dir=False, gid_want=gid_want)
        if probs:
            report.issues.append(PermIssue(str(root), False, probs))
        return
    if not root.is_dir():
        return
    report.n_dirs += 1
    probs = _entry_problems(root, is_dir=True, gid_want=gid_want)
    if probs:
        report.issues.append(PermIssue(str(root), True, probs))
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            p = Path(dirpath) / d
            if p.is_symlink():
                continue
            report.n_dirs += 1
            pr = _entry_problems(p, is_dir=True, gid_want=gid_want)
            if pr:
                report.issues.append(PermIssue(str(p), True, pr))
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_symlink():
                continue
            report.n_files += 1
            pr = _entry_problems(p, is_dir=False, gid_want=gid_want)
            if pr:
                report.issues.append(PermIssue(str(p), False, pr))


def audit_series(session: Session, name: str) -> SeriesAuditReport:
    """Read-only check of one series' modifiable files against the policy.

    Scans `<annot_loc>/dats`, `<image_loc>/matlab`, `<image_loc>/MLtemp` and the
    `matlabParams` file (probing both roots, deduped) — the subtrees a curation
    handoff re-saves — and reports every entry that isn't group `users` +
    group-writable (dirs setgid). Touches nothing; `normalize_series` /
    `embryodb fix-permissions` is the mutating twin that applies the fix.
    """
    row = q_series.get_by_name(session, name)
    report = SeriesAuditReport(name=name)
    if row is None:
        report.missing = True
        return report

    gid_want = _target_gid()
    bases = [b for b in (row.annot_loc, row.image_loc) if b]
    roots: list[Path] = []
    seen: set[Path] = set()
    for base in bases:
        for sub in AUDIT_SUBPATHS:
            c = Path(base) / sub
            try:
                key = c.resolve()
            except OSError:
                key = c
            if key in seen or not c.exists():
                continue
            seen.add(key)
            roots.append(c)
    report.roots = [str(r) for r in roots]
    if not roots:
        report.no_paths = True
        return report
    for r in roots:
        _audit_tree(r, gid_want, report)
    return report
