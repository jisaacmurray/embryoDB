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
from datetime import datetime
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


@dataclass(frozen=True)
class JobRow:
    """One row in the Background-jobs panel, from either source.

    ``source`` is ``"detached"`` (a fire-and-forget log file from
    :mod:`embryodb.external_tools`) or ``"pipeline"`` (a
    :class:`~embryodb.models.PipelineStepRun` run by the worker). ``log_path``
    may be ``None`` for a pipeline step that hasn't started yet.
    """

    source: str
    kind: str                      # "Type" column (LIF import / Pipeline / …)
    name: str                      # "Job" column (log filename / series · step)
    status_label: str
    running: bool
    started_at: datetime | None
    last_activity: datetime | None
    log_path: Path | None

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


def _pipeline_row(run, series_name: str) -> JobRow:
    status = str(getattr(run.status, "value", run.status))
    last = run.heartbeat_at or run.completed_at or run.started_at
    step_label = _STEP_LABELS.get(run.step, run.step)
    return JobRow(
        source="pipeline",
        kind="Pipeline",
        name=f"{series_name} · {step_label}",
        status_label=_PIPELINE_STATUS_LABELS.get(status, status),
        running=status == "running",
        started_at=run.started_at,
        last_activity=last,
        log_path=Path(run.log_path) if run.log_path else None,
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


def list_jobs(
    session_cm: Callable | None = None,
    *,
    since: datetime | None = None,
) -> list[JobRow]:
    """All background jobs, newest-first: detached log files + (if a session
    factory is given) DB-backed pipeline steps."""
    rows = [JobRow.from_detached(j) for j in discover_jobs(since=since)]
    if session_cm is not None:
        rows.extend(discover_pipeline_jobs(session_cm, since=since))
    rows.sort(key=lambda r: r.started_at or datetime.min, reverse=True)
    return rows


__all__ = [
    "JobInfo",
    "JobRow",
    "discover_jobs",
    "discover_pipeline_jobs",
    "list_jobs",
]
