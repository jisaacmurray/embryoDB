"""Tests for the queued CommandJob path (remote-mode batch launchers).

Covers the three new layers added so remote clients route extract / print-trees
/ getacd through the DB queue a penticton worker drains:

1. The pure command builders render byte-identical shell whether reached
   directly (local launch) or via ``build_command_for_kind`` (worker).
2. ``run_*`` launchers enqueue a PENDING row (no local spawn) in remote mode.
3. ``_claim_next_command`` atomically flips PENDING→RUNNING exactly once.
4. ``jobs.discover_command_jobs`` surfaces the rows in the Background panel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb import database, external_tools, jobs
from embryodb.models import CommandJob, RunStatus
from embryodb.pipeline import worker


@pytest.fixture
def fresh_db(monkeypatch):
    database.reset_for_tests("sqlite:///:memory:")
    database.create_all()
    # Keep enqueue's log path deterministic + inside tmp-free space.
    return database.session_scope


# --- 1. builder parity ----------------------------------------------------


def test_build_command_for_kind_matches_direct_builders(tmp_path):
    base = Path("/fake/tools3")
    lst = tmp_path / "x.list"
    names = ["sA", "sB"]

    direct_extract = external_tools.build_extract_command(
        names, ["measure", "align"], lst, base
    )
    via_extract = external_tools.build_command_for_kind(
        "extract",
        {"series_names": names, "step_keys": ["measure", "align"]},
        lst,
        base,
    )
    assert direct_extract == via_extract

    direct_trees = external_tools.build_print_trees_command(
        names, lst, min_expr=0, max_expr=5000, color_scheme="rainbow",
        linewidth=3, base_dir=base,
    )
    via_trees = external_tools.build_command_for_kind(
        "print_trees",
        {
            "series_names": names,
            "min_expr": 0,
            "max_expr": 5000,
            "color_scheme": "rainbow",
            "linewidth": 3,
        },
        lst,
        base,
    )
    assert direct_trees == via_trees

    direct_acd = external_tools.build_getacd_command(names, lst, base)
    via_acd = external_tools.build_command_for_kind(
        "getacd", {"series_names": names}, lst, base
    )
    assert direct_acd == via_acd


def test_build_command_for_kind_rejects_unknown(tmp_path):
    with pytest.raises(ValueError, match="unknown command-job kind"):
        external_tools.build_command_for_kind("nope", {}, tmp_path / "l", Path("/t"))


# --- 2. enqueue + remote gate --------------------------------------------


def test_enqueue_command_job_creates_pending_row(fresh_db):
    result = external_tools.enqueue_command_job(
        "getacd", {"series_names": ["sA", "sB"]}
    )
    assert result.proc is None
    assert result.job_id is not None
    assert result.log_path == external_tools.command_job_log_path(result.job_id)

    with database.session_scope() as s:
        job = s.get(CommandJob, result.job_id)
        assert job.kind == "getacd"
        assert job.status == RunStatus.PENDING
        assert job.params["series_names"] == ["sA", "sB"]
        assert job.log_path == str(result.log_path)


def test_run_extract_remote_enqueues_instead_of_spawning(fresh_db, monkeypatch):
    def _boom(*a, **k):  # any local spawn attempt is a bug in remote mode
        raise AssertionError("remote mode must not spawn a local subprocess")

    monkeypatch.setattr(external_tools.subprocess, "Popen", _boom)
    monkeypatch.setattr(external_tools.settings, "remote", True)

    result = external_tools.run_extract(["sA"], ["measure"])
    assert result.proc is None
    assert result.job_id is not None
    with database.session_scope() as s:
        job = s.get(CommandJob, result.job_id)
        assert job.kind == "extract"
        assert job.params["step_keys"] == ["measure"]


# --- 3. atomic claim ------------------------------------------------------


def test_claim_next_command_is_atomic_and_single_winner(fresh_db):
    external_tools.enqueue_command_job("getacd", {"series_names": ["sA"]})

    with database.session_scope() as s:
        first = worker._claim_next_command(s)
        assert first is not None
        job_id, kind, params, log_path = first
        assert kind == "getacd"
        row = s.get(CommandJob, job_id)
        assert row.status == RunStatus.RUNNING
        assert row.claimed_by is not None
        # No second runnable job remains.
        assert worker._claim_next_command(s) is None


def test_reset_stale_commands_requeues_crashed(fresh_db):
    from datetime import datetime, timedelta, timezone

    external_tools.enqueue_command_job("getacd", {"series_names": ["sA"]})
    with database.session_scope() as s:
        job_id, *_ = worker._claim_next_command(s)
        row = s.get(CommandJob, job_id)
        row.heartbeat_at = datetime.now(tz=timezone.utc) - timedelta(minutes=10)

    with database.session_scope() as s:
        assert worker._reset_stale_commands(s) == 1
        row = s.get(CommandJob, job_id)
        assert row.status == RunStatus.PENDING
        assert row.claimed_by is None


# --- 4. job-panel discovery ----------------------------------------------


def test_discover_command_jobs_surfaces_rows(fresh_db):
    res = external_tools.enqueue_command_job(
        "print_trees", {"series_names": ["sA", "sB", "sC"]}
    )
    rows = jobs.discover_command_jobs(database.session_scope)
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "command"
    assert row.kind == "Print trees"
    assert f"#{res.job_id}" in row.name
    assert "3 series" in row.name
    assert row.status_label == "queued"
    assert row.running is False

    # list_jobs merges it alongside the other sources.
    all_rows = jobs.list_jobs(database.session_scope)
    assert any(r.source == "command" for r in all_rows)
