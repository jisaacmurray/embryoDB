"""Tests for background-job discovery from on-disk log files.

:mod:`embryodb.jobs` reconstructs detached jobs from the log files (and
``.pid`` sidecars) the launchers leave under the runs directory, so a
restarted GUI can see ongoing/finished jobs without any database. These
tests build those files by hand and assert the reconstruction:

- filename parsing (tag with embedded hyphens, epoch → started_at),
- running vs finished classification (pid liveness + exit trailer),
- the ``since`` time-window filter (running always included).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import pytest

from embryodb import external_tools
from embryodb.jobs import (
    CancelError,
    cancel_job,
    discover_jobs,
    discover_pipeline_jobs,
    list_jobs,
)


def _write_log(runs_dir, tag, epoch, pid, *, body="working...\n", exit_code=None):
    """Create an embryodb-<tag>-<epoch>-<pid>.log (+ .pid sidecar)."""
    log = runs_dir / f"embryodb-{tag}-{epoch}-{os.getpid()}.log"
    text = body
    if exit_code is not None:
        text += f"{external_tools._EXIT_TRAILER}={exit_code}\n"
    log.write_text(text, encoding="utf-8")
    if pid is not None:
        external_tools._pidfile_for(log).write_text(str(pid), encoding="utf-8")
    return log


def test_runs_dir_defaults_to_stable_home(monkeypatch):
    monkeypatch.delenv("EMBRYODB_RUNS_DIR", raising=False)
    runs = external_tools._embryodb_runs_dir()
    assert runs == external_tools.Path.home() / ".embryodb" / "runs"


def test_runs_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("EMBRYODB_RUNS_DIR", str(tmp_path))
    assert external_tools._embryodb_runs_dir() == tmp_path


def test_discover_empty_dir(tmp_path):
    assert discover_jobs(tmp_path) == []


def test_parse_tag_with_hyphen_and_kind_label(tmp_path):
    _write_log(tmp_path, "lif-import", int(time.time()), os.getpid())
    (job,) = discover_jobs(tmp_path)
    assert job.tag == "lif-import"
    assert job.kind == "LIF import"


def test_finished_job_reads_exit_code(tmp_path):
    # Dead pid + exit trailer → finished with that exit code.
    _write_log(tmp_path, "extract", int(time.time()), pid=999999, exit_code=0)
    (job,) = discover_jobs(tmp_path)
    assert job.running is False
    assert job.exit_code == 0
    assert job.status_label == "done"


def test_failed_job_status(tmp_path):
    _write_log(tmp_path, "extract", int(time.time()), pid=999999, exit_code=2)
    (job,) = discover_jobs(tmp_path)
    assert job.running is False
    assert job.status_label == "failed (exit 2)"


def test_running_job_live_pid(tmp_path):
    # Our own pid is alive and there's no exit trailer → running.
    _write_log(tmp_path, "lif-import", int(time.time()), pid=os.getpid())
    (job,) = discover_jobs(tmp_path)
    assert job.running is True
    assert job.pid == os.getpid()
    assert "running" in job.status_label


def test_interrupted_job_dead_pid_no_trailer(tmp_path):
    # Dead pid, no exit trailer → the process died without recording status.
    _write_log(tmp_path, "trees", int(time.time()), pid=999999)
    (job,) = discover_jobs(tmp_path)
    assert job.running is False
    assert job.exit_code is None
    assert job.status_label == "interrupted"


def test_exit_trailer_wins_over_live_pid(tmp_path):
    # Even if the recorded pid happens to be alive (pid reuse), a present
    # exit trailer means the job has ended.
    _write_log(tmp_path, "extract", int(time.time()), pid=os.getpid(), exit_code=0)
    (job,) = discover_jobs(tmp_path)
    assert job.running is False


def test_since_filter_excludes_old_finished(tmp_path):
    old_epoch = int(time.time()) - 3 * 86400
    log = _write_log(tmp_path, "extract", old_epoch, pid=999999, exit_code=0)
    # Backdate the file mtime so last_activity is 3 days ago.
    os.utime(log, (old_epoch, old_epoch))

    cutoff = datetime.now() - timedelta(days=1)
    assert discover_jobs(tmp_path, since=cutoff) == []
    # No cutoff → it shows up.
    assert len(discover_jobs(tmp_path, since=None)) == 1


def test_since_filter_keeps_running_regardless(tmp_path):
    old_epoch = int(time.time()) - 3 * 86400
    log = _write_log(tmp_path, "lif-import", old_epoch, pid=os.getpid())
    os.utime(log, (old_epoch, old_epoch))

    cutoff = datetime.now() - timedelta(hours=1)
    (job,) = discover_jobs(tmp_path, since=cutoff)
    assert job.running is True


def test_jobs_sorted_newest_first(tmp_path):
    now = int(time.time())
    _write_log(tmp_path, "extract", now - 100, pid=999999, exit_code=0)
    _write_log(tmp_path, "extract", now, pid=999999, exit_code=0)
    _write_log(tmp_path, "extract", now - 50, pid=999999, exit_code=0)
    jobs = discover_jobs(tmp_path)
    starts = [j.started_at for j in jobs]
    assert starts == sorted(starts, reverse=True)


def test_non_matching_files_ignored(tmp_path):
    (tmp_path / "embryodb-extract-123-456.list").write_text("seriesA\n")
    (tmp_path / "random.log").write_text("nope\n")
    (tmp_path / "embryodb-extract-123-456.log.pid").write_text("999\n")
    _write_log(tmp_path, "extract", int(time.time()), pid=999999, exit_code=0)
    assert len(discover_jobs(tmp_path)) == 1


# ---------------------------------------------------------------------------
# DB-backed pipeline steps (what "Re-run pipeline" queues)
# ---------------------------------------------------------------------------


def _seed_run(session, series_name, step, status, **kw):
    from embryodb.models import PipelineStepRun, RunStatus, Series

    series = session.query(Series).filter_by(series_name=series_name).one_or_none()
    if series is None:
        series = Series(series_name=series_name)
        session.add(series)
        session.flush()
    run = PipelineStepRun(
        series_id=series.id, step=step, status=RunStatus(status), **kw
    )
    session.add(run)
    session.flush()
    return run


def test_pipeline_running_step_appears(db_session):
    from embryodb import database

    _seed_run(
        db_session, "20240805_JIM763_L5", "run_starrynite", "running",
        started_at=datetime.now(),
        heartbeat_at=datetime.now(),
        log_path="/tmp/sn.log",
    )
    db_session.commit()

    (job,) = discover_pipeline_jobs(database.session_scope)
    assert job.source == "pipeline"
    assert job.kind == "Pipeline"
    assert "StarryNite" in job.name
    assert job.running is True
    assert job.status_label == "running"
    assert str(job.log_path) == "/tmp/sn.log"


def test_pipeline_pending_step_is_active(db_session):
    from embryodb import database

    _seed_run(db_session, "seriesX", "run_measure", "pending")
    db_session.commit()
    # Even with the "Running only" window (datetime.max), a queued step shows.
    (job,) = discover_pipeline_jobs(database.session_scope, since=datetime.max)
    assert job.status_label == "queued"
    assert job.log_path is None  # not started → no log yet


def test_pipeline_running_only_excludes_finished(db_session):
    from embryodb import database

    _seed_run(
        db_session, "seriesX", "run_measure", "complete",
        completed_at=datetime.now(), log_path="/tmp/m.log",
    )
    db_session.commit()
    assert discover_pipeline_jobs(database.session_scope, since=datetime.max) == []
    # No cutoff → the finished step shows.
    (job,) = discover_pipeline_jobs(database.session_scope, since=None)
    assert job.status_label == "done"


def test_pipeline_since_window_filters_old_finished(db_session):
    from embryodb import database

    old = datetime.now() - timedelta(days=5)
    _seed_run(
        db_session, "seriesX", "run_measure", "complete",
        completed_at=old, log_path="/tmp/m.log",
    )
    db_session.commit()
    cutoff = datetime.now() - timedelta(days=1)
    assert discover_pipeline_jobs(database.session_scope, since=cutoff) == []


def test_list_jobs_merges_both_sources(db_session, tmp_path, monkeypatch):
    from embryodb import database

    monkeypatch.setenv("EMBRYODB_RUNS_DIR", str(tmp_path))
    _write_log(tmp_path, "lif-import", int(time.time()), pid=os.getpid())
    _seed_run(
        db_session, "seriesX", "run_starrynite", "running",
        started_at=datetime.now(), heartbeat_at=datetime.now(),
    )
    db_session.commit()

    rows = list_jobs(database.session_scope)
    sources = {r.source for r in rows}
    assert sources == {"detached", "pipeline"}
    assert len(rows) == 2


def test_discover_pipeline_jobs_no_session_in_list(tmp_path, monkeypatch):
    # list_jobs without a session factory must still return the file jobs.
    monkeypatch.setenv("EMBRYODB_RUNS_DIR", str(tmp_path))
    _write_log(tmp_path, "extract", int(time.time()), pid=999999, exit_code=0)
    rows = list_jobs(None)
    assert len(rows) == 1
    assert rows[0].source == "detached"


# ---------------------------------------------------------------------------
# Cancelling queued jobs
# ---------------------------------------------------------------------------


def _seed_command(session, kind="extract", status="pending", **kw):
    from embryodb.models import CommandJob, RunStatus

    job = CommandJob(
        kind=kind, params={"series_names": ["seriesX"]}, status=RunStatus(status), **kw
    )
    session.add(job)
    session.flush()
    return job


def test_cancel_queued_pipeline_step_marks_failed_with_marker(db_session):
    from embryodb import database
    from embryodb.models import PipelineStepRun, RunStatus

    run = _seed_run(db_session, "seriesX", "run_starrynite", "pending")
    run_id = run.id
    db_session.commit()

    assert cancel_job(database.session_scope, "pipeline", run_id, user="tester") is True

    with database.session_scope() as s:
        row = s.get(PipelineStepRun, run_id)
        # FAILED (not SKIPPED) so a dependent step isn't wrongly unblocked.
        assert row.status == RunStatus.FAILED
        assert row.output_summary.get("cancelled_by") == "tester"
        assert row.started_at is None
    # The panel surfaces the intent, not the raw FAILED status.
    (job,) = discover_pipeline_jobs(database.session_scope, since=None)
    assert job.status_label == "cancelled"
    assert job.cancelable is False  # terminal now


def test_cancel_queued_command_job_marks_skipped(db_session):
    from embryodb import database
    from embryodb.models import CommandJob, RunStatus

    job = _seed_command(db_session, status="pending")
    job_id = job.id
    db_session.commit()

    assert cancel_job(database.session_scope, "command", job_id, user="tester") is True
    with database.session_scope() as s:
        row = s.get(CommandJob, job_id)
        assert row.status == RunStatus.SKIPPED
        assert row.output_summary.get("cancelled_by") == "tester"


def test_cancel_running_job_is_rejected_by_guard(db_session):
    from embryodb import database
    from embryodb.models import PipelineStepRun, RunStatus

    run = _seed_run(
        db_session, "seriesX", "run_starrynite", "running",
        started_at=datetime.now(), heartbeat_at=datetime.now(),
    )
    run_id = run.id
    db_session.commit()

    # Already claimed by a worker → the PENDING-guarded UPDATE matches 0 rows.
    assert cancel_job(database.session_scope, "pipeline", run_id) is False
    with database.session_scope() as s:
        assert s.get(PipelineStepRun, run_id).status == RunStatus.RUNNING


def test_cancel_unknown_source_raises(db_session):
    from embryodb import database

    with pytest.raises(CancelError):
        cancel_job(database.session_scope, "detached", 1)


def test_cancelable_property_only_true_for_queued_db_jobs(db_session):
    from embryodb import database

    _seed_run(db_session, "seriesX", "run_measure", "pending")
    db_session.commit()
    (queued,) = discover_pipeline_jobs(database.session_scope, since=datetime.max)
    assert queued.cancelable is True
    assert queued.job_id is not None
