"""End-to-end orchestration of a pipeline import.

For one acquisition source dir + chosen Protocol, produce:
  - one Acquisition row
  - one Series row per discovered position (with embryoDB legacy XML written
    to source-dir so the unmodified Java GUI sees it immediately)
  - per-series image_loc populated with staged TIFs (channel-routed)
  - per-series annot_loc/dats/<series>.xml AceTree config
  - per-series MicroscopyMetadata populated from Leica Properties.xml
  - per-series matlabParams file with resolution from the metadata
  - /murrlab/<user>/images/<series> symlink to /murrlab3/<user>/images/<series>
  - one PipelineStepRun row per (series, step), all reaching COMPLETE

Steps 7-9 (run_starrynite, run_red_extract, run_measure) are stubs in v2 —
they create PENDING rows and a follow-up commit wires in the subprocesses.

All file writes route through `embryodb.fsutil` for the project permission
discipline.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..exporters.xml_exporter import _row_to_record  # type: ignore[reportPrivateImportUsage]
from ..fsutil import ensure_dir, safe_symlink, safe_write_text
from ..models import (
    Acquisition,
    ImageLayout,
    MicroscopyMetadata,
    PipelineStepRun,
    Protocol,
    RunStatus,
    Series,
    Status,
)
from ..parsers.leica_metadata import LeicaMetadata, parse_properties_xml
from ..parsers.matlab_params import MatlabParams, load as load_params, render as render_params
from ..parsers.xml import serialize as serialize_legacy_xml
from .stage import PositionPlan, StageOutcome, StagePlan, plan_acquisition, stage_position

log = logging.getLogger(__name__)

# Lab default image-loc root. Override per-call if you want a different
# layout (e.g. for the test fixture).
DEFAULT_IMAGE_LOC_ROOT = Path("/murrlab3")
# Alias root the lab uses (`/murrlab/<user>/images/...` -> `/murrlab3/...`)
DEFAULT_ALIAS_ROOT = Path("/murrlab")

# Lab default fallback resolution when Leica metadata is missing.
FALLBACK_VOXEL_XY_UM = 0.087
FALLBACK_VOXEL_Z_UM = 0.5


# Step names. Strings (not enum) so that adding a new step later doesn't
# require a schema migration on the PipelineStepRun.step column.
STEPS = (
    "stage_images",
    "stage_metadata",
    "write_acetree_config",
    "write_embryodb_xml",
    "create_alias_symlink",
    "write_matlab_params",
    "run_starrynite",
    "run_red_extract",
    "run_measure",
)


@dataclass
class ImportOptions:
    """Knobs the caller (wizard / CLI) supplies."""

    image_loc_root: Path = DEFAULT_IMAGE_LOC_ROOT
    user: str | None = None  # owns the per-user images dir; defaults to settings.user
    alias_root: Path | None = DEFAULT_ALIAS_ROOT  # None disables alias creation
    parameter_overrides: dict[str, str] = field(default_factory=dict)
    compress_with_lzw: bool = True
    overwrite_existing_images: bool = False
    # Steps to run end-to-end. v2 runs through write_matlab_params by default
    # and leaves the StarryNite + acebatch3 steps as PENDING rows for the
    # worker to pick up later.
    run_through_step: str = "write_matlab_params"


@dataclass
class ImportResult:
    acquisition: Acquisition
    series_outcomes: list["SeriesOutcome"]

    def summary(self) -> str:
        ok = sum(1 for s in self.series_outcomes if s.failed_step is None)
        return (
            f"acquisition {self.acquisition.name!r}: "
            f"{len(self.series_outcomes)} positions, "
            f"{ok} succeeded through {self.acquisition.name}"
        )


@dataclass
class SeriesOutcome:
    series_name: str
    series_id: int | None = None
    image_loc: Path | None = None
    stage_outcome: StageOutcome | None = None
    steps: dict[str, RunStatus] = field(default_factory=dict)
    failed_step: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------


def _get_or_create_run(session: Session, series_id: int, step: str) -> PipelineStepRun:
    run = (
        session.query(PipelineStepRun)
        .filter_by(series_id=series_id, step=step)
        .one_or_none()
    )
    if run is None:
        run = PipelineStepRun(series_id=series_id, step=step, status=RunStatus.PENDING)
        session.add(run)
        session.flush()
    return run


def _begin(run: PipelineStepRun) -> None:
    run.status = RunStatus.RUNNING
    run.started_at = datetime.now(tz=timezone.utc)
    run.heartbeat_at = run.started_at
    run.error_excerpt = None


def _complete(run: PipelineStepRun, summary: dict[str, Any] | None = None) -> None:
    run.status = RunStatus.COMPLETE
    run.completed_at = datetime.now(tz=timezone.utc)
    if summary:
        run.output_summary = summary


def _fail(run: PipelineStepRun, err: BaseException) -> None:
    run.status = RunStatus.FAILED
    run.completed_at = datetime.now(tz=timezone.utc)
    run.error_excerpt = (repr(err))[:1024]


def _user_for(opts: ImportOptions) -> str:
    return opts.user or settings.user or os.environ.get("USER") or "anonymous"


def _series_image_loc(opts: ImportOptions, series_name: str) -> Path:
    """`/murrlab3/<user>/images/<series>/`."""
    return opts.image_loc_root / _user_for(opts) / "images" / series_name


def _series_alias(opts: ImportOptions, series_name: str) -> Path | None:
    if opts.alias_root is None:
        return None
    return opts.alias_root / _user_for(opts) / "images" / series_name


# ---------------------------------------------------------------------------
# Per-step implementations
# ---------------------------------------------------------------------------


def step_stage_metadata(
    series: Series, source_dir: Path, position: int
) -> tuple[LeicaMetadata | None, list[Path]]:
    """Copy the per-position Leica metadata XMLs into the series' dats/ dir
    and parse Properties.xml into a LeicaMetadata. Returns (metadata, copied)."""
    dats = ensure_dir(Path(series.annot_loc) / "dats")
    # The fixture has filenames like "<stem>_Position N_Properties.xml" and
    # "<stem>_Position N.xml" and "<stem>_Position N_Properties_CIP_HWS3.xsl"
    # All matching position N belong to this series.
    pattern_stems = [f"_Position {position}_", f"_Position {position}."]
    copied: list[Path] = []
    md: LeicaMetadata | None = None
    for entry in sorted(source_dir.iterdir()):
        if not entry.is_file():
            continue
        if not any(s in entry.name for s in pattern_stems):
            continue
        if entry.suffix.lower() not in (".xml", ".xsl"):
            continue
        from ..fsutil import safe_copy
        dst = safe_copy(entry, dats / entry.name)
        copied.append(dst)
        if entry.name.endswith(f"_Position {position}_Properties.xml"):
            try:
                md = parse_properties_xml(entry)
                _apply_microscopy_row(series, md, raw_path=str(entry))
            except Exception as exc:
                log.warning("failed to parse %s: %s", entry, exc)
    return md, copied


def _apply_microscopy_row(
    series: Series, md: LeicaMetadata, raw_path: str | None = None
) -> None:
    """Create or update the MicroscopyMetadata row attached to `series`.

    Idempotent: on re-import, updates the existing row in place rather than
    inserting a duplicate (which would violate the unique series_id constraint).
    """
    if series.microscopy is None:
        series.microscopy = _microscopy_row(md, raw_path=raw_path)
        return
    existing = series.microscopy
    new = _microscopy_row(md, raw_path=raw_path)
    # Copy over every mutable column from `new` to `existing`, but skip None
    # values. SQLAlchemy column defaults (default="") only apply at INSERT
    # time, so the in-memory `new` instance has None for unspecified string
    # columns — pushing those over the existing values would violate NOT NULL
    # constraints. Partial re-parses also shouldn't wipe previously-good data.
    for col in (
        "vendor", "instrument", "objective", "objective_NA", "magnification",
        "immersion", "refractive_index", "voxel_xy_um", "voxel_z_um",
        "planes_per_volume", "channels_per_plane", "x_pixels", "y_pixels",
        "n_timepoints", "cycle_time_s", "pinhole_um", "pinhole_airy",
        "scan_speed_hz", "line_averaging", "frame_averaging", "channels",
        "stage_x_um", "stage_y_um", "stage_z_um", "acquired_at",
        "raw_metadata_path",
    ):
        val = getattr(new, col)
        if val is not None:
            setattr(existing, col, val)


def _microscopy_row(md: LeicaMetadata, raw_path: str | None = None) -> MicroscopyMetadata:
    return MicroscopyMetadata(
        vendor="leica_stellaris",
        objective=md.objective,
        objective_NA=md.objective_NA,
        magnification=md.magnification,
        immersion=md.immersion,
        refractive_index=md.refractive_index,
        voxel_xy_um=md.voxel_xy_um,
        voxel_z_um=md.voxel_z_um,
        planes_per_volume=md.planes_per_volume,
        channels_per_plane=len(md.channels) or None,
        x_pixels=md.x_pixels,
        y_pixels=md.y_pixels,
        n_timepoints=md.n_timepoints,
        cycle_time_s=md.cycle_time_s,
        pinhole_um=md.pinhole_um,
        pinhole_airy=md.pinhole_airy,
        scan_speed_hz=md.scan_speed_hz,
        line_averaging=md.line_averaging,
        frame_averaging=md.frame_averaging,
        channels=md.channels,
        stage_x_um=md.stage_x_um,
        stage_y_um=md.stage_y_um,
        stage_z_um=md.stage_z_um,
        raw_metadata_path=raw_path,
    )


def step_write_acetree_config(
    series: Series, image_loc: Path, n_timepoints: int, planes_per_volume: int,
    voxel_xy_um: float | None, voxel_z_um: float | None
) -> Path:
    """Write `<image_loc>/dats/<series>.xml` — the AceTree per-series config.

    Format inferred from real fixtures (XMLConfig.java + sample inspection):
      <embryo>
        <image file="<image_loc>/tif/<series>-t001-p01.tif"/>
        <nuclei file="<image_loc>/dats/<series>-edit.zip"/>
        <start index="1"/>
        <end index="<N>"/>
        <axis axis=""/>
        <resolution xyRes=".087" zRes=".504" planeEnd="66"/>
      </embryo>
    """
    dats = ensure_dir(image_loc / "dats")
    cfg_path = dats / f"{series.series_name}.xml"
    image_file = image_loc / "tif" / f"{series.series_name}-t001-p01.tif"
    nuclei_file = dats / f"{series.series_name}-edit.zip"
    xyres = voxel_xy_um if voxel_xy_um is not None else FALLBACK_VOXEL_XY_UM
    zres = voxel_z_um if voxel_z_um is not None else FALLBACK_VOXEL_Z_UM
    body = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<embryo>\n"
        f'<image file="{image_file}"/>\n'
        f'<nuclei file="{nuclei_file}"/>\n'
        '<start index="1"/>\n'
        f'<end index="{n_timepoints}"/>\n'
        '<axis axis=""/>\n'
        f'<resolution xyRes="{xyres}" zRes="{zres}" planeEnd="{planes_per_volume - 1}"/>\n'
        "</embryo>\n"
    )
    safe_write_text(cfg_path, body)
    series.acetree_config = cfg_path.name
    return cfg_path


def step_write_embryodb_xml(series: Series, source_xml_dir: Path) -> Path:
    """Write the legacy embryoDB XML for a series.

    New acquisitions: writes to source-dir so the legacy Java GUI sees them
    immediately. Never overwrites an existing file — if one is already there
    (from a previous import or the legacy Java GUI), reads its content and
    populates provenance so audit-import stays happy, then returns without
    modifying the file.
    """
    target = source_xml_dir / f"{series.series_name}.xml"
    if target.exists():
        # Already written (re-import or legacy file). Populate provenance from
        # the existing file so audit-import round-trips correctly.
        if not series.xml_source_path:
            body = target.read_text(encoding="utf-8", errors="replace")
            series.xml_source_path = str(target)
            series.xml_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            series.xml_mtime = datetime.now(tz=timezone.utc)
            series.raw_xml = body
        return target
    body = serialize_legacy_xml(_row_to_record(series))
    safe_write_text(target, body)
    series.xml_source_path = str(target)
    series.xml_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    series.xml_mtime = datetime.now(tz=timezone.utc)
    series.raw_xml = body
    return target


def step_create_alias_symlink(image_loc: Path, alias: Path | None) -> Path | None:
    """`/murrlab/<user>/images/<series>` -> `/murrlab3/<user>/images/<series>`."""
    if alias is None:
        return None
    if alias.exists() and alias.is_symlink():
        return alias
    if alias.exists():
        # Real directory at alias location — surprising. Don't try to replace it.
        log.warning("alias path %s already exists as a non-symlink; skipping", alias)
        return alias
    return safe_symlink(image_loc, alias)


def step_write_matlab_params(
    image_loc: Path,
    protocol: Protocol,
    overrides: dict[str, str],
    voxel_xy_um: float | None,
    voxel_z_um: float | None,
    n_timepoints: int,
    slices: int,
) -> Path:
    """Read the protocol's parameter file, apply per-acquisition overrides and
    metadata-derived defaults, write to `<image_loc>/matlabParams`.

    Resolution + plane count + time range are sourced from the metadata
    (single source of truth) and copied here so StarryNite gets the right
    values without the user having to edit anything.
    """
    template_path = Path(protocol.parameters_file_path)
    params = load_params(template_path) if template_path.exists() else MatlabParams()

    # Push metadata-derived values
    if voxel_xy_um is not None:
        params.set("xyres", str(voxel_xy_um))
    if voxel_z_um is not None:
        params.set("zres", str(voxel_z_um))
    params.set("slices", str(slices))
    params.set("end_time", str(n_timepoints))

    # Apply wizard-supplied overrides last (highest priority).
    for k, v in overrides.items():
        params.set(k, v)

    target = image_loc / "matlabParams"
    safe_write_text(target, render_params(params))
    return target


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def import_acquisition(
    session: Session,
    source_dir: Path | str,
    protocol: Protocol,
    *,
    acquisition_name: str | None = None,
    parser_name: str = "leica_tilescan",
    options: ImportOptions | None = None,
    person: str = "",
    strain_name: str = "",
    treatments: str = "",
    reporter_gene: str = "",
    comments: str = "",
    legacy_xml_dir: Path | None = None,
) -> ImportResult:
    """Run the v2 import pipeline for one acquisition end-to-end.

    Steps up to `options.run_through_step` execute synchronously and update
    PipelineStepRun rows. Remaining steps land as PENDING for the future
    worker to pick up.
    """
    opts = options or ImportOptions()
    source = Path(source_dir)
    plan = plan_acquisition(source, protocol, parser_name=parser_name)
    if not plan.positions:
        raise ValueError(f"no positions discovered in {source}")
    acq_name = acquisition_name or plan.acquisition_stem

    # Find the run-through step's index so we know when to stop.
    try:
        stop_after = STEPS.index(opts.run_through_step)
    except ValueError:
        raise ValueError(f"unknown step {opts.run_through_step!r}")

    legacy_dir = Path(legacy_xml_dir or settings.source_dir)
    n_timepoints_from_plan = max((p.n_timepoints for p in plan.positions.values()), default=0)
    raw_timepts = n_timepoints_from_plan or protocol.default_timepts

    # Acquisition row — find or create so re-importing the same source dir
    # is idempotent (wizard submit after a previous run won't crash).
    acq = session.query(Acquisition).filter_by(name=acq_name).one_or_none()
    if acq is None:
        acq = Acquisition(
            name=acq_name,
            source_dir=str(source),
            parser_name=parser_name,
            protocol=protocol,
            raw_timepts=raw_timepts,
            staged_at=datetime.now(tz=timezone.utc),
            staged_by=_user_for(opts),
            parameter_overrides=dict(opts.parameter_overrides),
            person=person,
            strain_name=strain_name,
            treatments=treatments,
            reporter_gene=reporter_gene,
            comments=comments,
        )
        session.add(acq)
    else:
        # Update mutable metadata from this call's arguments (non-empty wins).
        if person:
            acq.person = person
        if strain_name:
            acq.strain_name = strain_name
        if treatments:
            acq.treatments = treatments
        if reporter_gene:
            acq.reporter_gene = reporter_gene
        if comments:
            acq.comments = comments
        if opts.parameter_overrides:
            acq.parameter_overrides = dict(opts.parameter_overrides)
    session.flush()

    outcomes: list[SeriesOutcome] = []

    for position in plan.position_numbers:
        pos_plan = plan.positions[position]
        outcome = SeriesOutcome(series_name=pos_plan.series_name)

        # Series row — find or create for the same idempotency reason.
        image_loc = _series_image_loc(opts, pos_plan.series_name)
        existing_series = (
            session.query(Series)
            .filter_by(series_name=pos_plan.series_name)
            .one_or_none()
        )
        if existing_series is not None:
            series = existing_series
            # Re-link to this acquisition if not already linked.
            if series.acquisition_id is None:
                series.acquisition = acq
                series.position = position
        else:
            stem = pos_plan.series_name
            date_from_stem = stem[:8] if stem[:8].isdigit() and len(stem) >= 8 else ""
            date_acquired = date_from_stem or datetime.now(tz=timezone.utc).strftime("%Y%m%d")
            series = Series(
                series_name=pos_plan.series_name,
                date_acquired=date_acquired,
                person=person or acq.person,
                strain_name=strain_name or acq.strain_name,
                treatments=treatments or acq.treatments,
                reporter_gene=reporter_gene or acq.reporter_gene,
                image_loc=str(image_loc),
                annot_loc=str(image_loc),
                acetree_config=f"{pos_plan.series_name}.xml",
                timepts=str(pos_plan.n_timepoints or raw_timepts),
                status=Status.NEW,
                acquisition=acq,
                position=position,
                image_layout=ImageLayout.SPLIT_TIF_PER_PLANE,
                updated_by=_user_for(opts),
            )
            session.add(series)
        session.flush()
        outcome.series_id = series.id

        # Pre-create PENDING rows for every step so the GUI can show them.
        for step in STEPS:
            _get_or_create_run(session, series.id, step)

        # --- step 1: stage_images
        outcome.image_loc = image_loc
        if stop_after >= 0:
            run = _get_or_create_run(session, series.id, "stage_images")
            _begin(run)
            try:
                slices = protocol.defaults.get("slices")
                slices_int = int(slices) if slices and slices.isdigit() else (
                    pos_plan.planes_per_volume or 67
                )
                stage_outcome = stage_position(
                    pos_plan,
                    plan.channel_map,
                    image_loc_root=opts.image_loc_root / _user_for(opts) / "images",
                    slices=slices_int,
                    compress_with_lzw=opts.compress_with_lzw,
                    overwrite=opts.overwrite_existing_images,
                )
                outcome.stage_outcome = stage_outcome
                _complete(
                    run,
                    {
                        "written": stage_outcome.written,
                        "skipped": stage_outcome.skipped,
                        "errors": len(stage_outcome.errors),
                    },
                )
                outcome.steps["stage_images"] = RunStatus.COMPLETE
            except Exception as exc:
                _fail(run, exc)
                outcome.steps["stage_images"] = RunStatus.FAILED
                outcome.failed_step = "stage_images"
                outcome.error = repr(exc)
                outcomes.append(outcome)
                continue

        # --- step 2: stage_metadata
        if stop_after >= 1:
            run = _get_or_create_run(session, series.id, "stage_metadata")
            _begin(run)
            try:
                md, copied = step_stage_metadata(series, source, position)
                _complete(run, {"copied": len(copied)})
                outcome.steps["stage_metadata"] = RunStatus.COMPLETE
            except Exception as exc:
                _fail(run, exc)
                outcome.steps["stage_metadata"] = RunStatus.FAILED
                outcome.failed_step = "stage_metadata"
                outcome.error = repr(exc)
                outcomes.append(outcome)
                continue

        # Resolve the resolution / plane count, preferring metadata.
        voxel_xy = getattr(series.microscopy, "voxel_xy_um", None) if series.microscopy else None
        voxel_z = getattr(series.microscopy, "voxel_z_um", None) if series.microscopy else None
        planes = (
            getattr(series.microscopy, "planes_per_volume", None) if series.microscopy else None
        ) or pos_plan.planes_per_volume or 67
        # Actual staged file count takes priority over the metadata-reported
        # count (which reflects what the microscope was *configured* to do, not
        # what was actually acquired). For an aborted or test acquisition the
        # two differ and using the wrong value causes StarryNite to request
        # non-existent files.
        n_tp = (
            pos_plan.n_timepoints
            or (getattr(series.microscopy, "n_timepoints", None) if series.microscopy else None)
            or raw_timepts
        )

        # --- step 3: write_acetree_config
        if stop_after >= 2:
            run = _get_or_create_run(session, series.id, "write_acetree_config")
            _begin(run)
            try:
                cfg = step_write_acetree_config(
                    series, image_loc, n_tp, planes, voxel_xy, voxel_z
                )
                _complete(run, {"path": str(cfg)})
                outcome.steps["write_acetree_config"] = RunStatus.COMPLETE
            except Exception as exc:
                _fail(run, exc)
                outcome.steps["write_acetree_config"] = RunStatus.FAILED
                outcome.failed_step = "write_acetree_config"
                outcome.error = repr(exc)
                outcomes.append(outcome)
                continue

        # --- step 4: write_embryodb_xml
        if stop_after >= 3:
            run = _get_or_create_run(session, series.id, "write_embryodb_xml")
            _begin(run)
            try:
                target = step_write_embryodb_xml(series, legacy_dir)
                _complete(run, {"path": str(target)})
                outcome.steps["write_embryodb_xml"] = RunStatus.COMPLETE
            except Exception as exc:
                _fail(run, exc)
                outcome.steps["write_embryodb_xml"] = RunStatus.FAILED
                outcome.failed_step = "write_embryodb_xml"
                outcome.error = repr(exc)
                outcomes.append(outcome)
                continue

        # --- step 5: create_alias_symlink
        if stop_after >= 4:
            run = _get_or_create_run(session, series.id, "create_alias_symlink")
            _begin(run)
            try:
                alias = _series_alias(opts, pos_plan.series_name)
                link = step_create_alias_symlink(image_loc, alias)
                _complete(run, {"link": str(link) if link else None})
                outcome.steps["create_alias_symlink"] = RunStatus.COMPLETE
            except Exception as exc:
                _fail(run, exc)
                outcome.steps["create_alias_symlink"] = RunStatus.FAILED
                outcome.failed_step = "create_alias_symlink"
                outcome.error = repr(exc)
                outcomes.append(outcome)
                continue

        # --- step 6: write_matlab_params
        if stop_after >= 5:
            run = _get_or_create_run(session, series.id, "write_matlab_params")
            _begin(run)
            try:
                target = step_write_matlab_params(
                    image_loc,
                    protocol,
                    opts.parameter_overrides,
                    voxel_xy,
                    voxel_z,
                    n_tp,
                    planes,
                )
                _complete(run, {"path": str(target)})
                outcome.steps["write_matlab_params"] = RunStatus.COMPLETE
            except Exception as exc:
                _fail(run, exc)
                outcome.steps["write_matlab_params"] = RunStatus.FAILED
                outcome.failed_step = "write_matlab_params"
                outcome.error = repr(exc)
                outcomes.append(outcome)
                continue

        outcomes.append(outcome)

    session.flush()
    return ImportResult(acquisition=acq, series_outcomes=outcomes)


__all__ = [
    "ImportOptions",
    "ImportResult",
    "SeriesOutcome",
    "STEPS",
    "import_acquisition",
]
