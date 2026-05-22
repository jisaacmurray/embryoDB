"""Per-machine background worker for the embryoDB pipeline.

The worker picks up PENDING PipelineStepRun rows for the three subprocess
steps (run_starrynite, run_red_extract, run_measure) and executes them in
series order (series.id ASC), one step at a time.

Properties
----------
- GUI-detachable: started with start_new_session=True so closing the GUI
  does not kill the worker.
- Crash-safe: if the worker dies mid-step the row stays RUNNING with a stale
  heartbeat_at. On next startup, _reset_stale_running() requeues those rows
  as PENDING so they are retried from scratch (steps are idempotent — outputs
  are overwritten).
- One per machine: a pidfile at <worker_pidfile_dir>/embryodb-worker-<host>.pid
  prevents duplicate instances. spawn_worker() checks before forking.
- Self-terminating: after MAX_IDLE_LOOPS consecutive idle polls the worker
  exits. The GUI re-spawns it when new work arrives.

Usage
-----
From Python (GUI / tests):
    from embryodb.pipeline.worker import spawn_worker, worker_is_running
    spawn_worker()  # no-op if already alive

CLI:
    embryodb pipeline worker
    embryodb-worker           # installed entry point
"""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from .. import database
from ..config import settings
from ..models import (
    Acquisition,
    PipelineStepRun,
    Protocol,
    RunStatus,
    Series,
)
from . import subprocess_steps

# Steps handled by this worker (in execution order within a series).
# stage_images is here so the heavy I/O step can be scheduled (delayed) via
# PipelineStepRun.not_before. Inline steps that run in the orchestrator
# (stage_metadata, write_acetree_config, write_embryodb_xml,
# create_alias_symlink, write_matlab_params) are not in this tuple — those
# are fast and stay synchronous in import_acquisition.
WORKER_STEPS = ("stage_images", "run_starrynite", "run_red_extract", "run_measure")

# A RUNNING row with heartbeat_at older than this is assumed crashed.
STALE_THRESHOLD = timedelta(minutes=5)

# Seconds to sleep between idle polls.
IDLE_SLEEP = 10

# Exit after this many consecutive idle polls (~MAX_IDLE_LOOPS * IDLE_SLEEP seconds).
MAX_IDLE_LOOPS = 30  # ~5 minutes


# ---------------------------------------------------------------------------
# Pidfile helpers
# ---------------------------------------------------------------------------


def _pidfile() -> Path:
    return settings.worker_pidfile_dir / f"embryodb-worker-{socket.gethostname()}.pid"


def _write_pidfile() -> None:
    _pidfile().write_text(str(os.getpid()), encoding="utf-8")


def _remove_pidfile() -> None:
    try:
        _pidfile().unlink(missing_ok=True)
    except OSError:
        pass


def worker_is_running() -> bool:
    """Return True if a live worker process exists for this host."""
    pf = _pidfile()
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)  # signal 0: check existence without sending anything
        return True
    except (OSError, ValueError):
        # Process doesn't exist or pidfile is corrupt — clean up.
        _remove_pidfile()
        return False


