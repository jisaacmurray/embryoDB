"""Audit operations that build trust in the safe mirror.

Each audit answers a single question:
- audit_import:        does every imported series round-trip byte-perfectly
                       to its source XML?
- compare_with_source: for one series, diff the exported XML against the
                       source-dir original.
- find_duplicates:     are there series_name collisions or near-duplicates
                       (e.g. trailing whitespace, case-folding, file vs row)?
- validate_paths:      do image_loc / annot_loc / acetree_config point to
                       things that exist on disk?
- missing_images:      per-dataset coverage report — which series have no
                       image data on disk?
- missing_annots:      same, for AceTree annotations.
"""

from __future__ import annotations

import filecmp
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .exporters.xml_exporter import _content_for_row  # type: ignore[reportPrivateImportUsage]
from .models import Dataset, Series


@dataclass
class AuditReport:
    matched: list[str] = field(default_factory=list)
    byte_diffs: list[str] = field(default_factory=list)
    missing_source: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"matched: {len(self.matched)}, "
            f"byte diffs: {len(self.byte_diffs)}, "
            f"missing source: {len(self.missing_source)}, "
            f"errors: {len(self.errors)}"
        )


def audit_import(
    session: Session,
    source_dir: Path | None = None,
) -> AuditReport:
    """For every Series row, regenerate the XML content and compare byte-for-
    byte against the source-dir file recorded at import time.

    Writes nothing to disk. Source-dir is never modified.
    """
    src = Path(source_dir or settings.source_dir)
    rep = AuditReport()
    for row in session.execute(select(Series)).scalars():
        try:
            generated = _content_for_row(row)
        except Exception as exc:
            rep.errors.append((row.series_name, f"generate: {exc}"))
            continue
        source_path = Path(row.xml_source_path) if row.xml_source_path else None
        if source_path is None or not source_path.exists():
            # Fall back to the configured source_dir using the series name.
            candidate = src / f"{row.series_name}.xml"
            if not candidate.exists():
                rep.missing_source.append(row.series_name)
                continue
            source_path = candidate
        try:
            original = source_path.read_text(encoding="utf-8")
        except Exception as exc:
            rep.errors.append((row.series_name, f"read source: {exc}"))
            continue
        if generated == original:
            rep.matched.append(row.series_name)
        else:
            rep.byte_diffs.append(row.series_name)
    return rep


def compare_with_source(
    session: Session,
    series_name: str,
    source_dir: Path | None = None,
) -> tuple[bool, Path, Path]:
    """For one series, write its current DB representation to a temp file and
    diff it against source-dir. Returns (matches, generated_path, source_path).
    Caller is responsible for inspecting / deleting the temp file.
    """
    src = Path(source_dir or settings.source_dir)
    row = session.execute(
        select(Series).where(Series.series_name == series_name)
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"no series named {series_name!r}")
    source_path = Path(row.xml_source_path) if row.xml_source_path else src / f"{series_name}.xml"
    if not source_path.exists():
        raise FileNotFoundError(f"source XML not found: {source_path}")
    tmp = Path(tempfile.mkstemp(suffix=".xml", prefix=f"{series_name}-")[1])
    tmp.write_text(_content_for_row(row), encoding="utf-8")
    matches = filecmp.cmp(tmp, source_path, shallow=False)
    return matches, tmp, source_path


@dataclass
class DuplicateReport:
    exact: list[tuple[str, str]] = field(default_factory=list)  # (a, b) — collision
    case_fold: list[tuple[str, str]] = field(default_factory=list)
    whitespace: list[tuple[str, str]] = field(default_factory=list)
    file_without_row: list[str] = field(default_factory=list)
    row_without_file: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"exact: {len(self.exact)}, "
            f"case-fold: {len(self.case_fold)}, "
            f"whitespace: {len(self.whitespace)}, "
            f"file w/o row: {len(self.file_without_row)}, "
            f"row w/o file: {len(self.row_without_file)}"
        )


