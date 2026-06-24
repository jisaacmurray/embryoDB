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

from sqlalchemy import select, update

from .. import database
from ..config import settings
from ..models import (
    Acquisition,
    CommandJob,
    PipelineStepRun,
    Protocol,
    RunStatus,
    Series,
)
from . import subprocess_steps

# Seconds between DB heartbeat writes while a queued command job runs. Well
# under STALE_THRESHOLD so a healthy job never looks crashed.
COMMAND_HEARTBEAT = 30

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
    """Fork a detached worker process. Returns None if one is already alive.

    In remote-client mode (settings.remote) this is a no-op: an off-network
    Mac/laptop must not run the heavy pipeline locally. Work it enqueues is
    serviced by a worker resident on penticton claiming the same PENDING rows.
    """
    if settings.remote:
        return None
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


def _claim_next(session) -> tuple[Series, str, PipelineStepRun] | None:
    """Atomically claim the next runnable work item for this worker.

    `_next_work_item` only *reads* a candidate; on a shared DB two workers on
    different hosts can pick the same PENDING row and both run the subprocess.
    The claim closes that race with a conditional UPDATE that flips
    PENDING→RUNNING only while the row is still PENDING — exactly one worker's
    UPDATE matches the WHERE (the losers block on the row lock, then re-evaluate
    after the winner commits, see RUNNING, and affect 0 rows). Losers retry
    against the next candidate. Portable across SQLite and PostgreSQL; no
    SELECT … FOR UPDATE needed.

    Returns (series, step, run) for the claimed row, or None when no runnable
    work remains. The caller must capture any scalars it needs while still
    inside the session.
    """
    while True:
        item = _next_work_item(session)
        if item is None:
            return None
        series_obj, step_name, run_obj = item
        now = datetime.now(tz=timezone.utc)
        result = session.execute(
            update(PipelineStepRun)
            .where(
                PipelineStepRun.id == run_obj.id,
                PipelineStepRun.status == RunStatus.PENDING,
            )
            .values(
                status=RunStatus.RUNNING,
                started_at=now,
                heartbeat_at=now,
                claimed_by=socket.gethostname(),
            )
        )
        # Sync the ORM view with what the bulk UPDATE wrote (or didn't): on a
        # win the row reads RUNNING; on a loss the next _next_work_item re-reads
        # the committed RUNNING status and skips it.
        session.expire(run_obj)
        if result.rowcount == 1:
            return series_obj, step_name, run_obj
        # else: lost the race — loop and pick the next candidate.


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
# Command jobs (batch extract / print-trees / getacd queued by remote clients)
# ---------------------------------------------------------------------------


def _reset_stale_commands(session) -> int:
    """Requeue RUNNING CommandJobs whose heartbeat has gone stale (crashed worker)."""
    cutoff = datetime.now(tz=timezone.utc) - STALE_THRESHOLD
    stale = (
        session.query(CommandJob)
        .filter(
            CommandJob.status == RunStatus.RUNNING,
            CommandJob.heartbeat_at < cutoff,
        )
        .all()
    )
    for job in stale:
        job.status = RunStatus.PENDING
        job.started_at = None
        job.heartbeat_at = None
        job.claimed_by = None
        job.error_excerpt = "reset: stale heartbeat (worker crashed?)"
    if stale:
        session.flush()
    return len(stale)


def _claim_next_command(session) -> tuple[int, str, dict, str | None] | None:
    """Atomically claim the oldest runnable CommandJob for this worker.

    Same guarded-UPDATE pattern as `_claim_next` — flips PENDING→RUNNING only
    while still PENDING, so exactly one worker wins. No prerequisite chain (these
    are independent batch jobs); ordering is by submission (id ASC). Returns
    (job_id, kind, params, log_path) or None.
    """
    from sqlalchemy import or_

    now = datetime.now(tz=timezone.utc)
    while True:
        row = (
            session.query(CommandJob)
            .filter(
                CommandJob.status == RunStatus.PENDING,
                or_(CommandJob.not_before.is_(None), CommandJob.not_before <= now),
            )
            .order_by(CommandJob.id.asc())
            .first()
        )
        if row is None:
            return None
        job_id, kind, params, log_path = (
            row.id,
            row.kind,
            dict(row.params or {}),
            row.log_path,
        )
        result = session.execute(
            update(CommandJob)
            .where(
                CommandJob.id == job_id,
                CommandJob.status == RunStatus.PENDING,
            )
            .values(
                status=RunStatus.RUNNING,
                started_at=now,
                heartbeat_at=now,
                claimed_by=socket.gethostname(),
            )
        )
        if result.rowcount == 1:
            return job_id, kind, params, log_path
        session.expire(row)
        # lost the race — loop; the SQL filter excludes the now-RUNNING row.


