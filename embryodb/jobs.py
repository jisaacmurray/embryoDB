"""Discover detached background jobs from their on-disk log files.

The GUI launchers in :mod:`embryodb.external_tools` spawn fully-detached
subprocesses (LIF imports, extract chains, tree renders). Their only
persistent trace is a log file under the runs directory
(``EMBRYODB_RUNS_DIR``, default ``~/.embryodb/runs``) plus a ``<log>.pid``
sidecar holding the child pid. This module reads those back so a freshly
restarted GUI can list ongoing and recently-finished jobs and re-attach to
their logs — no database, no schema: a job *is* its log file.

Filenames are ``embryodb-<tag>-<epoch>-<launcher_pid>.log``. Liveness comes
from the ``.pid`` sidecar (the filename's pid is the launcher's, not the
child's); completion + exit status come from the ``__EMBRYODB_EXIT=<rc>``
trailer the launcher appends to the log on exit.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .external_tools import _EXIT_TRAILER, _embryodb_runs_dir, _pidfile_for

# Map the filename tag to a human label for the GUI. Unknown tags fall back
# to the raw tag (title-cased) so new launchers show up without edits here.
_KIND_LABELS = {
    "lif-import": "LIF import",
    "extract": "Extract",
    "trees": "Print trees",
}

# embryodb-<tag>-<epoch>-<pid>.log ; tag may itself contain hyphens
# (e.g. "lif-import"), so anchor on the two trailing numeric fields.
_LOG_NAME_RE = re.compile(r"^embryodb-(?P<tag>.+)-(?P<epoch>\d+)-(?P<pid>\d+)\.log$")

_EXIT_RE = re.compile(rf"{re.escape(_EXIT_TRAILER)}=(\d+)")


@dataclass(frozen=True)
class JobInfo:
    """One detached job, reconstructed from its log file on disk."""

    tag: str
    kind: str
    log_path: Path
    pid: int | None          # detached child pid (from the .pid sidecar)
    started_at: datetime     # parsed from the log filename's epoch field
    last_activity: datetime  # log mtime — proxy for "still doing work"
    running: bool
    exit_code: int | None    # from the trailer; None if absent (interrupted)
    size_bytes: int

    @property
    def status_label(self) -> str:
        if self.running:
            pid = f" (pid {self.pid})" if self.pid else ""
            return f"running{pid}"
        if self.exit_code == 0:
            return "done"
        if self.exit_code is not None:
            return f"failed (exit {self.exit_code})"
        return "interrupted"


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by another user
    return True


def _read_pid(log_path: Path) -> int | None:
    try:
        return int(_pidfile_for(log_path).read_text().strip())
    except (OSError, ValueError):
        return None


def _read_exit_code(log_path: Path) -> int | None:
    """Scan the tail of the log for the launcher's exit trailer."""
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as fh:
            if size > 4096:
                fh.seek(-4096, os.SEEK_END)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    matches = _EXIT_RE.findall(tail)
    return int(matches[-1]) if matches else None


def _job_from_log(log_path: Path) -> JobInfo | None:
    m = _LOG_NAME_RE.match(log_path.name)
    if m is None:
        return None
    tag = m.group("tag")
    try:
        started_at = datetime.fromtimestamp(int(m.group("epoch")))
        stat = log_path.stat()
    except (OSError, ValueError, OverflowError):
        return None

    exit_code = _read_exit_code(log_path)
    pid = _read_pid(log_path)
    # The exit trailer is authoritative: once present, the job has ended
    # regardless of pid reuse. Otherwise fall back to pid liveness.
    if exit_code is not None:
        running = False
    else:
        running = pid is not None and _pid_alive(pid)

    return JobInfo(
        tag=tag,
        kind=_KIND_LABELS.get(tag, tag.replace("-", " ").title()),
        log_path=log_path,
        pid=pid,
        started_at=started_at,
        last_activity=datetime.fromtimestamp(stat.st_mtime),
        running=running,
        exit_code=exit_code,
        size_bytes=stat.st_size,
    )


