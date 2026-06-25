"""Tests for the subprocess watchdog in pipeline.subprocess_steps.

Focus on the two backstops that catch a crashed-but-hung child (the JIM799
StarryNite job that wedged for 6 days in futex_wait after an MCR
"pure virtual method called" abort):

1. a hard wall-clock cap (deterministic, can't be defeated by intermittent
   CPU from periodic MCR/java helpers), and
2. the crash-signature fast-fail, now including the MCR pure-virtual abort.

We run cheap real subprocesses (sleep / echo+sleep) with HEARTBEAT_INTERVAL
shrunk to keep the tests sub-second.
"""

from __future__ import annotations

import time

import pytest

from embryodb import database
from embryodb.models import PipelineStepRun, RunStatus, Series
from embryodb.pipeline import subprocess_steps as ss


@pytest.fixture
def run_id(db_session):
    series = Series(series_name="20260602_JIM799_pop-1_noAuxin_L1")
    db_session.add(series)
    db_session.flush()
    run = PipelineStepRun(
        series_id=series.id, step="run_starrynite", status=RunStatus.RUNNING
    )
    db_session.add(run)
    db_session.flush()
    return run.id


@pytest.fixture
def fast_heartbeat(monkeypatch):
    # Tighten the poll interval so the watchdog checks fire quickly.
    monkeypatch.setattr(ss, "HEARTBEAT_INTERVAL", 0.1)


def test_hard_cap_kills_busy_but_stuck_process(tmp_path, run_id, fast_heartbeat):
    """A long-running process that never exits is killed by max_seconds and the
    sentinel -1 is returned even though it's burning CPU the whole time."""
    log = tmp_path / "sn.log"
    # Busy CPU spin with NO log output => CPU grows so the idle heuristic never
    # trips; the log stays empty so there's no writer racing the watchdog's
    # append. Only the hard cap can stop this.
    start = time.monotonic()
    rc = ss._run_with_heartbeat(
        ["bash", "-c", "while true; do :; done"],
        log, run_id, max_seconds=1,
    )
    elapsed = time.monotonic() - start
    assert rc == -1
    assert elapsed < 5  # killed promptly near the 1s cap, not left to run
    assert "hard wall-clock cap" in log.read_text(errors="replace")


def test_pure_virtual_signature_fast_fails(tmp_path, run_id, fast_heartbeat):
    """The MCR 'pure virtual method called' abort is recognized as a crash
    signature, so a process that prints it then hangs is killed quickly."""
    assert "pure virtual method called" in ss._CRASH_SIGNATURES
    log = tmp_path / "sn.log"
    rc = ss._run_with_heartbeat(
        ["bash", "-c", "echo 'pure virtual method called'; sleep 30"],
        log, run_id, max_seconds=60,
    )
    assert rc == -1
    text = log.read_text(errors="replace")
    assert "crash signature" in text
    assert "pure virtual method called" in text


def test_clean_exit_returns_zero(tmp_path, run_id, fast_heartbeat):
    """A normal fast process is not touched by the watchdog."""
    log = tmp_path / "ok.log"
    rc = ss._run_with_heartbeat(
        ["bash", "-c", "echo done"], log, run_id, max_seconds=60
    )
    assert rc == 0
    assert "watchdog" not in log.read_text(errors="replace")
