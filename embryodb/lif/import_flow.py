"""LIF→pipeline import orchestration.

Mirrors :func:`embryodb.pipeline.orchestrate.import_acquisition` for the
case where the source is a Leica `.lif` file rather than a directory of
exported TIFs.

The key difference: instead of walking a source directory and copying
files via :func:`stage_position`, we extract per-plane TIFs from the LIF
**directly to the staged image_loc layout** (with Z-inversion + channel
routing baked in). That's the I/O win recommended in the master plan
(half the disk reads/writes vs. the two-step "extract-to-source-dir →
pipeline" path).

The remaining inline pipeline steps run via the existing helpers in
``embryodb.pipeline.orchestrate``: per-series metadata parse, timestamps,
AceTree config, legacy XML, alias symlink, matlabParams. Worker steps
(StarryNite, red-extract, measure) land as PENDING rows for the worker.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from ..config import settings
from ..fsutil import ensure_dir, safe_write_text
from ..models import (
    Acquisition,
    ImageLayout,
    PipelineStepRun,
    Protocol,
    RunStatus,
    Series,
    Status,
)
from ..pipeline.orchestrate import (
    ImportOptions,
    ImportResult,
    SeriesOutcome,
    STEPS,
    _apply_microscopy_row,
    _get_or_create_run,
    _series_alias,
    step_compute_timestamps,
    step_create_alias_symlink,
    step_write_acetree_config,
    step_write_embryodb_xml,
    step_write_matlab_params,
)
from ..pipeline.stage import StageOutcome, role_subdir
from .extractor import LifExtractor

log = logging.getLogger(__name__)


def _user_for(opts: ImportOptions) -> str:
    return opts.user or settings.user or "anonymous"


_POSITION_NUM_RE = re.compile(r"Position\s+(\d+)", re.IGNORECASE)


def _position_number(name: str, fallback: int) -> int:
    """Parse the 1-indexed Leica position number out of a LIF position name.

    readlif yields positions in acquisition order, NOT numeric order (e.g.
    ``Position 1, 2, 4, 3, …``). The ``_L<N>`` series suffix and the
    ``Position N_Properties.xml`` sidecar must use the real Leica position
    number — the same one the source-dir filename parser extracts — so a
    LIF import names series identically to a LAS X TIF export of the same
    acquisition. Falls back to enumeration order only when the name has no
    parseable ``Position N``.
    """
    m = _POSITION_NUM_RE.search(name or "")
    return int(m.group(1)) if m else fallback


def import_lif(
    session: Session,
    lif_path: Path | str,
    series_name: str,
    protocol: Protocol,
    *,
    acquisition_name: str | None = None,
    options: ImportOptions | None = None,
    person: str = "",
    strain_name: str = "",
    treatments: str = "",
    reporter_gene: str = "",
    comments: str = "",
    legacy_xml_dir: Path | None = None,
    positions: list[str] | None = None,
    channel_role_override: dict[int, str] | None = None,
    bit_depth_policy: str = "downcast",
    compress: bool = True,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> ImportResult:
    """Import one acquisition end-to-end from a LIF file.

    Args:
      session: SQLAlchemy session in an open transaction.
      lif_path: path to the `.lif` file.
      series_name: which top-level series inside the LIF to import
        (e.g. ``"TileScan 2"``).
      protocol: the lab Protocol whose ``channel_map`` routes raw
        channels to roles (histone / reporter / …).
      positions: optional whitelist of position names to import
        (default: all positions in the series).
      channel_role_override: optional ``{raw_index: role}`` mapping that
        overrides ``protocol.channel_map`` for this import only. Useful
        when the dye-to-role assignment differs from the protocol's
        default (e.g. swapped channels for a specific acquisition).
      bit_depth_policy: ``pass`` | ``downcast`` | ``fail``.
      compress: LZW-compress on TIF write (default ``True``).
      on_progress: optional callback ``(series_name, done, total)``
        called periodically during extraction.

    Returns:
      :class:`ImportResult` with one :class:`SeriesOutcome` per position.
    """
    opts = options or ImportOptions()
    image_loc_root = opts.image_loc_root or Path(settings.image_loc_root)
    user = _user_for(opts)
    legacy_dir = Path(legacy_xml_dir or settings.source_dir)
    stop_after = STEPS.index(opts.run_through_step)

    ex = LifExtractor(lif_path)
    series_info = next(
        (s for s in ex.list_series() if s.name == series_name), None
    )
    if series_info is None:
        raise ValueError(
            f"series {series_name!r} not found in {lif_path}"
        )
    if not series_info.positions:
        raise ValueError(
            f"series {series_name!r} has no positions"
        )

    pos_list = series_info.positions
    if positions is not None:
        pos_list = [p for p in pos_list if p.name in positions]
    if not pos_list:
        raise ValueError("no positions selected for import")

    # Resolve channel mapping. Protocol's channel_map is keyed by raw
    # channel index as a string (matches the format on Protocol rows);
    # an override dict provided by the caller wins when present.
    channel_map: dict[int, str] = {}
    for raw_idx_str, role in (protocol.channel_map or {}).items():
        try:
            channel_map[int(raw_idx_str)] = role
        except ValueError:
            continue
    if channel_role_override:
        channel_map.update(channel_role_override)

    # Acquisition stem: caller-supplied or derived from the LIF filename.
    stem = acquisition_name or Path(lif_path).stem
    # We store stem on Acquisition.name so re-runs are idempotent on the
    # natural key; source_dir gets set to a virtual path that identifies
    # this LIF+series pair (not used for staging — extractor goes direct).
    acq_source = f"{lif_path}::{series_name}"

    # ---- Acquisition row (find-or-create) ----
    acq = (
        session.query(Acquisition)
        .filter_by(name=stem)
        .one_or_none()
    )
    if acq is None:
        acq = Acquisition(
            name=stem,
            source_dir=acq_source,
            parser_name="lif",
            protocol=protocol,
            raw_timepts=pos_list[0].n_timepoints or protocol.default_timepts,
            staged_at=datetime.now(tz=timezone.utc),
            staged_by=user,
            parameter_overrides=dict(opts.parameter_overrides),
            person=person,
            strain_name=strain_name,
            treatments=treatments,
            reporter_gene=reporter_gene,
            comments=comments,
        )
        session.add(acq)
    else:
        # Non-empty wins; same UX as import_acquisition.
        for col, val in (
            ("person", person), ("strain_name", strain_name),
            ("treatments", treatments), ("reporter_gene", reporter_gene),
            ("comments", comments),
        ):
            if val:
                setattr(acq, col, val)
        if opts.parameter_overrides:
            acq.parameter_overrides = dict(opts.parameter_overrides)
    session.flush()

    outcomes: list[SeriesOutcome] = []
    planes_per_volume = pos_list[0].n_planes

    for pos_idx, pos in enumerate(pos_list, start=1):
        position_num = _position_number(pos.name, pos_idx)
        outcome = _import_one_position(
            session,
            ex=ex,
            acq=acq,
            protocol=protocol,
            channel_map=channel_map,
            series_name_in_lif=series_name,
            position_info=pos,
            position_num=position_num,
            stem=stem,
            user=user,
            image_loc_root=image_loc_root,
            alias_root=opts.alias_root,
            legacy_dir=legacy_dir,
            bit_depth_policy=bit_depth_policy,
            compress=compress,
            overwrite=opts.overwrite_existing_images,
            stop_after=stop_after,
            opts=opts,
            on_progress=on_progress,
        )
        outcomes.append(outcome)

    session.flush()
    return ImportResult(acquisition=acq, series_outcomes=outcomes)


def _import_one_position(
    session: Session,
    *,
    ex: LifExtractor,
    acq: Acquisition,
    protocol: Protocol,
    channel_map: dict[int, str],
    series_name_in_lif: str,
    position_info,
    position_num: int,
    stem: str,
    user: str,
    image_loc_root: Path,
    alias_root: Path | None,
    legacy_dir: Path,
    bit_depth_policy: str,
    compress: bool,
    overwrite: bool,
    stop_after: int,
    opts: ImportOptions,
    on_progress: Callable | None,
) -> SeriesOutcome:
    """Run extraction + the inline pipeline steps for one position."""
    series_name = f"{stem}_L{position_num}"
    image_loc = ensure_dir(image_loc_root / user / "images" / series_name)
    dats_dir = ensure_dir(image_loc / "dats")
    outcome = SeriesOutcome(series_name=series_name, image_loc=image_loc)

    # ---- Series row (find-or-create) ----
    series = (
        session.query(Series).filter_by(series_name=series_name).one_or_none()
    )
    if series is None:
        series = Series(
            series_name=series_name,
            status=Status.NEW,
            image_loc=str(image_loc),
            annot_loc=str(image_loc),
            acquisition=acq,
            person=acq.person,
            strain_name=acq.strain_name,
            treatments=acq.treatments,
            reporter_gene=acq.reporter_gene,
            comments=acq.comments,
            date_acquired=stem[:8] if stem[:8].isdigit() else "",
            timepts=str(position_info.n_timepoints),
        )
        session.add(series)
    else:
        series.image_loc = str(image_loc)
        series.annot_loc = str(image_loc)
        if series.acquisition_id is None:
            series.acquisition = acq
        if not series.timepts or series.timepts == "0":
            series.timepts = str(position_info.n_timepoints)
    session.flush()
    outcome.series_id = series.id

    # ---- Step 1: extract TIFs directly to the staged layout ----
    # We do this BEFORE the orchestrator's step_stage_images because LIF
    # doesn't go through that step at all. Track it as a synthetic
    # `stage_images` run so the GUI's pipeline column shows progress.
    nz = position_info.n_planes
    run = _get_or_create_run(session, series.id, "stage_images")
    run.status = RunStatus.RUNNING
    run.started_at = datetime.now(tz=timezone.utc)
    session.flush()

    def _path_fn(t: int, z: int, raw_ch: int) -> Path:
        role = channel_map.get(raw_ch, "")
        subdir = ensure_dir(image_loc / role_subdir(role, raw_ch))
        t1 = t + 1
        p1 = nz - z   # invert Z so deepest plane = p01 (AceTree convention)
        return subdir / f"{series_name}-t{t1:03d}-p{p1:02d}.tif"

    try:
        extract_report = ex.extract_position(
            series_name_in_lif,
            position_info.name,
            path_fn=_path_fn,
            bit_depth_policy=bit_depth_policy,
            compress=compress,
            overwrite=overwrite,
            on_progress=(
                lambda done, total: on_progress(series_name, done, total)
            )
            if on_progress is not None
            else None,
        )
    except Exception as exc:  # noqa: BLE001
        run.status = RunStatus.FAILED
        run.error_excerpt = repr(exc)[:1024]
        outcome.failed_step = "stage_images"
        outcome.error = repr(exc)
        return outcome

    if extract_report.error is not None:
        run.status = RunStatus.FAILED
        run.error_excerpt = extract_report.error[:1024]
        outcome.failed_step = "stage_images"
        outcome.error = extract_report.error
        return outcome
    run.status = RunStatus.COMPLETE
    run.completed_at = datetime.now(tz=timezone.utc)
    run.output_summary = {
        "extracted_from": "lif",
        "planes_written": extract_report.planes_written,
        "planes_skipped": extract_report.planes_skipped,
        "bytes_written": extract_report.bytes_written,
    }
    outcome.stage_outcome = StageOutcome(
        position=position_num,
        series_name=series_name,
        image_loc=image_loc,
        written=extract_report.planes_written,
        skipped=extract_report.planes_skipped,
        completed_at=datetime.now(tz=timezone.utc),
    )
    outcome.steps["stage_images"] = RunStatus.COMPLETE

    # ---- Step 2: write Properties.xml directly to dats/ ----
    xml_path = dats_dir / f"{stem}_Position {position_num}_Properties.xml"
    ex.write_position_xml(series_name_in_lif, position_info.name, xml_path)

    # ---- Step 3: parse metadata → MicroscopyMetadata (skips the copy
    #              loop that step_stage_metadata would do; the file is
    #              already in dats/ where we want it).
    run = _get_or_create_run(session, series.id, "stage_metadata")
    run.status = RunStatus.RUNNING
    run.started_at = datetime.now(tz=timezone.utc)
    try:
        from ..parsers.leica_metadata import parse_properties_xml
        md = parse_properties_xml(xml_path)
        _apply_microscopy_row(series, md, raw_path=str(xml_path))
        run.status = RunStatus.COMPLETE
        run.completed_at = datetime.now(tz=timezone.utc)
        run.output_summary = {"copied": 1, "parsed": True}
        outcome.steps["stage_metadata"] = RunStatus.COMPLETE
    except Exception as exc:  # noqa: BLE001
        run.status = RunStatus.FAILED
        run.error_excerpt = repr(exc)[:1024]
        outcome.failed_step = "stage_metadata"
        outcome.error = repr(exc)
        return outcome

    voxel_xy = (
        series.microscopy.voxel_xy_um if series.microscopy else None
    )
    voxel_z = (
        series.microscopy.voxel_z_um if series.microscopy else None
    )

    # ---- Step 4: compute_timestamps ----
    if stop_after >= STEPS.index("compute_timestamps"):
        run = _get_or_create_run(session, series.id, "compute_timestamps")
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(tz=timezone.utc)
        try:
            n_rows, csv_path = step_compute_timestamps(series, position_num)
            run.status = RunStatus.COMPLETE
            run.completed_at = datetime.now(tz=timezone.utc)
            run.output_summary = {
                "timepoints": n_rows,
                "csv": str(csv_path) if csv_path else None,
            }
            outcome.steps["compute_timestamps"] = RunStatus.COMPLETE
        except Exception as exc:  # noqa: BLE001
            run.status = RunStatus.FAILED
            run.error_excerpt = repr(exc)[:1024]
            outcome.failed_step = "compute_timestamps"
            outcome.error = repr(exc)
            return outcome

    # ---- Step 5: write_acetree_config ----
    if stop_after >= STEPS.index("write_acetree_config"):
        run = _get_or_create_run(session, series.id, "write_acetree_config")
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(tz=timezone.utc)
        try:
            cfg = step_write_acetree_config(
                series, image_loc, position_info.n_timepoints, nz, voxel_xy, voxel_z
            )
            run.status = RunStatus.COMPLETE
            run.completed_at = datetime.now(tz=timezone.utc)
            run.output_summary = {"path": str(cfg)}
            outcome.steps["write_acetree_config"] = RunStatus.COMPLETE
        except Exception as exc:  # noqa: BLE001
            run.status = RunStatus.FAILED
            run.error_excerpt = repr(exc)[:1024]
            outcome.failed_step = "write_acetree_config"
            outcome.error = repr(exc)
            return outcome

    # ---- Step 6: write_embryodb_xml ----
    if stop_after >= STEPS.index("write_embryodb_xml"):
        run = _get_or_create_run(session, series.id, "write_embryodb_xml")
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(tz=timezone.utc)
        try:
            target = step_write_embryodb_xml(series, legacy_dir)
            run.status = RunStatus.COMPLETE
            run.completed_at = datetime.now(tz=timezone.utc)
            run.output_summary = {"path": str(target)}
            outcome.steps["write_embryodb_xml"] = RunStatus.COMPLETE
        except Exception as exc:  # noqa: BLE001
            run.status = RunStatus.FAILED
            run.error_excerpt = repr(exc)[:1024]
            outcome.failed_step = "write_embryodb_xml"
            outcome.error = repr(exc)
            return outcome

    # ---- Step 7: create_alias_symlink ----
    if stop_after >= STEPS.index("create_alias_symlink"):
        run = _get_or_create_run(session, series.id, "create_alias_symlink")
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(tz=timezone.utc)
        try:
            alias = _series_alias(opts, series_name)
            step_create_alias_symlink(image_loc, alias)
            run.status = RunStatus.COMPLETE
            run.completed_at = datetime.now(tz=timezone.utc)
            run.output_summary = {"alias": str(alias) if alias else None}
            outcome.steps["create_alias_symlink"] = RunStatus.COMPLETE
        except Exception as exc:  # noqa: BLE001
            run.status = RunStatus.FAILED
            run.error_excerpt = repr(exc)[:1024]
            outcome.failed_step = "create_alias_symlink"
            outcome.error = repr(exc)
            return outcome

    # ---- Step 8: write_matlab_params ----
    if stop_after >= STEPS.index("write_matlab_params"):
        run = _get_or_create_run(session, series.id, "write_matlab_params")
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(tz=timezone.utc)
        try:
            target = step_write_matlab_params(
                image_loc,
                protocol,
                opts.parameter_overrides,
                voxel_xy,
                voxel_z,
                position_info.n_timepoints,
                nz,
            )
            run.status = RunStatus.COMPLETE
            run.completed_at = datetime.now(tz=timezone.utc)
            run.output_summary = {"path": str(target)}
            outcome.steps["write_matlab_params"] = RunStatus.COMPLETE
        except Exception as exc:  # noqa: BLE001
            run.status = RunStatus.FAILED
            run.error_excerpt = repr(exc)[:1024]
            outcome.failed_step = "write_matlab_params"
            outcome.error = repr(exc)
            return outcome

    # ---- Worker steps stay PENDING (unchanged contract with the worker) ----
    for ws in ("run_starrynite", "run_red_extract", "run_measure"):
        _get_or_create_run(session, series.id, ws)

    return outcome


__all__ = ["import_lif"]