def find_duplicates(
    session: Session,
    source_dir: Path | None = None,
) -> DuplicateReport:
    src = Path(source_dir or settings.source_dir)
    rep = DuplicateReport()
    names = [r.series_name for r in session.execute(select(Series.series_name)).all()]
    name_set = set(names)

    # case-fold collisions
    by_lower: dict[str, list[str]] = defaultdict(list)
    for n in names:
        by_lower[n.lower()].append(n)
    for variants in by_lower.values():
        if len(variants) > 1:
            for v in variants[1:]:
                rep.case_fold.append((variants[0], v))

    # whitespace-only collisions
    by_trimmed: dict[str, list[str]] = defaultdict(list)
    for n in names:
        by_trimmed[n.strip()].append(n)
    for variants in by_trimmed.values():
        if len(variants) > 1:
            for v in variants[1:]:
                rep.whitespace.append((variants[0], v))

    # file ↔ row symmetric diff
    if src.is_dir():
        files = {p.stem for p in src.glob("*.xml") if p.stem and p.stem != ".xml"}
        rep.file_without_row = sorted(files - name_set)
        rep.row_without_file = sorted(name_set - files)
    return rep


@dataclass
class NameMismatchReport:
    """Series whose `series_name` doesn't match the directory or filename
    paths recorded on the row. These are usually candidates for manual
    cleanup — a series renamed in one place but not another."""

    image_loc_mismatches: list[tuple[str, str]] = field(default_factory=list)  # (series, dir_name)
    annot_loc_mismatches: list[tuple[str, str]] = field(default_factory=list)
    xml_source_mismatches: list[tuple[str, str]] = field(default_factory=list)  # (series, xml_stem)
    acetree_config_mismatches: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.image_loc_mismatches)
            + len(self.annot_loc_mismatches)
            + len(self.xml_source_mismatches)
            + len(self.acetree_config_mismatches)
        )

    def summary(self) -> str:
        return (
            f"name vs imageloc: {len(self.image_loc_mismatches)}, "
            f"name vs annots: {len(self.annot_loc_mismatches)}, "
            f"name vs xml_source: {len(self.xml_source_mismatches)}, "
            f"name vs acetree_config: {len(self.acetree_config_mismatches)}"
        )


def find_name_mismatches(session: Session) -> NameMismatchReport:
    """Flag series where the series_name disagrees with one of the recorded
    paths.

    The legacy Java EmbryoDB GUI had a bug where renaming a series in one
    field didn't update the others, leading to imagesloc/annotsloc/acetree
    paths pointing at stale series names. Each such row is a manual
    cleanup candidate.
    """
    rep = NameMismatchReport()
    for row in session.execute(select(Series)).scalars():
        name = row.series_name or ""
        if not name:
            continue
        if row.image_loc:
            dir_name = Path(row.image_loc).name
            if dir_name and dir_name != name:
                rep.image_loc_mismatches.append((name, dir_name))
        if row.annot_loc:
            dir_name = Path(row.annot_loc).name
            if dir_name and dir_name != name:
                rep.annot_loc_mismatches.append((name, dir_name))
        if row.xml_source_path:
            stem = Path(row.xml_source_path).stem
            if stem and stem != name:
                rep.xml_source_mismatches.append((name, stem))
        if row.acetree_config:
            cfg = row.acetree_config
            if cfg.endswith(".xml"):
                cfg = cfg[: -len(".xml")]
            if cfg and cfg != name:
                rep.acetree_config_mismatches.append((name, cfg))
    return rep


@dataclass
class CheckedByAnomalyReport:
    """Series whose `partial_editing_code` doesn't look like a partial
    editing code. The legacy Java EmbryoDB GUI had a bug that occasionally
    let other-field content land in checkedBy; these rows are likely
    candidates for manual cleanup."""

    suspect: list[tuple[str, str]] = field(default_factory=list)  # (series, value)

    @property
    def total(self) -> int:
        return len(self.suspect)

    def summary(self) -> str:
        return f"suspect checkedBy: {len(self.suspect)}"


# Valid partial editing code:
#   empty | "n/a"
#   N
#   N,Cell1:T1,Cell2:T2,...
# Where N is a global timepoint number and CellN starts with a letter
# (standard C. elegans names: AB*, MS*, E*, C*, D*, P*, Z*, …).
_PEC_TOKEN_RE = re.compile(r"^(\d+|[A-Za-z][A-Za-z0-9_-]*:\d+)$")


