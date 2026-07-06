"""Tests for the per-series tracked extract runner (embryodb.pipeline.extract_run).

The point of this module is (1) failure isolation — one embryo's crash must not
stop the others — and (2) visibility — each step records a PipelineStepRun row,
with three deduped steps sharing the import-pipeline slots. We never spawn
java/perl; `_run_step` is monkeypatched to simulate per-step return codes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb import database
from embryodb.models import PipelineStepRun, RunStatus, Series
from embryodb.pipeline import extract_run
from embryodb.pipeline.extract_run import (
    EXTRACT_CATEGORY_COMPONENTS,
    rollup_category,
    run_extract_batch,
    run_extract_for_series,
)

ALL_KEYS = [
    "red_extractor", "red_excel1", "red_excel2", "partial",
    "measure", "align", "update_perms",
]


@pytest.fixture
def fake_steps(monkeypatch, tmp_path):
    """Route the per-series list files to tmp and stub step execution.

    Returns a setter: call `fake_steps({"RedExcel1": 1})` to make any step whose
    invocation contains that substring return rc=1; everything else returns 0.
    """
    monkeypatch.setattr(extract_run.settings, "command_log_dir", str(tmp_path))
    plan: dict[str, int] = {}

    def _fake_run_step(invocation: str, base_dir: Path):
        for needle, rc in plan.items():
            if needle in invocation:
                return rc, f"boom in {needle}\n"
        return 0, "ok\n"

    monkeypatch.setattr(extract_run, "_run_step", _fake_run_step)
    return lambda p: plan.update(p)


def _rows(session, series_id):
    return {
        r.step: r
        for r in session.query(PipelineStepRun).filter(
            PipelineStepRun.series_id == series_id
        )
    }


def test_all_steps_complete_and_dedup(db_session, fake_steps):
    fake_steps({})  # everything succeeds
    s = Series(series_name="acq_L1", annot_loc="/x")
    db_session.add(s)
    db_session.commit()
    sid = s.id

    rc = run_extract_for_series("acq_L1", ALL_KEYS, base_dir=Path("/fake"))
    assert rc == 0

    rows = _rows(db_session, sid)
    # RedExtractor1/Measure1 dedup onto the import slots; the rest get their own.
    for step in ("run_red_extract", "red_excel1", "red_excel2", "partial",
                 "run_measure", "align1", "update_perms"):
        assert rows[step].status == RunStatus.COMPLETE, step
    # No legacy "red_extractor"/"measure" step names leaked through.
    assert "red_extractor" not in rows
    assert "measure" not in rows


def test_partial_failure_halts_series(db_session, fake_steps):
    fake_steps({"embryodb.cli partial": 1})
    s = Series(series_name="acq_L2", annot_loc="/x")
    db_session.add(s)
    db_session.commit()
    sid = s.id

    rc = run_extract_for_series("acq_L2", ALL_KEYS, base_dir=Path("/fake"))
    assert rc == 1

    rows = _rows(db_session, sid)
    # Steps up to and including the failure are recorded...
    assert rows["run_red_extract"].status == RunStatus.COMPLETE
    assert rows["red_excel1"].status == RunStatus.COMPLETE
    assert rows["red_excel2"].status == RunStatus.COMPLETE
    assert rows["partial"].status == RunStatus.FAILED
    assert rows["partial"].error_excerpt
    # ...but downstream steps for this series never run.
    assert "run_measure" not in rows
    assert "align1" not in rows
    assert "update_perms" not in rows


def test_batch_isolates_failures(db_session, fake_steps):
    # First series' Partial crashes; the second must still complete fully.
    fake_steps({"embryodb.cli partial": 1})
    a = Series(series_name="acq_A", annot_loc="/x")
    b = Series(series_name="acq_B", annot_loc="/x")
    db_session.add_all([a, b])
    db_session.commit()
    aid, bid = a.id, b.id

    # Make Partial fail only for acq_A: the fake keys on the invocation string,
    # which carries the series name for the per_series Partial step.
    def _fake_run_step(invocation, base_dir):
        if "embryodb.cli partial" in invocation and "acq_A" in invocation:
            return 1, "boom\n"
        return 0, "ok\n"

    extract_run._run_step = _fake_run_step  # type: ignore[assignment]

    worst = run_extract_batch(["acq_A", "acq_B"], ALL_KEYS, base_dir=Path("/fake"))
    assert worst == 1

    a_rows = _rows(db_session, aid)
    b_rows = _rows(db_session, bid)
    # acq_A halted at Partial; acq_B ran the whole chain despite acq_A's failure.
    assert a_rows["partial"].status == RunStatus.FAILED
    assert "update_perms" not in a_rows
    assert b_rows["partial"].status == RunStatus.COMPLETE
    assert b_rows["update_perms"].status == RunStatus.COMPLETE


def test_series_not_found_returns_2(db_session, fake_steps):
    fake_steps({})
    rc = run_extract_for_series("nope_L9", ALL_KEYS, base_dir=Path("/fake"))
    assert rc == 2


# --- category roll-up ------------------------------------------------------


def test_output_excel_rollup_needs_all_three():
    comps = EXTRACT_CATEGORY_COMPONENTS["output_excel"]
    # All three complete → category complete.
    done = {c: RunStatus.COMPLETE for c in comps}
    assert rollup_category(done, comps) == RunStatus.COMPLETE
    # Partial failed → category failed.
    done["partial"] = RunStatus.FAILED
    assert rollup_category(done, comps) == RunStatus.FAILED


def test_rollup_absent_is_none():
    comps = EXTRACT_CATEGORY_COMPONENTS["align"]
    # No component rows yet → category not started.
    assert rollup_category({}, comps) is None
    # align1 present, getacd reserved/absent → status comes from align1 alone.
    assert rollup_category({"align1": RunStatus.COMPLETE}, comps) == RunStatus.COMPLETE


def test_summarize_runs_shows_categories():
    from embryodb.gui.models import _summarize_runs

    runs = [
        PipelineStepRun(step="run_red_extract", status=RunStatus.COMPLETE),
        PipelineStepRun(step="red_excel1", status=RunStatus.COMPLETE),
        PipelineStepRun(step="red_excel2", status=RunStatus.COMPLETE),
        PipelineStepRun(step="partial", status=RunStatus.FAILED),
        PipelineStepRun(step="update_perms", status=RunStatus.COMPLETE),
    ]
    summary = _summarize_runs(runs)
    # output_excel rolls up to a failure glyph; permissions shows complete.
    assert "xls✗" in summary
    assert "perm✓" in summary
    assert "1!" in summary  # one failed unit flagged in the head