def spawn_worker() -> subprocess.Popen | None:
    """Fork a detached worker process. Returns None if one is already alive."""
    if worker_is_running():
        return None
    return subprocess.Popen(
        [sys.executable, "-m", "embryodb.pipeline.worker"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------


def _reset_stale_running(session) -> int:
    """Requeue any RUNNING worker-step rows whose heartbeat has gone stale.

    Returns the number of rows reset.
    """
    cutoff = datetime.now(tz=timezone.utc) - STALE_THRESHOLD
    stale = (
        session.query(PipelineStepRun)
        .filter(
            PipelineStepRun.step.in_(WORKER_STEPS),
            PipelineStepRun.status == RunStatus.RUNNING,
            PipelineStepRun.heartbeat_at < cutoff,
        )
        .all()
    )
    for run in stale:
        run.status = RunStatus.PENDING
        run.started_at = None
        run.heartbeat_at = None
        run.error_excerpt = "reset: stale heartbeat (worker crashed?)"
    if stale:
        session.flush()
    return len(stale)


def _prerequisite_ok(session, series_id: int, step: str) -> bool:
    """Return True if all steps preceding `step` are COMPLETE or SKIPPED.

    Inline steps must all be done before any worker step. Within worker
    steps, ordering follows WORKER_STEPS.
    """
    # stage_images was moved out of inline_steps once it became
    # worker-runnable for off-hours scheduling.
    inline_steps = (
        "stage_metadata",
        "write_acetree_config",
        "write_embryodb_xml",
        "create_alias_symlink",
        "write_matlab_params",
    )
    terminal = {RunStatus.COMPLETE, RunStatus.SKIPPED}

    for s in inline_steps:
        row = (
            session.query(PipelineStepRun)
            .filter_by(series_id=series_id, step=s)
            .one_or_none()
        )
        if row is not None and row.status not in terminal:
            return False

    # Check preceding WORKER_STEPS.
    for ws in WORKER_STEPS:
        if ws == step:
            break
        row = (
            session.query(PipelineStepRun)
            .filter_by(series_id=series_id, step=ws)
            .one_or_none()
        )
        if row is None or row.status not in terminal:
            return False

    return True


def _next_work_item(session) -> tuple[Series, str, PipelineStepRun] | None:
    """Find the next (series, step, run) to execute.

    Selects the PENDING (or stale-RUNNING — already requeued) worker-step
    row belonging to the series with the lowest id, subject to all
    prerequisite steps being COMPLETE/SKIPPED.
    """
    # Candidate runs: PENDING worker-step rows ordered by series.id ASC.
    # Also exclude rows whose `not_before` is still in the future (off-hours
    # scheduling).
    from sqlalchemy import or_
    now = datetime.now(tz=timezone.utc)
    candidates = (
        session.query(PipelineStepRun, Series)
        .join(Series, PipelineStepRun.series_id == Series.id)
        .filter(
            PipelineStepRun.step.in_(WORKER_STEPS),
            PipelineStepRun.status == RunStatus.PENDING,
            or_(
                PipelineStepRun.not_before.is_(None),
                PipelineStepRun.not_before <= now,
            ),
        )
        .order_by(Series.id.asc())
        .all()
    )

    # Walk candidates in WORKER_STEPS order per series.
    seen_series: set[int] = set()
    step_order = {s: i for i, s in enumerate(WORKER_STEPS)}

    # Group by series and respect step ordering.
    by_series: dict[int, list[tuple[PipelineStepRun, Series]]] = {}
    for run, series in candidates:
        by_series.setdefault(series.id, []).append((run, series))

    for series_id in sorted(by_series):
        if series_id in seen_series:
            continue
        runs_for_series = sorted(
            by_series[series_id], key=lambda x: step_order[x[0].step]
        )
        for run, series in runs_for_series:
            if _prerequisite_ok(session, series_id, run.step):
                return series, run.step, run
        seen_series.add(series_id)

    return None


def _get_channel_map(session, series: Series) -> dict:
    """Resolve protocol channel_map for a series. Falls back to empty dict."""
    if series.acquisition_id is None:
        return {}
    acq = session.get(Acquisition, series.acquisition_id)
    if acq is None or acq.protocol_id is None:
        return {}
    proto = session.get(Protocol, acq.protocol_id)
    if proto is None:
        return {}
    # channel_map keys are stored as strings in JSON; convert to int if needed.
    return {k: v for k, v in (proto.channel_map or {}).items()}


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------


def run_worker() -> None:
    """Main blocking loop. Returns when the queue is empty (MAX_IDLE_LOOPS)."""
    _write_pidfile()
    atexit.register(_remove_pidfile)

    def _handle_sigterm(*_):
        _remove_pidfile()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    idle_count = 0

    while True:
        series_obj = None
        step_name = None
        run_obj = None
        image_loc = None
        channel_map: dict = {}
        series_id = None
        series_name = None
        run_id = None

        with database.session_scope() as s:
            _reset_stale_running(s)
            item = _next_work_item(s)
            if item is None:
                idle_count += 1
                if idle_count >= MAX_IDLE_LOOPS:
                    break
            else:
                idle_count = 0
                series_obj, step_name, run_obj = item
                # Mark RUNNING before leaving the session so the GUI sees it.
                run_obj.status = RunStatus.RUNNING
                run_obj.started_at = datetime.now(tz=timezone.utc)
                run_obj.heartbeat_at = run_obj.started_at
                image_loc = Path(series_obj.image_loc)
                channel_map = _get_channel_map(s, series_obj)
                # Capture scalar values — ORM objects expire after session close.
                series_id = series_obj.id
                series_name = series_obj.series_name
                run_id = run_obj.id

        if series_id is None:
            time.sleep(IDLE_SLEEP)
            continue

        # Run the subprocess step (manages its own DB sessions for heartbeats).
        if step_name == "stage_images":
            subprocess_steps.step_stage_images(series_id, run_id)
        elif step_name == "run_starrynite":
            subprocess_steps.step_run_starrynite(
                series_id, series_name, image_loc, run_id
            )
        elif step_name == "run_red_extract":
            subprocess_steps.step_run_red_extract(
                series_id, series_name, image_loc, run_id, channel_map
            )
        elif step_name == "run_measure":
            subprocess_steps.step_run_measure(
                series_id, series_name, image_loc, run_id, channel_map
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if worker_is_running():
        print("embryodb-worker: already running, exiting.", flush=True)
        return
    run_worker()


if __name__ == "__main__":
    main()