def find_checkedby_anomalies(session: Session) -> CheckedByAnomalyReport:
    rep = CheckedByAnomalyReport()
    for row in session.execute(select(Series)).scalars():
        code = (row.partial_editing_code or "").strip()
        if not code or code.lower() == "n/a":
            continue
        # Quick negative filters: free-text content rarely uses only
        # commas + colons + cell-name tokens.
        if any(ch in code for ch in (" ", ".", "/", "(", ")", "?")):
            rep.suspect.append((row.series_name, code))
            continue
        tokens = code.split(",")
        if not all(_PEC_TOKEN_RE.match(t) for t in tokens):
            rep.suspect.append((row.series_name, code))
    return rep


@dataclass
class MigrateCheckedByReport:
    migrated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"migrated: {len(self.migrated)}, skipped: {len(self.skipped)}"


_MIGRATION_TAG = "[migrated from legacy checkedBy]"


def migrate_checkedby_anomalies_to_comments(
    session: Session, *, dry_run: bool = True
) -> MigrateCheckedByReport:
    """For each suspect row (per `find_checkedby_anomalies`), move the
    `partial_editing_code` value into `comments` (prepended, tagged) and
    clear `partial_editing_code`. Idempotent — already-migrated rows
    (whose comments start with the tag) are skipped.

    `dry_run=True` (default) reports what would change without writing.
    Pass `dry_run=False` to actually update rows.
    """
    rep = MigrateCheckedByReport()
    suspects = find_checkedby_anomalies(session).suspect
    for series_name, code in suspects:
        row = session.execute(
            select(Series).where(Series.series_name == series_name)
        ).scalar_one_or_none()
        if row is None:
            rep.skipped.append(series_name)
            continue
        if (row.comments or "").lstrip().startswith(_MIGRATION_TAG):
            rep.skipped.append(series_name)
            continue
        if not dry_run:
            existing = (row.comments or "").rstrip()
            prefix = f"{_MIGRATION_TAG} {code}"
            row.comments = prefix if not existing else f"{prefix}\n{existing}"
            row.partial_editing_code = ""
            row.version = (row.version or 1) + 1
        rep.migrated.append(series_name)
    return rep


@dataclass
class PathValidationReport:
    series_count: int = 0
    image_missing: list[tuple[str, str]] = field(default_factory=list)  # (series, path)
    annot_missing: list[tuple[str, str]] = field(default_factory=list)
    config_missing: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"checked: {self.series_count}, "
            f"image missing: {len(self.image_missing)}, "
            f"annot missing: {len(self.annot_missing)}, "
            f"config missing: {len(self.config_missing)}"
        )


def _is_meaningful(path: str) -> bool:
    return bool(path) and path.lower() not in {"n/a", "none", "tbd"}


def validate_paths(
    session: Session,
    dataset_name: str | None = None,
) -> PathValidationReport:
    """Check that image_loc, annot_loc, and acetree_config refer to extant
    paths on disk. Restricting to a single dataset is supported."""
    rep = PathValidationReport()
    if dataset_name:
        ds = session.execute(
            select(Dataset).where(Dataset.name == dataset_name)
        ).scalar_one_or_none()
        if ds is None:
            raise LookupError(f"no dataset named {dataset_name!r}")
        rows: list[Series] = list(ds.series)
    else:
        rows = list(session.execute(select(Series)).scalars())

    for row in rows:
        rep.series_count += 1
        if _is_meaningful(row.image_loc) and not Path(row.image_loc).exists():
            rep.image_missing.append((row.series_name, row.image_loc))
        if _is_meaningful(row.annot_loc) and not Path(row.annot_loc).exists():
            rep.annot_missing.append((row.series_name, row.annot_loc))
        if _is_meaningful(row.acetree_config):
            cfg = row.acetree_config
            # acetree_config is often a filename relative to annot_loc.
            candidate = Path(cfg)
            if not candidate.is_absolute() and _is_meaningful(row.annot_loc):
                candidate = Path(row.annot_loc) / cfg
            if not candidate.exists():
                rep.config_missing.append((row.series_name, str(candidate)))
    return rep
