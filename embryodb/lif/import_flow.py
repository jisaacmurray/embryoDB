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


def extra_timepoint_siblings(series_list, main_name: str) -> list[str]:
    """Sibling series that are *extra timepoints* of ``main_name``.

    The microscope occasionally saves a late extra volume as a SEPARATE LIF
    object named ``<main>_t<N>`` (e.g. ``..._t241`` next to a 240-tp main
    series) instead of as timepoint N of the main acquisition. Those should be
    appended to the end of the main time course so StarryNite/AceTree see one
    continuous movie. Returns the matching names ordered by their numeric
    suffix ascending. The suffix is informational — actual placement is by
    append order (see :func:`_import_one_position`).
    """
    rx = re.compile(rf"^{re.escape(main_name)}_t(\d+)$")
    matched: list[tuple[int, str]] = []
    for s in series_list:
        m = rx.match(s.name)
        if m:
            matched.append((int(m.group(1)), s.name))
    return [name for _, name in sorted(matched)]


def _series_geometry(info) -> tuple[int, int, int, int] | None:
    """``(n_channels, n_planes, x_pixels, y_pixels)`` of a series' first
    position, or ``None`` when the series has no positions. This is the shape
    a movie must share to be concatenated onto another without breaking
    StarryNite/AceTree (which assume a fixed per-volume plane count + frame
    dimensions across the whole time course).
    """
    if not info.positions:
        return None
    p = info.positions[0]
    n_channels = len(info.channels) or len(p.bit_depth)
    return (n_channels, p.n_planes, p.x_pixels, p.y_pixels)


def series_compatible_for_append(main, candidate) -> bool:
    """Whether ``candidate``'s frames can be appended onto ``main``'s movie.

    Compatibility is structural, NOT name-based (the ``_t<N>`` convention is
    unreliable — users don't always follow it). A candidate is appendable when:

    - it has the same per-volume geometry (channel count, plane count, and
      X/Y pixel dimensions) as the main series, and
    - it shares at least one position NAME with the main series (append is
      matched position-by-position by name).

    Timepoint count is deliberately NOT compared — the whole point is that the
    extra movie has a different (usually much shorter) length.
    """
    gm = _series_geometry(main)
    gc = _series_geometry(candidate)
    if gm is None or gc is None or gm != gc:
        return False
    main_pos = {p.name for p in main.positions}
    cand_pos = {p.name for p in candidate.positions}
    return bool(main_pos & cand_pos)