def _command_tail(path: Path, n: int = 30) -> str | None:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return None


def _finish_command(
    job_id: int, log_path: Path, *, returncode: int, error: str | None = None
) -> None:
    with database.session_scope() as s:
        job = s.get(CommandJob, job_id)
        if job is None:
            return
        job.log_path = str(log_path)
        job.returncode = returncode
        job.completed_at = datetime.now(tz=timezone.utc)
        job.status = RunStatus.COMPLETE if returncode == 0 else RunStatus.FAILED
        if error:
            job.error_excerpt = error[:4000]


def _execute_command_job(
    job_id: int, kind: str, params: dict, log_path: Path
) -> None:
    """Materialize + run a claimed CommandJob, heartbeating the row until exit.

    Renders the same shell the local launcher would (via the shared builders in
    external_tools), writes the series-list + log into the shared
    `command_log_dir`, and runs from `tools3_dir` so the jars/scripts resolve.
    """
    from .. import external_tools as ext
    from .. import fsutil

    base_dir = Path(settings.tools3_dir)
    if not log_path or str(log_path) in ("", "."):
        log_path = ext.command_job_log_path(job_id)
    log_path = Path(log_path)
    fsutil.ensure_dir(log_path.parent)

    series_names = list(params.get("series_names") or [])
    list_file = log_path.with_suffix(".list")
    try:
        fsutil.safe_write_text(list_file, "\n".join(series_names) + "\n")
        shell = ext.build_command_for_kind(kind, params, list_file, base_dir)
    except Exception as exc:  # bad params / unknown kind — fail the row cleanly
        _finish_command(job_id, log_path, returncode=2, error=f"build failed: {exc}")
        return

    q = ext.shell_quote(str(log_path))
    full = f"({shell}) >> {q} 2>&1; echo \"{ext._EXIT_TRAILER}=$?\" >> {q}"
    try:
        log_path.touch(exist_ok=True)
    except OSError:
        pass
    proc = subprocess.Popen(
        ["bash", "-c", full],
        cwd=str(base_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    while proc.poll() is None:
        time.sleep(COMMAND_HEARTBEAT)
        try:
            with database.session_scope() as s:
                job = s.get(CommandJob, job_id)
                if job is not None:
                    job.heartbeat_at = datetime.now(tz=timezone.utc)
        except Exception:
            pass  # heartbeat failures are non-fatal

    rc = proc.returncode
    fsutil.chmod_if_possible(log_path, 0o664)
    error = None if rc == 0 else _command_tail(log_path)
    _finish_command(job_id, log_path, returncode=rc, error=error)


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
        command: tuple[int, str, dict, str | None] | None = None

        with database.session_scope() as s:
            _reset_stale_running(s)
            _reset_stale_commands(s)
            # Atomic claim: flips PENDING→RUNNING and commits on block exit so
            # the GUI (and other workers) see the row as RUNNING. Pipeline
            # steps take priority; only when none are runnable do we claim a
            # batch command job (extract / print-trees / getacd).
            item = _claim_next(s)
            if item is None:
                command = _claim_next_command(s)
                if command is None:
                    idle_count += 1
                    if idle_count >= MAX_IDLE_LOOPS:
                        break
                else:
                    idle_count = 0
            else:
                idle_count = 0
                series_obj, step_name, run_obj = item
                image_loc = Path(series_obj.image_loc)
                channel_map = _get_channel_map(s, series_obj)
                # Capture scalar values — ORM objects expire after session close.
                series_id = series_obj.id
                series_name = series_obj.series_name
                run_id = run_obj.id

        if series_id is None and command is None:
            time.sleep(IDLE_SLEEP)
            continue

        if command is not None:
            job_id, kind, params, log_path = command
            # _execute_command_job recomputes the path if this is empty.
            _execute_command_job(job_id, kind, params, Path(log_path or "."))
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
