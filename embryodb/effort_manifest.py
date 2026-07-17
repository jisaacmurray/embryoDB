"""Curation-effort corpus manifest — the embryoDB side of the editing-difficulty
predictor.

The StarryNite "effort" model (``StarryNite/release_v1/effort/`` +
``/murrlab3/jmurr/starrynite_test/build_classic_effort_table.py``) learns to
predict how much hand-curation a StarryNite run needs from GT-free features of
its raw output. Its training label is the diff between a series' raw tracker
output (``dats/<series>.zip``) and its curated ground truth
(``dats/<series>-edit.zip``). To build that label correctly at corpus scale we
must know, per series, **how far the curation is trustworthy** — otherwise the
raw-vs-edit diff manufactures phantom errors past the curated extent (see
``docs/editing_codes.md``).

This module enumerates curated series from the DB and emits a manifest the SN
table-builder consumes in place of its hand-made ``/tmp/*.tsv`` inputs. It owns
exactly the embryoDB-side concerns: DB enumeration, resolution lookup,
editing-code scoping (via :mod:`embryodb.partial`), curation-confidence
bucketing, and an on-disk check that both zips actually exist.

**No mount walking.** Series come from the DB; the only filesystem touch is a
bounded pair of ``stat`` calls per series on the two known zip paths.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from .models import Series, Status
from .partial import PartialEditRule, canonicalize, parse_editing_code
from .queries import datasets as q_datasets
from .queries import series as q_series

# Resolution fallback matches the SN effort scripts (ZSF = z/xy). The SN classic
# table used 0.087 / 0.504 when a series had no microscopy row, so we mirror it
# rather than orchestrate.py's 0.5 z-fallback, to keep ZSF identical end-to-end.
DEFAULT_XY_UM = 0.087
DEFAULT_Z_UM = 0.504

# Datasets whose curation jmurr vouches for as high-confidence fully-curated
# (2026-07-16). Membership clears the aspirational-risk flag.
DEFAULT_TRUSTED_LISTS = ("EPIC_murrlab_additions", "RefCel_expression")

# partial_editing_code values meaning "no per-branch code" (fall back to
# edited_timepts). Mirrors partial.parse_editing_code's sentinel set.
_SENTINELS = {"", "n/a", "none", "na", "-"}

# Curation-confidence buckets. Ordered most→least informative.
BUCKET_PARTIAL = "partial"        # parseable per-branch rules (branch scoping possible)
BUCKET_WHOLE_CODE = "whole_code"  # parseable P0-only / bare-time code → explicit whole depth
BUCKET_TIME_ONLY = "time_only"    # edited_timepts only, no code → likely good, aspirational risk
BUCKET_INITIALS = "initials"      # legacy checkedby holds initials/free text → aspirational risk
BUCKET_UNCURATED = "uncurated"    # no usable curation depth → excluded from training

MANIFEST_COLUMNS = (
    "series", "annot_loc", "xy_um", "z_um", "gene", "strain", "date_acquired",
    "bucket", "global_depth", "n_branch_rules", "partial_rules", "partial_raw",
    "aspirational", "trusted_lists", "raw_zip", "edit_zip", "edited_cells", "usable",
)


@dataclass
class CurationScope:
    """How far a series' curation is ground truth.

    ``global_depth`` bounds the whole-embryo FP/FN-valid region; ``branch_rules``
    are per-branch extents that may go deeper (recall/division only, no FP).
    """

    global_depth: int | None
    branch_rules: list[PartialEditRule]
    bucket: str
    partial_raw: str


@dataclass
class ManifestRow:
    series: str
    annot_loc: str
    xy_um: float
    z_um: float
    gene: str
    strain: str
    date_acquired: str
    bucket: str
    global_depth: int | None
    branch_rules: list[PartialEditRule]
    partial_raw: str
    aspirational: bool
    trusted_lists: list[str]
    raw_zip: bool
    edit_zip: bool
    edited_cells: str

    @property
    def usable(self) -> bool:
        """A training-grade row: has a curation depth and both zips on disk."""
        return (
            self.bucket != BUCKET_UNCURATED
            and self.global_depth is not None
            and self.raw_zip
            and self.edit_zip
        )

    def as_tsv_fields(self) -> list[str]:
        return [
            self.series,
            self.annot_loc,
            f"{self.xy_um:g}",
            f"{self.z_um:g}",
            self.gene or "n/a",
            self.strain or "n/a",
            self.date_acquired or "n/a",
            self.bucket,
            "" if self.global_depth is None else str(self.global_depth),
            str(len(self.branch_rules)),
            canonicalize(self.branch_rules) if self.branch_rules else "",
            self.partial_raw,
            "1" if self.aspirational else "0",
            ",".join(self.trusted_lists),
            "1" if self.raw_zip else "0",
            "1" if self.edit_zip else "0",
            self.edited_cells or "",
            "1" if self.usable else "0",
        ]


def _int_or_none(text: str | None) -> int | None:
    try:
        v = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def resolution_for(series: Series) -> tuple[float, float]:
    """(xy_um, z_um) from microscopy metadata, falling back to lab defaults."""
    md = series.microscopy
    xy = md.voxel_xy_um if md and md.voxel_xy_um else DEFAULT_XY_UM
    z = md.voxel_z_um if md and md.voxel_z_um else DEFAULT_Z_UM
    return float(xy), float(z)


def curation_scope(series: Series) -> CurationScope:
    """Classify a series' curation extent and confidence from its editing fields.

    ``edited_timepts`` is the whole-embryo depth; ``partial_editing_code``
    refines it per branch (or, in the legacy era, held the curator's initials).
    See ``docs/editing_codes.md``.
    """
    raw = (series.partial_editing_code or "").strip()
    edited = _int_or_none(series.edited_timepts)
    rules = parse_editing_code(raw)

    if rules is not None:
        p0_times = [r.time for r in rules if r.cell_name == "P0"]
        branch = [r for r in rules if r.cell_name != "P0"]
        # Whole-embryo depth = deepest P0 rule if present, else edited_timepts.
        global_depth = max(p0_times) if p0_times else edited
        bucket = BUCKET_PARTIAL if branch else BUCKET_WHOLE_CODE
        if global_depth is None:
            bucket = BUCKET_UNCURATED
        return CurationScope(global_depth, branch, bucket, raw)

    # No parseable code.
    if raw and raw.lower() not in _SENTINELS:
        # Non-empty but ungrammatical → legacy "checked by = initials"/free text.
        bucket = BUCKET_INITIALS if edited is not None else BUCKET_UNCURATED
        return CurationScope(edited, [], bucket, raw)

    # Empty/sentinel code → depth from edited_timepts alone.
    bucket = BUCKET_TIME_ONLY if edited is not None else BUCKET_UNCURATED
    return CurationScope(edited, [], bucket, raw)


def _zip_paths(annot_loc: str, series_name: str) -> tuple[Path, Path]:
    dats = Path(annot_loc) / "dats"
    return dats / f"{series_name}.zip", dats / f"{series_name}-edit.zip"


def _is_deleted(series: Series) -> bool:
    return series.deleted_at is not None or series.status in (Status.DELETED, Status.DEL1)


def build_manifest(
    session: Session,
    *,
    dataset_names: Iterable[str] | None = None,
    trusted_lists: Iterable[str] = DEFAULT_TRUSTED_LISTS,
    stat_zips: bool = True,
    date_from: str | None = None,
) -> list[ManifestRow]:
    """Build the corpus manifest.

    ``dataset_names`` restricts to the union of those datasets' members (e.g. the
    control collection); ``None`` enumerates every non-deleted series. Deleted
    series are always excluded. When ``stat_zips`` is True (default), each row's
    ``raw_zip``/``edit_zip`` reflect a real ``stat`` of the two known paths — the
    only filesystem touch, bounded and per-series (never a directory walk).
    ``date_from`` (YYYYMMDD string) filters to series with ``date_acquired >=``
    that value — useful for generating a prediction manifest for recent movies
    regardless of curation status.
    """
    trusted_set = set(trusted_lists)

    if dataset_names is not None:
        seen: dict[str, Series] = {}
        for name in dataset_names:
            ds = q_datasets.get_by_name(session, name)
            if ds is None:
                raise ValueError(f"no dataset named {name!r}")
            for s in ds.series:
                seen.setdefault(s.series_name, s)
        rows_in = list(seen.values())
    else:
        rows_in = q_series.list_series(session, limit=None)

    if date_from is not None:
        rows_in = [s for s in rows_in if (s.date_acquired or "") >= date_from]

    out: list[ManifestRow] = []
    for s in rows_in:
        if _is_deleted(s):
            continue
        scope = curation_scope(s)
        xy, z = resolution_for(s)
        trusted = sorted(d.name for d in s.datasets if d.name in trusted_set)
        aspirational = scope.bucket in (BUCKET_TIME_ONLY, BUCKET_INITIALS) and not trusted

        raw_ok = edit_ok = False
        if stat_zips and s.annot_loc:
            raw_p, edit_p = _zip_paths(s.annot_loc, s.series_name)
            raw_ok = raw_p.is_file()
            edit_ok = edit_p.is_file()

        out.append(
            ManifestRow(
                series=s.series_name,
                annot_loc=s.annot_loc or "",
                xy_um=xy,
                z_um=z,
                gene=s.reporter_gene or "",
                strain=s.strain_name or "",
                date_acquired=s.date_acquired or "",
                bucket=scope.bucket,
                global_depth=scope.global_depth,
                branch_rules=scope.branch_rules,
                partial_raw=scope.partial_raw,
                aspirational=aspirational,
                trusted_lists=trusted,
                raw_zip=raw_ok,
                edit_zip=edit_ok,
                edited_cells=s.edited_cells or "",
            )
        )

    out.sort(key=lambda r: r.series)
    return out


def write_manifest(rows: list[ManifestRow], path: Path) -> Path:
    """Write the manifest as a header'd TSV for the SN table-builder."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(MANIFEST_COLUMNS)]
    lines += ["\t".join(r.as_tsv_fields()) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@dataclass
class ManifestSummary:
    total: int = 0
    usable: int = 0
    by_bucket: dict[str, int] = field(default_factory=dict)
    aspirational: int = 0
    trusted: int = 0
    missing_raw: int = 0
    missing_edit: int = 0
    aspirational_series: list[str] = field(default_factory=list)
    missing_series: list[str] = field(default_factory=list)


def summarize(rows: list[ManifestRow]) -> ManifestSummary:
    """Aggregate the manifest for a pre-training sanity check of the collection."""
    s = ManifestSummary(total=len(rows))
    for r in rows:
        s.by_bucket[r.bucket] = s.by_bucket.get(r.bucket, 0) + 1
        if r.usable:
            s.usable += 1
        if r.aspirational:
            s.aspirational += 1
            s.aspirational_series.append(r.series)
        if r.trusted_lists:
            s.trusted += 1
        if r.bucket != BUCKET_UNCURATED:
            if not r.raw_zip:
                s.missing_raw += 1
            if not r.edit_zip:
                s.missing_edit += 1
            if not (r.raw_zip and r.edit_zip):
                s.missing_series.append(r.series)
    return s


__all__ = [
    "BUCKET_INITIALS",
    "BUCKET_PARTIAL",
    "BUCKET_TIME_ONLY",
    "BUCKET_UNCURATED",
    "BUCKET_WHOLE_CODE",
    "DEFAULT_TRUSTED_LISTS",
    "CurationScope",
    "ManifestRow",
    "ManifestSummary",
    "build_manifest",
    "curation_scope",
    "resolution_for",
    "summarize",
    "write_manifest",
]