def appendable_candidates(series_list, main_name: str) -> list[str]:
    """All series in ``series_list`` that could be appended onto ``main_name``.

    Geometry-compatible siblings (see :func:`series_compatible_for_append`),
    excluding the main series itself, in document order. Use this to surface
    the full set of choices in the CLI/GUI regardless of how they're named;
    :func:`extra_timepoint_siblings` is the subset confident enough to append
    automatically by default.
    """
    main = next((s for s in series_list if s.name == main_name), None)
    if main is None:
        return []
    return [
        s.name
        for s in series_list
        if s.name != main_name and series_compatible_for_append(main, s)
    ]


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
    append_series: list[str] | None = None,
    auto_append_extra: bool = True,
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
      append_series: optional sibling LIF series whose timepoints are
        appended onto the END of each matching position's main time course
        (matched by position name). Any geometry-compatible series may be
        named here regardless of its name — incompatible ones (different
        plane count / pixel dims / channel count, or no shared position) are
        warned about and skipped (see :func:`series_compatible_for_append`).
        Use for late extra volumes the scope saved as separate objects.
      auto_append_extra: when True (default), also auto-detect and append
        siblings named ``<series_name>_t<N>`` (see
        :func:`extra_timepoint_siblings`). This is the conservative
        auto-default; arbitrary compatible movies (see
        :func:`appendable_candidates`) are only appended when named explicitly
        in ``append_series``. An explicit ``append_series`` is unioned with the
        auto-detected set; both are compatibility-gated.
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
    all_series = ex.list_series()
    series_info = next(
        (s for s in all_series if s.name == series_name), None
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

    # Resolve extra-timepoint sibling series to append (auto-detected
    # ``<main>_t<N>`` + any explicit names), preserving suffix order then
    # caller order. Skip the main itself and unknown names (warn).
    append_names: list[str] = []
    if auto_append_extra:
        append_names.extend(extra_timepoint_siblings(all_series, series_name))
    for name in append_series or []:
        if name != series_name and name not in append_names:
            append_names.append(name)
    append_infos = []
    for name in append_names:
        info = next((s for s in all_series if s.name == name), None)
        if info is None:
            log.warning("append series %r not found in %s; skipping", name, lif_path)
            continue
        if not series_compatible_for_append(series_info, info):
            log.warning(
                "append series %r is not geometry-compatible with %r "
                "(plane count / pixel dims / channel count differ, or no shared "
                "position); skipping",
                name, series_name,
            )
            continue
        append_infos.append(info)

    # Resolve channel mapping. Protocol's channel_map is keyed by raw
    # channel index as a string (matches the format on Protocol rows);
    # an override dict provided by the caller wins when present.
    channel_map: dict[int, str] = {}
    for raw_idx_str, role in (protocol.channel_map or {}).items():
        try:
            channel_map[int(raw_idx_str)] = role
        except ValueError:
            continue
    # Single-channel acquisitions are always the histone (nuclear) channel,
    # regardless of the protocol's default role for channel 0. Some protocols
    # (e.g. JIM113) map ch0 -> reporter, which is wrong when only the histone
    # channel was acquired and would route the nuclei into tifR/.
    n_channels = len(series_info.channels) or len(pos_list[0].bit_depth)
    if n_channels == 1:
        channel_map = {0: "histone"}
    # An explicit caller override always wins (deliberate manual assignment).
    if channel_role_override:
        channel_map.update(channel_role_override)

    # histone and reporter each stage into a single fixed dir (tif/ resp.
    # tifR/), so at most one channel may hold each — two would overwrite/
    # interleave on disk. Reject before any extraction runs.
    for role in ("histone", "reporter"):
        dup = sorted(ch for ch, r in channel_map.items() if r == role)
        if len(dup) > 1:
            raise ValueError(
                f"channel role {role!r} assigned to multiple channels "
                f"{dup}; it stages into a single directory, so only one "
                f"channel may hold it (reassign the rest to 'extra')"
            )

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
        # Match each append series' position by NAME (positions correspond
        # 1:1 across the main and extra-timepoint objects). A missing match
        # is a warn-and-skip — that position simply gets no extra frames.
        extra_segments: list[tuple[str, object]] = []
        for info in append_infos:
            match = next(
                (p for p in info.positions if p.name == pos.name), None
            )
            if match is None:
                log.warning(
                    "append series %r has no position %r; not appending to %s",
                    info.name, pos.name, series_name,
                )
                continue
            extra_segments.append((info.name, match))
        outcome = _import_one_position(
            session,
            ex=ex,
            acq=acq,
            protocol=protocol,
            channel_map=channel_map,
            series_name_in_lif=series_name,
            position_info=pos,
            position_num=position_num,
            extra_segments=extra_segments,
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
    extra_segments: list[tuple[str, object]] | None = None,
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

    # The main position plus any extra-timepoint segments are concatenated
    # into one continuous time course. ``total_nt`` is the combined frame
    # count and drives every downstream step (timepts, AceTree end index,
    # matlabParams end_time) so StarryNite/AceTree see a single movie.
    segments: list[tuple[str, object]] = [
        (series_name_in_lif, position_info)
    ] + list(extra_segments or [])
    total_nt = sum(seg_pos.n_timepoints for _, seg_pos in segments)

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
            timepts=str(total_nt),
        )
        session.add(series)
    else:
        series.image_loc = str(image_loc)
        series.annot_loc = str(image_loc)
        if series.acquisition_id is None:
            series.acquisition = acq
        # Keep timepts in sync with what's being staged. A re-import that
        # appends extra timepoints (e.g. a late _t<N> volume) raises the
        # count, so the DB must track on-disk reality — not the stale value
        # from the original series-create. This matches the acetree config /
        # matlabParams end index, which are already driven by total_nt below.
        series.timepts = str(total_nt)
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

    total_written = 0
    total_skipped = 0
    total_bytes = 0
    offset = 0
    for seg_lif_name, seg_pos in segments:

        def _path_fn(t: int, z: int, raw_ch: int, _offset: int = offset) -> Path:
            role = channel_map.get(raw_ch, "")
            subdir = ensure_dir(image_loc / role_subdir(role, raw_ch))
            # 1-based timepoint, shifted by the running offset so appended
            # segments continue the main movie (e.g. t241 after a 240-tp main).
            t1 = _offset + t + 1
            p1 = nz - z   # invert Z so deepest plane = p01 (AceTree convention)
            return subdir / f"{series_name}-t{t1:03d}-p{p1:02d}.tif"

        try:
            extract_report = ex.extract_position(
                seg_lif_name,
                seg_pos.name,
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
        total_written += extract_report.planes_written
        total_skipped += extract_report.planes_skipped
        total_bytes += extract_report.bytes_written
        offset += seg_pos.n_timepoints

    run.status = RunStatus.COMPLETE
    run.completed_at = datetime.now(tz=timezone.utc)
    run.output_summary = {
        "extracted_from": "lif",
        "segments": len(segments),
        "appended_timepoints": total_nt - position_info.n_timepoints,
        "planes_written": total_written,
        "planes_skipped": total_skipped,
        "bytes_written": total_bytes,
    }
    outcome.stage_outcome = StageOutcome(
        position=position_num,
        series_name=series_name,
        image_loc=image_loc,
        written=total_written,
        skipped=total_skipped,
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
                series, image_loc, total_nt, nz, voxel_xy, voxel_z
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
                total_nt,
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
    # When a delay is requested, push `not_before` onto the worker steps so the
    # background worker waits until off-hours before picking them up (mirrors
    # orchestrate.import_acquisition). delay_hours == 0 leaves them immediately
    # eligible.
    not_before = None
    if opts.delay_hours and opts.delay_hours > 0:
        not_before = datetime.now(tz=timezone.utc) + timedelta(hours=opts.delay_hours)
    for ws in ("run_starrynite", "compute_difficulty", "run_red_extract", "run_measure"):
        wrun = _get_or_create_run(session, series.id, ws)
        if not_before is not None and wrun.status == RunStatus.PENDING:
            wrun.not_before = not_before
        # Record the StarryNite engine choice (default "old" => no-op).
        if ws == "run_starrynite" and opts.sn_engine and opts.sn_engine != "old":
            wrun.params = {**(wrun.params or {}), "sn_engine": opts.sn_engine}

    return outcome


__all__ = ["import_lif"]
