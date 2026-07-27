"""Backfill Acquisition / Series / PipelineStepRun rows from existing on-disk state.

The lab has thousands of acquisitions already staged by the legacy Perl
pipeline. Backfill walks an image_loc parent directory (e.g.
`/murrlab3/<user>/images/`), recognises directories matching one of the
existing Series rows, and records what's already on disk:

- `Acquisition` row inferred from the shared filename stem (everything
  before `_L<N>`), wiring the position series back to it
- Per-step `PipelineStepRun` rows marked COMPLETE where the expected
  outputs exist (e.g. tif/ populated means `stage_images` complete; an
  `<series>-edit.zip` in dats/ means `run_starrynite` complete)

This is **read-only** with respect to image data and metadata — no files
are moved or rewritten. Only DB rows are added/updated.

Idempotent: re-running the backfill on the same directory tree updates
status without creating duplicate rows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Acquisition,
    ImageLayout,
    PipelineStepRun,
    RunStatus,
    Series,
)
from ..parsers.leica_metadata import parse_properties_xml
from .orchestrate import _microscopy_row

log = logging.getLogger(__name__)


# Each step's "is it complete?" predicate, given the staged image_loc.
def _step_status_for(image_loc: Path, step: str) -> RunStatus:
    if step == "stage_images":
        tif = image_loc / "tif"
        return RunStatus.COMPLETE if tif.is_dir() and any(tif.glob("*.tif")) else RunStatus.PENDING
    if step == "stage_metadata":
        dats = image_loc / "dats"
        if dats.is_dir() and any(dats.glob("*Properties.xml")):
            return RunStatus.COMPLETE
        return RunStatus.PENDING
    if step == "write_acetree_config":
        cfg = image_loc / "dats" / f"{image_loc.name}.xml"
        return RunStatus.COMPLETE if cfg.exists() else RunStatus.PENDING
    if step == "write_embryodb_xml":
        # Recorded as complete iff the series has provenance from a prior import.
        # Backfill itself doesn't write the legacy XML; it can be marked from
        # outside via the existing safe-mirror importer.
        return RunStatus.PENDING
    if step == "create_alias_symlink":
        # We can't tell from inside the series dir; leave as PENDING.
        return RunStatus.PENDING
    if step == "write_matlab_params":
        return RunStatus.COMPLETE if (image_loc / "matlabParams").exists() else RunStatus.PENDING
    if step == "run_starrynite":
        # Legacy convention: StarryNite output is <series>-edit.zip in dats/
        edit_zip = image_loc / "dats" / f"{image_loc.name}-edit.zip"
        return RunStatus.COMPLETE if edit_zip.exists() else RunStatus.PENDING
    if step == "compute_difficulty":
        effort = image_loc / "dats" / f"{image_loc.name}_sn_effort.json"
        return RunStatus.COMPLETE if effort.exists() else RunStatus.PENDING
    if step == "run_red_extract":
        # Legacy convention: dats/CD<series>.csv produced by RedExcel2
        cd = image_loc / "dats" / f"CD{image_loc.name}.csv"
        return RunStatus.COMPLETE if cd.exists() else RunStatus.PENDING
    if step == "run_measure":
        # Heuristic: Measure1 emits dats/measure files; presence of <series>AuxInfo.csv
        # is a reasonable surrogate.
        aux = image_loc / "dats" / f"{image_loc.name}AuxInfo.csv"
        return RunStatus.COMPLETE if aux.exists() else RunStatus.PENDING
    return RunStatus.PENDING


# Marker recorded on a synthetic stage_images row for series that predate the
# new staging pipeline (the old XML EmbryoDB, retired ~2026-05). Their images
# are already on disk but were never run through step_stage_images, so they
# carry no stage_images PipelineStepRun at all.
_LEGACY_STAGING_NOTE = (
    "legacy import: series predates the new staging pipeline (~2026-05); "
    "images came from the old XML EmbryoDB, so there is no staging record. "
    "Marked SKIPPED so downstream worker steps are not blocked by a missing "
    "prerequisite."
)


def backfill_legacy_staging(
    session: Session,
    series_names: list[str] | None = None,
) -> list[str]:
    """Record a SKIPPED ``stage_images`` row for legacy series that have none.

    Series imported from the old XML EmbryoDB (before the new staging pipeline)
    have their images on disk but were never run through ``step_stage_images``,
    so they carry no ``stage_images`` PipelineStepRun. A *missing* worker-step
    row blocks :func:`worker._prerequisite_ok`, so an enqueued ``run_starrynite``
    on such a series can never be claimed — and the bare absence is easily
    mistaken for a malformed enqueue. This writes an explicit terminal marker
    (SKIPPED + ``output_summary['legacy'] = True``) so the staging step reads as
    "intentionally not run for a legacy import" rather than "pending/failed",
    and downstream steps become claimable.

    Purely DB-side: it does **not** touch the filesystem (no NFS walk). A series
    is treated as legacy iff it has no ``stage_images`` row; soft-deleted series
    are skipped. Idempotent — a series already carrying any ``stage_images`` row
    is left untouched. Returns the names of the series newly marked.
    """
    q = select(Series).where(Series.deleted_at.is_(None))
    if series_names is not None:
        q = q.where(Series.series_name.in_(series_names))

    marked: list[str] = []
    now = datetime.now(tz=timezone.utc)
    for series in session.execute(q).scalars():
        existing = session.execute(
            select(PipelineStepRun).where(
                PipelineStepRun.series_id == series.id,
                PipelineStepRun.step == "stage_images",
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            PipelineStepRun(
                series_id=series.id,
                step="stage_images",
                status=RunStatus.SKIPPED,
                completed_at=now,
                error_excerpt=_LEGACY_STAGING_NOTE,
                output_summary={"legacy": True, "marked_by": "backfill_legacy_staging"},
            )
        )
        marked.append(series.series_name)

    if marked:
        session.flush()
    return marked


@dataclass
class BackfillReport:
    acquisitions_created: list[str] = field(default_factory=list)
    series_linked: list[str] = field(default_factory=list)
    series_unmatched: list[str] = field(default_factory=list)
    runs_inserted: int = 0
    runs_updated: int = 0
    microscopy_attached: int = 0

    def summary(self) -> str:
        return (
            f"acquisitions: {len(self.acquisitions_created)}, "
            f"series linked: {len(self.series_linked)}, "
            f"unmatched dirs: {len(self.series_unmatched)}, "
            f"runs inserted/updated: {self.runs_inserted}/{self.runs_updated}, "
            f"microscopy attached: {self.microscopy_attached}"
        )


# Series-name suffix used by the import pipeline (e.g. `_L3`).
_POS_SUFFIX = re.compile(r"^(.+)_L(\d+)$")


def _acquisition_stem(series_name: str) -> tuple[str, int] | None:
    m = _POS_SUFFIX.match(series_name)
    if m:
        return m.group(1), int(m.group(2))
    return None


def backfill_directory(
    session: Session,
    root: Path | str,
    *,
    create_unknown_acquisitions: bool = True,
    attach_microscopy: bool = True,
) -> BackfillReport:
    """Walk `root` (e.g. `/murrlab3/<user>/images/`) for staged series dirs.

    Each subdirectory whose name matches an existing Series gets:
      - linked into its inferred Acquisition (creating the Acquisition row
        if one doesn't exist and `create_unknown_acquisitions` is True)
      - per-step PipelineStepRun rows inserted/updated based on what's
        present on disk
      - a MicroscopyMetadata row populated from any `Position N_Properties.xml`
        found in dats/ if `attach_microscopy=True`
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"backfill root not found: {root}")

    rep = BackfillReport()

    # Group series-dirs by inferred acquisition stem so we only create one
    # Acquisition row per (stem, position-set).
    by_acq: dict[str, list[Path]] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        parsed = _acquisition_stem(child.name)
        if parsed is None:
            continue
        stem, _pos = parsed
        by_acq.setdefault(stem, []).append(child)

    for acq_name, child_dirs in by_acq.items():
        acq = session.execute(
            select(Acquisition).where(Acquisition.name == acq_name)
        ).scalar_one_or_none()
        if acq is None:
            if not create_unknown_acquisitions:
                rep.series_unmatched.extend(p.name for p in child_dirs)
                continue
            # Take source_dir as the parent of the first child for lack of
            # better info — backfill doesn't necessarily know the raw location.
            acq = Acquisition(
                name=acq_name,
                source_dir=str(root),
                parser_name="leica_tilescan",
                raw_timepts=240,
                staged_at=datetime.now(tz=timezone.utc),
                staged_by="backfill",
            )
            session.add(acq)
            session.flush()
            rep.acquisitions_created.append(acq_name)

        for series_dir in child_dirs:
            series = session.execute(
                select(Series).where(Series.series_name == series_dir.name)
            ).scalar_one_or_none()
            if series is None:
                rep.series_unmatched.append(series_dir.name)
                continue
            parsed = _acquisition_stem(series_dir.name)
            if parsed is not None:
                series.position = parsed[1]
            series.acquisition = acq
            if not series.image_loc:
                series.image_loc = str(series_dir)
            if not series.annot_loc:
                series.annot_loc = str(series_dir)
            if not series.image_layout:
                series.image_layout = ImageLayout.SPLIT_TIF_PER_PLANE
            rep.series_linked.append(series_dir.name)

            # Per-step run rows
            from .orchestrate import STEPS
            for step in STEPS:
                status = _step_status_for(series_dir, step)
                run = session.execute(
                    select(PipelineStepRun).where(
                        PipelineStepRun.series_id == series.id,
                        PipelineStepRun.step == step,
                    )
                ).scalar_one_or_none()
                if run is None:
                    run = PipelineStepRun(
                        series_id=series.id, step=step, status=status,
                        completed_at=datetime.now(tz=timezone.utc) if status == RunStatus.COMPLETE else None,
                        output_summary={"discovered_by": "backfill"},
                    )
                    session.add(run)
                    rep.runs_inserted += 1
                else:
                    if run.status != status:
                        run.status = status
                        run.completed_at = (
                            datetime.now(tz=timezone.utc) if status == RunStatus.COMPLETE else None
                        )
                        rep.runs_updated += 1

            # Microscopy
            if attach_microscopy and series.microscopy is None:
                props = next(
                    (p for p in (series_dir / "dats").glob("*Properties.xml")),
                    None,
                )
                if props is not None:
                    try:
                        md = parse_properties_xml(props)
                        series.microscopy = _microscopy_row(md, raw_path=str(props))
                        rep.microscopy_attached += 1
                    except Exception as exc:
                        log.warning("metadata parse failed for %s: %s", props, exc)

    session.flush()
    return rep
