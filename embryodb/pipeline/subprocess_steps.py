"""Subprocess wrappers for the three long-running pipeline steps.

These functions are called by the background worker (worker.py). Each one:
- Manages its own DB sessions (so heartbeats commit independently of the
  caller's transaction context).
- Writes a log file under <image_loc>/dats/.
- Updates PipelineStepRun.status, .completed_at, .log_path, and .error_excerpt.
- Updates PipelineStepRun.heartbeat_at every HEARTBEAT_INTERVAL seconds so the
  GUI can detect a stale worker.

None of these functions call _begin()/_complete()/_fail() from orchestrate.py —
the worker already set status=RUNNING before calling; these functions transition
to COMPLETE, FAILED, or SKIPPED and commit.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..database import session_scope
from ..fsutil import chmod_if_possible, ensure_dir
from ..models import PipelineStepRun, RunStatus

HEARTBEAT_INTERVAL = 30  # seconds between DB heartbeat writes during subprocess run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tail_log(path: Path, n: int = 30) -> str:
    """Return last `n` lines of a log file as a single string."""
    try:
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return "(log unreadable)"


def _run_with_heartbeat(
    cmd: list[str],
    log_path: Path,
    run_id: int,
    *,
    cwd: str | None = None,
) -> int:
    """Spawn cmd, stream output to log_path, heartbeat the DB row every
    HEARTBEAT_INTERVAL seconds. Returns the process returncode."""
    ensure_dir(log_path.parent)
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_fp:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            cwd=cwd,
        )

    # Polling loop: update heartbeat while subprocess is running.
    while proc.poll() is None:
        time.sleep(HEARTBEAT_INTERVAL)
        try:
            with session_scope() as s:
                run = s.get(PipelineStepRun, run_id)
                if run is not None:
                    run.heartbeat_at = datetime.now(tz=timezone.utc)
        except Exception:
            pass  # Heartbeat failures are non-fatal; the step continues.

    # Apply project permissions to the log file.
    chmod_if_possible(log_path, 0o664)

    return proc.returncode


def _finish_run(run_id: int, log_path: Path, returncode: int) -> None:
    """Commit the final COMPLETE or FAILED status for a run row."""
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is None:
            return
        run.log_path = str(log_path)
        run.completed_at = datetime.now(tz=timezone.utc)
        if returncode == 0:
            run.status = RunStatus.COMPLETE
        else:
            run.status = RunStatus.FAILED
            run.error_excerpt = _tail_log(log_path)


def _skip_run(run_id: int, reason: str) -> None:
    """Mark a run as SKIPPED (e.g. no reporter channel)."""
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is None:
            return
        run.status = RunStatus.SKIPPED
        run.completed_at = datetime.now(tz=timezone.utc)
        run.error_excerpt = reason


def _write_series_list(tmp_dir: Path, series_name: str) -> Path:
    """Write a single-line acebatch3 series list file. Returns the path.

    acebatch3 treats each line as a series name, looks up
    <embryoDB_source_dir>/<name>.xml, and derives image/annot paths from there.
    Do NOT write the image_loc path — the Java tool would prepend source_dir to
    it and produce a nonsensical combined path.
    """
    list_file = tmp_dir / f"{series_name}.list"
    list_file.write_text(series_name + "\n", encoding="utf-8")
    return list_file


# ---------------------------------------------------------------------------
# Public step functions
# ---------------------------------------------------------------------------


def step_run_starrynite(
    series_id: int,
    series_name: str,
    image_loc: Path,
    run_id: int,
) -> None:
    """Wrap matlab_SN_cluster.pl for one series.

    Command shape (derived from matlabRunner.pl line 21):
        nice perl <tools3_dir>/matlab_SN_cluster.pl \\
             <image_loc>/tif/<series_name> \\
             <image_loc>/matlabParams

    The Perl script calls chdir($SCRIPTLOCATION) internally, so we set
    cwd=tools3_dir so it can find starrynite_traceonly/starrynite relative
    to itself.
    """
    log_path = ensure_dir(image_loc / "logs") / f"{series_name}-run_starrynite.log"

    # Record log_path before the subprocess starts so the GUI can open it.
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)

    cmd = [
        "nice",
        "perl",
        str(settings.tools3_dir / "matlab_SN_cluster.pl"),
        str(image_loc / "tif" / series_name),
        str(image_loc / "matlabParams"),
    ]
    returncode = _run_with_heartbeat(
        cmd, log_path, run_id, cwd=str(settings.tools3_dir)
    )
    _finish_run(run_id, log_path, returncode)


def step_run_red_extract(
    series_id: int,
    series_name: str,
    image_loc: Path,
    run_id: int,
    protocol_channel_map: dict,
) -> None:
    """Wrap acebatch3.jar RedExtractor1 for one series.

    Skipped automatically when the protocol has no reporter channel.

    Command shape:
        nice java -mx500m -cp <tools3_dir>/acebatch3.jar \\
             RedExtractor1 <series_list_file>
    """
    if "reporter" not in protocol_channel_map.values():
        _skip_run(run_id, "no reporter channel in protocol channel_map")
        return

    log_path = ensure_dir(image_loc / "logs") / f"{series_name}-run_red_extract.log"

    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)

    with tempfile.TemporaryDirectory() as tmp_dir:
        list_file = _write_series_list(Path(tmp_dir), series_name)
        cmd = [
            "nice",
            settings.java_command,
            f"-mx{settings.java_mx}",
            "-cp",
            str(settings.tools3_dir / "acebatch3.jar"),
            "RedExtractor1",
            str(list_file),
        ]
        returncode = _run_with_heartbeat(cmd, log_path, run_id)

    _finish_run(run_id, log_path, returncode)


def step_run_measure(
    series_id: int,
    series_name: str,
    image_loc: Path,
    run_id: int,
    protocol_channel_map: dict,
) -> None:
    """Wrap acebatch3.jar Measure1 for one series.

    Skipped automatically when the protocol has no reporter channel.

    Command shape:
        nice java -mx500m -cp <tools3_dir>/acebatch3.jar \\
             Measure1 <series_list_file>
    """
    if "reporter" not in protocol_channel_map.values():
        _skip_run(run_id, "no reporter channel in protocol channel_map")
        return

    log_path = ensure_dir(image_loc / "logs") / f"{series_name}-run_measure.log"

    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)

    with tempfile.TemporaryDirectory() as tmp_dir:
        list_file = _write_series_list(Path(tmp_dir), series_name)
        cmd = [
            "nice",
            settings.java_command,
            f"-mx{settings.java_mx}",
            "-cp",
            str(settings.tools3_dir / "acebatch3.jar"),
            "Measure1",
            str(list_file),
        ]
        returncode = _run_with_heartbeat(cmd, log_path, run_id)

    _finish_run(run_id, log_path, returncode)


def step_stage_images(series_id: int, run_id: int) -> None:
    """Worker-side staging: copy + LZW-compress raw TIFs into the series'
    image_loc. The orchestrator skips its inline stage_images when the user
    requested a delay; this function is what the worker eventually runs.

    All required inputs (source_dir, protocol, image_loc_root, position) are
    rehydrated from the DB so the worker doesn't need the caller's
    Python-side state.
    """
    from .stage import plan_acquisition, stage_position
    from ..models import Acquisition, Series

    log_path: Path | None = None
    started_at = datetime.now(tz=timezone.utc)

    # Resolve everything we need from the DB up front.
    with session_scope() as s:
        series = s.get(Series, series_id)
        if series is None:
            _fail_run_now(run_id, f"series id={series_id} not found")
            return
        if series.acquisition_id is None:
            _fail_run_now(run_id, f"series {series.series_name!r} has no acquisition; cannot re-stage")
            return
        acq = s.get(Acquisition, series.acquisition_id)
        if acq is None or not acq.source_dir:
            _fail_run_now(run_id, f"acquisition {series.acquisition_id} has no source_dir")
            return
        protocol = acq.protocol
        if protocol is None:
            _fail_run_now(run_id, f"acquisition {acq.name!r} has no protocol")
            return

        # Capture all the scalars we need; ORM objects expire after the
        # session closes.
        series_name = series.series_name
        position = series.position
        if position is None:
            _fail_run_now(run_id, f"series {series_name!r} has no position recorded")
            return
        source_dir = Path(acq.source_dir)
        parser_name = acq.parser_name or "leica_tilescan"
        image_loc = Path(series.image_loc) if series.image_loc else None
        if image_loc is None:
            _fail_run_now(run_id, f"series {series_name!r} has no image_loc")
            return
        # image_loc_root is the parent-of-parent of image_loc:
        # /murrlab3/<user>/images/<series>/   →   /murrlab3/<user>/images/
        image_loc_root = image_loc.parent
        slices_str = (protocol.defaults or {}).get("slices") or ""
        slices = int(slices_str) if slices_str.isdigit() else 67
        # channel_map keys come out of JSON as strings; the staging code
        # expects int keys.
        channel_map = {int(k): v for k, v in (protocol.channel_map or {}).items()}

    # Log location matches the other subprocess steps.
    log_path = ensure_dir(image_loc / "logs") / f"{series_name}-stage_images.log"
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)
            run.started_at = started_at
            run.heartbeat_at = started_at

    try:
        if not source_dir.is_dir():
            raise FileNotFoundError(
                f"source_dir does not exist: {source_dir}  "
                "(was it deleted between scheduling and now?)"
            )
        plan = plan_acquisition(source_dir, _DummyProtocol(channel_map), parser_name=parser_name)
        if position not in plan.positions:
            raise ValueError(
                f"position {position} not found in {source_dir} (plan had positions: "
                f"{sorted(plan.positions)})"
            )
        pos_plan = plan.positions[position]
        # Stage with periodic heartbeat updates. stage_position is blocking
        # and single-threaded; we run it on a background thread so the main
        # thread can heartbeat without waiting for I/O completion.
        outcome_holder: dict[str, object] = {}
        exc_holder: list[BaseException] = []

        def _do_stage():
            try:
                outcome_holder["out"] = stage_position(
                    pos_plan,
                    channel_map,
                    image_loc_root=image_loc_root,
                    slices=slices,
                    compress_with_lzw=True,
                    overwrite=True,  # worker re-runs always overwrite
                )
            except BaseException as exc:
                exc_holder.append(exc)

        import threading
        t = threading.Thread(target=_do_stage, daemon=True)
        t.start()
        while t.is_alive():
            t.join(timeout=HEARTBEAT_INTERVAL)
            try:
                with session_scope() as s:
                    run = s.get(PipelineStepRun, run_id)
                    if run is not None:
                        run.heartbeat_at = datetime.now(tz=timezone.utc)
            except Exception:
                pass

        if exc_holder:
            raise exc_holder[0]

        outcome = outcome_holder.get("out")
        # Write a brief summary into the log.
        try:
            with open(log_path, "w", encoding="utf-8") as fp:
                fp.write(
                    f"stage_images for {series_name}\n"
                    f"  source_dir: {source_dir}\n"
                    f"  position:   {position}\n"
                    f"  image_loc:  {image_loc}\n"
                    f"  raw count:  {plan.raw_count}\n"
                    f"  written:    {getattr(outcome, 'written', 0)}\n"
                    f"  skipped:    {getattr(outcome, 'skipped', 0)}\n"
                    f"  errors:     {len(getattr(outcome, 'errors', []))}\n"
                )
                for name, err in getattr(outcome, "errors", []):
                    fp.write(f"  err: {name}: {err}\n")
        except OSError:
            pass

        # Record success — also update series.timepts from the actual staged
        # file count, mirroring what the inline orchestrator does.
        with session_scope() as s:
            run = s.get(PipelineStepRun, run_id)
            if run is not None:
                run.status = RunStatus.COMPLETE
                run.completed_at = datetime.now(tz=timezone.utc)
                run.output_summary = {
                    "written": getattr(outcome, "written", 0),
                    "skipped": getattr(outcome, "skipped", 0),
                    "errors": len(getattr(outcome, "errors", [])),
                }
            series = s.get(Series, series_id)
            if series is not None:
                series.timepts = str(pos_plan.n_timepoints)
    except BaseException as exc:  # noqa: BLE001 — we need the full message
        _fail_run_now(run_id, f"{type(exc).__name__}: {exc}", log_path=log_path)


def _fail_run_now(run_id: int, message: str, log_path: Path | None = None) -> None:
    """Mark a step run as FAILED with `message` as the error_excerpt."""
    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is None:
            return
        run.status = RunStatus.FAILED
        run.completed_at = datetime.now(tz=timezone.utc)
        run.error_excerpt = message[:1024]
        if log_path is not None:
            run.log_path = str(log_path)
    if log_path is not None:
        try:
            with open(log_path, "a", encoding="utf-8") as fp:
                fp.write(f"\nFAILED: {message}\n")
        except OSError:
            pass


class _DummyProtocol:
    """Minimal stand-in for the ORM Protocol object so plan_acquisition can
    read .channel_map without needing a live SQLAlchemy session."""
    def __init__(self, channel_map: dict[int, str]) -> None:
        self.channel_map = {str(k): v for k, v in channel_map.items()}


__all__ = [
    "step_run_starrynite",
    "step_run_red_extract",
    "step_run_measure",
    "step_stage_images",
    "HEARTBEAT_INTERVAL",
]
