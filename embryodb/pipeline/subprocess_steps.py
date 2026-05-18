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


def _write_series_list(tmp_dir: Path, series_name: str, image_loc: Path) -> Path:
    """Write a single-line acebatch3 series list file. Returns the path."""
    list_file = tmp_dir / f"{series_name}.list"
    list_file.write_text(str(image_loc) + "\n", encoding="utf-8")
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
    log_path = ensure_dir(image_loc / "dats") / f"{series_name}-run_starrynite.log"

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

    log_path = ensure_dir(image_loc / "dats") / f"{series_name}-run_red_extract.log"

    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)

    with tempfile.TemporaryDirectory() as tmp_dir:
        list_file = _write_series_list(Path(tmp_dir), series_name, image_loc)
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

    log_path = ensure_dir(image_loc / "dats") / f"{series_name}-run_measure.log"

    with session_scope() as s:
        run = s.get(PipelineStepRun, run_id)
        if run is not None:
            run.log_path = str(log_path)

    with tempfile.TemporaryDirectory() as tmp_dir:
        list_file = _write_series_list(Path(tmp_dir), series_name, image_loc)
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


__all__ = [
    "step_run_starrynite",
    "step_run_red_extract",
    "step_run_measure",
    "HEARTBEAT_INTERVAL",
]
