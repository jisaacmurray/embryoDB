"""Tests for the CLI parity launchers added alongside the GUI actions:

  - ``embryodb pipeline rerun``      (shared core: pipeline.rerun.requeue_series)
  - ``embryodb extract``             (wraps external_tools.run_extract)
  - ``embryodb print-trees``         (wraps external_tools.run_print_trees)
  - ``embryodb jobs``                (wraps jobs.list_jobs)
  - ``embryodb launch-acetree``      (wraps external.launch_acetree)

The detached launchers and worker spawn are monkeypatched so nothing real is
spawned; the focus is the DB state changes (rerun) and that each command
routes to the right underlying function with the resolved series list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from embryodb import cli, database
from embryodb.models import (
    MicroscopyMetadata,
    PipelineStepRun,
    RunStatus,
    Series,
    Status,
)
from embryodb.pipeline.orchestrate import STEPS
from embryodb.pipeline.rerun import default_steps, earliest_incomplete_step, requeue_series

runner = CliRunner()


@pytest.fixture
def fresh_db(monkeypatch):
    """A file-backed SQLite DB the CLI subprocess-free commands share.

    The CLI opens its own ``database.session_scope`` rather than using the
    test's ``db_session``, so we point the engine at one in-memory DB and keep
    it alive for the whole test by pinning a module-level engine.
    """
    database.reset_for_tests("sqlite:///:memory:")
    database.create_all()
    yield


def _make_series_with_worker_runs(
    session, name: str, *, starrynite=RunStatus.FAILED, image_loc=None
) -> Series:
    series = Series(
        series_name=name,
        status=Status.NEW,
        image_loc=image_loc or f"/tmp/fake/{name}",
        annot_loc=f"/tmp/fake/{name}",
    )
    session.add(series)
    session.flush()
    inline_steps = STEPS[:7]
    for step in inline_steps:
        session.add(PipelineStepRun(
            series_id=series.id, step=step, status=RunStatus.COMPLETE,
        ))
    session.add(PipelineStepRun(
        series_id=series.id, step="run_starrynite", status=starrynite,
        error_excerpt="boom", log_path="/tmp/old.log",
    ))
    session.add(PipelineStepRun(
        series_id=series.id, step="run_red_extract", status=RunStatus.PENDING,
    ))
    session.add(PipelineStepRun(
        series_id=series.id, step="run_measure", status=RunStatus.PENDING,
    ))
    session.flush()
    return series


# --- shared core: requeue_series --------------------------------------------


def test_requeue_default_steps_resets_failed_starrynite(db_session):
    _make_series_with_worker_runs(db_session, "dv_L1")
    result = requeue_series(db_session, ["dv_L1"])
    # earliest incomplete is run_starrynite → it + downstream reset
    assert result.steps == ["run_starrynite", "run_red_extract", "run_measure"]
    assert result.matched == ["dv_L1"]
    assert not result.missing
    by_step = {
        r.step: r for r in db_session.query(PipelineStepRun).all()
    }
    sn = by_step["run_starrynite"]
    assert sn.status == RunStatus.PENDING
    assert sn.error_excerpt is None
    assert sn.log_path is None


def test_requeue_explicit_step_only_resets_that_step(db_session):
    _make_series_with_worker_runs(
        db_session, "dv_L2", starrynite=RunStatus.COMPLETE
    )
    result = requeue_series(db_session, ["dv_L2"], steps=["run_measure"])
    assert result.steps == ["run_measure"]
    by_step = {r.step: r.status for r in db_session.query(PipelineStepRun).all()}
    assert by_step["run_starrynite"] == RunStatus.COMPLETE
    assert by_step["run_measure"] == RunStatus.PENDING


def test_requeue_reports_missing_series(db_session):
    _make_series_with_worker_runs(db_session, "dv_L3")
    result = requeue_series(db_session, ["dv_L3", "nope_L9"])
    assert result.matched == ["dv_L3"]
    assert result.missing == ["nope_L9"]


def test_requeue_refreshes_resolution_and_applies_overrides(db_session, tmp_path):
    image_loc = tmp_path / "img"
    image_loc.mkdir()
    (image_loc / "matlabParams").write_text(
        "xyres=0.087;\nzres=0.50;\nfirsttimestepdiam=80;\n", encoding="utf-8"
    )
    series = _make_series_with_worker_runs(
        db_session, "dv_L4", image_loc=str(image_loc)
    )
    db_session.add(MicroscopyMetadata(
        series_id=series.id, voxel_xy_um=0.0926, voxel_z_um=0.4923,
    ))
    db_session.flush()

    requeue_series(
        db_session, ["dv_L4"],
        steps=["run_starrynite"],
        overrides={"firsttimestepdiam": "100"},
    )
    text = (image_loc / "matlabParams").read_text(encoding="utf-8")
    assert "xyres=0.0926;" in text
    assert "zres=0.4923;" in text
    assert "firsttimestepdiam=100;" in text


def test_default_steps_and_earliest_helpers():
    assert earliest_incomplete_step(
        {"stage_images": RunStatus.COMPLETE,
         "run_starrynite": RunStatus.COMPLETE,
         "run_red_extract": RunStatus.PENDING}
    ) == "run_red_extract"
    # all complete → default to every worker step EXCEPT stage_images (an
    # explicit redo of the analysis, never a restage)
    allc = {s: RunStatus.COMPLETE for s in
            ("stage_images", "run_starrynite", "run_red_extract", "run_measure")}
    assert default_steps([allc]) == [
        "run_starrynite", "run_red_extract", "run_measure"
    ]


def test_default_steps_never_includes_stage_images():
    """stage_images must never be selected by default, even when it is the
    earliest incomplete step (e.g. a FAILED restage whose TIFFs are already on
    disk). The default falls through to run_starrynite + downstream."""
    # stage_images FAILED, everything else untouched → default starts at
    # run_starrynite, not stage_images.
    runs = {
        "stage_images": RunStatus.FAILED,
        "run_starrynite": RunStatus.PENDING,
        "run_red_extract": RunStatus.PENDING,
        "run_measure": RunStatus.PENDING,
    }
    steps = default_steps([runs])
    assert "stage_images" not in steps
    assert steps == ["run_starrynite", "run_red_extract", "run_measure"]

    # Also when nothing has run yet (all rows absent): still no stage_images.
    assert "stage_images" not in default_steps([{}])


# --- CLI plumbing -----------------------------------------------------------


def test_cli_rerun_resets_and_spawns_worker(fresh_db, monkeypatch):
    with database.session_scope() as s:
        _make_series_with_worker_runs(s, "cli_L1")
    spawned = {"n": 0}
    monkeypatch.setattr(
        "embryodb.pipeline.worker.spawn_worker",
        lambda: spawned.__setitem__("n", spawned["n"] + 1),
    )
    res = runner.invoke(cli.app, ["pipeline", "rerun", "cli_L1"])
    assert res.exit_code == 0, res.output
    assert spawned["n"] == 1
    with database.session_scope() as s:
        sn = (
            s.query(PipelineStepRun)
            .filter_by(step="run_starrynite")
            .one()
        )
        assert sn.status == RunStatus.PENDING


def test_cli_rerun_no_run_does_not_spawn(fresh_db, monkeypatch):
    with database.session_scope() as s:
        _make_series_with_worker_runs(s, "cli_L2")
    spawned = {"n": 0}
    monkeypatch.setattr(
        "embryodb.pipeline.worker.spawn_worker",
        lambda: spawned.__setitem__("n", spawned["n"] + 1),
    )
    res = runner.invoke(cli.app, ["pipeline", "rerun", "cli_L2", "--no-run"])
    assert res.exit_code == 0, res.output
    assert spawned["n"] == 0


def test_cli_rerun_rejects_unknown_step(fresh_db):
    with database.session_scope() as s:
        _make_series_with_worker_runs(s, "cli_L3")
    res = runner.invoke(
        cli.app, ["pipeline", "rerun", "cli_L3", "--step", "bogus"]
    )
    assert res.exit_code == 1
    assert "unknown step" in res.output


def test_cli_extract_routes_to_run_extract(fresh_db, monkeypatch):
    captured = {}

    class FakeProc:
        pid = 4242

    class FakeResult:
        log_path = Path("/tmp/extract.log")
        proc = FakeProc()
        job_id = None

    def fake_run_extract(names, step_keys, **kw):
        captured["names"] = names
        captured["steps"] = step_keys
        return FakeResult()

    monkeypatch.setattr("embryodb.external_tools.run_extract", fake_run_extract)
    res = runner.invoke(
        cli.app, ["extract", "a_L1", "b_L1", "--step", "measure"]
    )
    assert res.exit_code == 0, res.output
    assert captured["names"] == ["a_L1", "b_L1"]
    assert captured["steps"] == ["measure"]


def test_cli_extract_list_steps(fresh_db):
    res = runner.invoke(cli.app, ["extract", "--list-steps"])
    assert res.exit_code == 0
    assert "measure" in res.output


def test_cli_print_trees_routes(fresh_db, monkeypatch):
    captured = {}

    class FakeProc:
        pid = 4243

    class FakeResult:
        log_path = Path("/tmp/trees.log")
        proc = FakeProc()
        job_id = None

    def fake_run_print_trees(names, **kw):
        captured["names"] = names
        captured["kw"] = kw
        return FakeResult()

    monkeypatch.setattr(
        "embryodb.external_tools.run_print_trees", fake_run_print_trees
    )
    res = runner.invoke(cli.app, ["print-trees", "a_L1", "--linewidth", "5"])
    assert res.exit_code == 0, res.output
    assert captured["names"] == ["a_L1"]
    assert captured["kw"]["linewidth"] == 5


def test_cli_extract_requires_step(fresh_db):
    res = runner.invoke(cli.app, ["extract", "a_L1"])
    assert res.exit_code == 1
    assert "--step" in res.output


def test_cli_launch_acetree_unknown_series(fresh_db):
    res = runner.invoke(cli.app, ["launch-acetree", "nope_L1"])
    assert res.exit_code == 1
    assert "no such series" in res.output


def test_cli_launch_acetree_py_unknown_series(fresh_db):
    res = runner.invoke(cli.app, ["launch-acetree-py", "nope_L1"])
    assert res.exit_code == 1
    assert "no such series" in res.output


def test_cli_jobs_empty(fresh_db):
    res = runner.invoke(cli.app, ["jobs"])
    assert res.exit_code == 0