def discover_jobs(
    runs_dir: Path | None = None,
    *,
    since: datetime | None = None,
) -> list[JobInfo]:
    """Reconstruct background jobs from log files in the runs directory.

    Running jobs are always returned. Finished jobs are returned only if
    their last activity is at or after `since` (pass ``None`` for no cutoff).
    Sorted newest-first by start time.
    """
    runs = runs_dir or _embryodb_runs_dir()
    if not runs.is_dir():
        return []

    jobs: list[JobInfo] = []
    for log_path in runs.glob("embryodb-*.log"):
        job = _job_from_log(log_path)
        if job is None:
            continue
        if not job.running and since is not None and job.last_activity < since:
            continue
        jobs.append(job)

    jobs.sort(key=lambda j: j.started_at, reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# Unified job rows — merge file-based detached jobs with DB-backed pipeline
# steps so one panel shows everything the user launched.
# ---------------------------------------------------------------------------

# Worker/orchestrator step name → human label for the panel.
_STEP_LABELS = {
    "stage_images": "Stage images",
    "stage_metadata": "Stage metadata",
    "write_acetree_config": "AceTree config",
    "write_embryodb_xml": "embryoDB XML",
    "create_alias_symlink": "Alias symlink",
    "write_matlab_params": "matlabParams",
    "run_starrynite": "StarryNite",
    "run_red_extract": "Red Extract",
    "run_measure": "Measure",
}

_PIPELINE_STATUS_LABELS = {
    "pending": "queued",
    "running": "running",
    "complete": "done",
    "failed": "failed",
    "skipped": "skipped",
}

# CommandJob.kind → human label for the panel.
_COMMAND_KIND_LABELS = {
    "extract": "Extract",
    "print_trees": "Print trees",
    "getacd": "GetACD",
}


# Marker written into a job's ``output_summary`` when a user cancels it while
# queued. Lets the panel show "cancelled" instead of the raw terminal status
# (FAILED for pipeline steps, SKIPPED for command jobs — see ``cancel_job``).
_CANCELLED_KEY = "cancelled_by"


@dataclass(frozen=True)
class JobRow:
    """One row in the Background-jobs panel, from either source.

    ``source`` is ``"detached"`` (a fire-and-forget log file from
    :mod:`embryodb.external_tools`), ``"pipeline"`` (a
    :class:`~embryodb.models.PipelineStepRun` run by the worker), or
    ``"command"`` (a queued :class:`~embryodb.models.CommandJob`). ``log_path``
    may be ``None`` for a step that hasn't started yet. ``job_id`` is the DB
    primary key for the ``pipeline``/``command`` sources (``None`` for detached
    log-file jobs), and is what :func:`cancel_job` targets.
    """

    source: str
    kind: str                      # "Type" column (LIF import / Pipeline / …)
    name: str                      # "Job" column (log filename / series · step)
    status_label: str
    running: bool
    started_at: datetime | None
    last_activity: datetime | None
    log_path: Path | None
    job_id: int | None = None

    @property
    def cancelable(self) -> bool:
        """True when this is a DB-backed job still queued (PENDING).

        Only queued worker/command jobs can be cancelled — once a job is
        RUNNING it's executing on a (possibly remote) worker we can't reach,
        and terminal jobs are already done. Detached log-file jobs aren't
        queued (they start immediately) so they're never cancelable here.
        """
        return (
            self.source in ("pipeline", "command")
            and self.job_id is not None
            and self.status_label == "queued"
        )

    @classmethod
    def from_detached(cls, job: JobInfo) -> "JobRow":
        return cls(
            source="detached",
            kind=job.kind,
            name=job.log_path.name,
            status_label=job.status_label,
            running=job.running,
            started_at=job.started_at,
            last_activity=job.last_activity,
            log_path=job.log_path,
        )


def _status_label(status: str, output_summary: dict | None) -> str:
    """Human status, surfacing a user cancellation as ``"cancelled"``.

    A cancelled job carries a terminal DB status (FAILED for a pipeline step so
    its dependents don't wrongly unblock; SKIPPED for a standalone command job)
    plus the ``_CANCELLED_KEY`` marker — show the intent, not the raw status."""
    if output_summary and output_summary.get(_CANCELLED_KEY):
        return "cancelled"
    return _PIPELINE_STATUS_LABELS.get(status, status)


def _pipeline_row(run, series_name: str) -> JobRow:
    status = str(getattr(run.status, "value", run.status))
    last = run.heartbeat_at or run.completed_at or run.started_at
    step_label = _STEP_LABELS.get(run.step, run.step)
    return JobRow(
        source="pipeline",
        kind="Pipeline",
        name=f"{series_name} · {step_label}",
        status_label=_status_label(status, run.output_summary),
        running=status == "running",
        started_at=run.started_at,
        last_activity=last,
        log_path=Path(run.log_path) if run.log_path else None,
        job_id=run.id,
    )


def discover_pipeline_jobs(
    session_cm: Callable,
    *,
    since: datetime | None = None,
) -> list[JobRow]:
    """Read worker pipeline steps (``PipelineStepRun``) as job rows.

    Active steps (RUNNING/PENDING) are always returned; terminal steps only
    when they completed at or after `since`. ``since == datetime.max`` (the
    "Running only" window) returns active steps alone. DB/query failures
    degrade to an empty list — the panel still shows the file-based jobs.
    """
    from sqlalchemy import or_, select

    from .models import PipelineStepRun, RunStatus, Series

    active = (RunStatus.RUNNING, RunStatus.PENDING)
    rows: list[JobRow] = []
    try:
        with session_cm() as s:
            q = select(PipelineStepRun, Series.series_name).join(
                Series, PipelineStepRun.series_id == Series.id
            )
            if since is not None:
                cond = PipelineStepRun.status.in_(active)
                if since != datetime.max:
                    cond = or_(cond, PipelineStepRun.completed_at >= since)
                q = q.where(cond)
            for run, series_name in s.execute(q).all():
                rows.append(_pipeline_row(run, series_name))
    except Exception:
        return rows
    return rows


def _command_row(job) -> JobRow:
    status = str(getattr(job.status, "value", job.status))
    last = job.heartbeat_at or job.completed_at or job.started_at or job.created_at
    names = list((job.params or {}).get("series_names") or [])
    n = len(names)
    label = _COMMAND_KIND_LABELS.get(job.kind, job.kind.replace("_", " ").title())
    descr = names[0] if n == 1 else f"{n} series"
    return JobRow(
        source="command",
        kind=label,
        name=f"#{job.id} · {descr}",
        status_label=_status_label(status, job.output_summary),
        running=status == "running",
        started_at=job.started_at or job.created_at,
        last_activity=last,
        log_path=Path(job.log_path) if job.log_path else None,
        job_id=job.id,
    )


def discover_command_jobs(
    session_cm: Callable,
    *,
    since: datetime | None = None,
) -> list[JobRow]:
    """Read queued batch CommandJobs (extract / print-trees / getacd) as job rows.

    Active jobs (RUNNING/PENDING) are always returned; terminal jobs only when
    they completed at or after `since`. ``since == datetime.max`` (the "Running
    only" window) returns active jobs alone. DB failures degrade to an empty
    list so the panel still shows the other sources.
    """
    from sqlalchemy import or_, select

    from .models import CommandJob, RunStatus

    active = (RunStatus.RUNNING, RunStatus.PENDING)
    rows: list[JobRow] = []
    try:
        with session_cm() as s:
            q = select(CommandJob)
            if since is not None:
                cond = CommandJob.status.in_(active)
                if since != datetime.max:
                    cond = or_(cond, CommandJob.completed_at >= since)
                q = q.where(cond)
            for (job,) in s.execute(q).all():
                rows.append(_command_row(job))
    except Exception:
        return rows
    return rows


def list_jobs(
    session_cm: Callable | None = None,
    *,
    since: datetime | None = None,
) -> list[JobRow]:
    """All background jobs, newest-first: detached log files + (if a session
    factory is given) DB-backed pipeline steps and queued command jobs."""
    rows = [JobRow.from_detached(j) for j in discover_jobs(since=since)]
    if session_cm is not None:
        rows.extend(discover_pipeline_jobs(session_cm, since=since))
        rows.extend(discover_command_jobs(session_cm, since=since))
    rows.sort(key=lambda r: _as_aware(r.started_at), reverse=True)
    return rows


class CancelError(ValueError):
    """A job could not be cancelled (unknown source, or no longer queued)."""


def cancel_job(
    session_cm: Callable,
    source: str,
    job_id: int,
    *,
    user: str | None = None,
) -> bool:
    """Cancel a queued (PENDING) worker or command job. Returns True on success.

    Race-safe against the worker's atomic claim: cancellation is a guarded
    UPDATE that only matches while the row is still PENDING (mirroring
    ``pipeline.worker._claim_next``). If a worker claimed the row first the
    UPDATE affects 0 rows and this returns False — the caller reports "already
    started". A cancelled row lands in a terminal state carrying the
    ``_CANCELLED_KEY`` marker so the panel shows "cancelled":

    - **pipeline**: → FAILED. SKIPPED would count as a satisfied prerequisite
      and wrongly unblock the next step (see ``worker._prerequisite_ok``); the
      worker's ``_fail_orphaned_steps`` then strands the dependents, exactly as
      a real step failure would.
    - **command**: → SKIPPED. Command jobs have no dependency chain, so the
      neutral "not run" status is the honest one.

    ``source`` must be ``"pipeline"`` or ``"command"``; anything else (e.g. a
    detached log-file job) raises :class:`CancelError`.
    """
    from sqlalchemy import update

    from .identity import current_user
    from .models import CommandJob, PipelineStepRun, RunStatus

    who = user or current_user()
    now = datetime.now(tz=timezone.utc)
    marker = {_CANCELLED_KEY: who}

    if source == "pipeline":
        model, cancelled_status = PipelineStepRun, RunStatus.FAILED
        extra = {"started_at": None, "heartbeat_at": None}
    elif source == "command":
        model, cancelled_status = CommandJob, RunStatus.SKIPPED
        extra = {}
    else:
        raise CancelError(
            f"cannot cancel a {source!r} job — only queued pipeline/command jobs"
        )

    with session_cm() as s:
        result = s.execute(
            update(model)
            .where(model.id == job_id, model.status == RunStatus.PENDING)
            .values(
                status=cancelled_status,
                completed_at=now,
                error_excerpt=f"cancelled by {who} while queued",
                output_summary=marker,
                **extra,
            )
        )
        return result.rowcount == 1


def _as_aware(dt: datetime | None) -> datetime:
    """Normalize to a tz-aware datetime so naive (detached-log) and aware
    (Postgres timestamptz) values sort together. Naive values are assumed UTC;
    missing values sort last (oldest)."""
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "CancelError",
    "JobInfo",
    "JobRow",
    "cancel_job",
    "discover_jobs",
    "discover_pipeline_jobs",
    "discover_command_jobs",
    "list_jobs",
]
