"""Tests for the NEW all-MATLAB StarryNite (release_v1) integration:
param-file translation, output landing, effort parsing, engine dispatch, and
the old/new threading through rerun + the run row's params column.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from embryodb.models import PipelineStepRun, RunStatus, Series
from embryodb.pipeline import starrynite_v1 as snv1
from embryodb.pipeline import subprocess_steps

# A minimal legacy matlabParams (detection block + resolution + flags), like
# the file step_write_matlab_params writes.
_MATLAB_PARAMS = """%parameter file for matlab nuclear detection
conservememory=false;
slices=67;
xyres=0.0925;
zres=0.492;
start_time=1;
firsttimestepdiam=80;
firsttimestepnumcells=6;
parameters.staging=[102,181,251,351,500];
parameters.intensitythreshold=[0.009,0.0075,0.008,0.008,0.008,0.008];
parameters.rangethreshold=[84,50,40,40,20,20];
distribution_file='clean_distributions.mat';
downsampling=0.5;
end_time=240;
"""

# A stand-in release template with the fixed TRACKING block after the marker.
_TEMPLATE = """savedata=true;
splitstack=false;
rednuclei=false;
flipstack=false;
zeropadding=true;
parameters.intensitythreshold=[0.016,0.016,0.018,0.030,0.04,0.044];
% ============ TRACKING (retrained Murray classifier) ============
load 'MurrayTrackingModel_20260704.mat';
trackingparameters.starttime=1;
trackingparameters.interval=1;
"""


@pytest.fixture
def release_dir(tmp_path, monkeypatch):
    """A fake release_v1 dir with the param template + effort/predict_effort.py."""
    rel = tmp_path / "release_v1"
    (rel / "effort").mkdir(parents=True)
    (rel / "params_template_newmatlab.txt").write_text(_TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(snv1.settings, "starrynite_v1_dir", rel)
    return rel


# --------------------------------------------------------------------------
# param-file translation
# --------------------------------------------------------------------------


def test_build_newmatlab_paramfile_appends_tracking_and_flags(release_dir, tmp_path):
    mp = tmp_path / "matlabParams"
    mp.write_text(_MATLAB_PARAMS, encoding="utf-8")
    out = snv1.build_newmatlab_paramfile(mp, tmp_path / "params.txt")
    text = out.read_text(encoding="utf-8")

    # Detection block preserved verbatim.
    assert "parameters.staging=[102,181,251,351,500];" in text
    assert "xyres=0.0925;" in text
    # Tracking block appended.
    assert "trackingparameters.starttime=1;" in text
    assert "load 'MurrayTrackingModel_20260704.mat';" in text
    # Missing routing flags added.
    for flag in ("splitstack=false;", "rednuclei=false;", "flipstack=false;", "zeropadding=true;"):
        assert flag in text


def test_build_newmatlab_paramfile_swaps_model_name(release_dir, tmp_path):
    mp = tmp_path / "matlabParams"
    mp.write_text(_MATLAB_PARAMS, encoding="utf-8")
    out = snv1.build_newmatlab_paramfile(
        mp, tmp_path / "params.txt", model_name="OtherModel_v9.mat"
    )
    text = out.read_text(encoding="utf-8")
    assert "load 'OtherModel_v9.mat';" in text
    assert "MurrayTrackingModel_20260704" not in text


def test_build_newmatlab_paramfile_skips_present_flags(release_dir, tmp_path):
    # If the legacy file already sets a flag, we must not duplicate it.
    mp = tmp_path / "matlabParams"
    mp.write_text(_MATLAB_PARAMS + "splitstack=true;\n", encoding="utf-8")
    out = snv1.build_newmatlab_paramfile(mp, tmp_path / "params.txt")
    text = out.read_text(encoding="utf-8")
    assert text.count("splitstack=") == 1
    assert "splitstack=true;" in text


# --------------------------------------------------------------------------
# output landing + discovery
# --------------------------------------------------------------------------


def _write_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("nuclei/t001-nuclei", "x,y,z\n")


def test_land_lineage_copies_into_legacy_slot(tmp_path):
    scratch_out = tmp_path / "out"
    scratch_out.mkdir()
    _write_zip(scratch_out / "SER_L1.zip")
    dats = tmp_path / "dats"
    res = snv1.land_lineage(scratch_out, dats, "SER_L1")
    assert (dats / "SER_L1-edit.zip").exists()
    assert (dats / "SER_L1.zip").exists()
    assert res["edit_zip"].endswith("SER_L1-edit.zip")


def test_land_lineage_no_zip_raises(tmp_path):
    scratch_out = tmp_path / "out"
    scratch_out.mkdir()
    with pytest.raises(FileNotFoundError):
        snv1.land_lineage(scratch_out, tmp_path / "dats", "SER_L1")


def test_find_nuclei_dir_subdir_and_flat(tmp_path):
    out = tmp_path / "out"
    (out / "nuclei").mkdir(parents=True)
    (out / "nuclei" / "t001-nuclei").write_text("x", encoding="utf-8")
    assert snv1.find_nuclei_dir(out) == out / "nuclei"

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "t001-nuclei").write_text("x", encoding="utf-8")
    assert snv1.find_nuclei_dir(flat) == flat

    empty = tmp_path / "empty"
    empty.mkdir()
    assert snv1.find_nuclei_dir(empty) is None


# --------------------------------------------------------------------------
# effort predictor wiring
# --------------------------------------------------------------------------


_FAKE_PAYLOAD = {
    "model_version": "gtfree_v1",
    "model_md5": "0" * 32,
    "effort_events": 123.0,
    "triage": "moderate",
    "triage_q1": 5417.0,
    "triage_q2": 14945.0,
    "stages": [
        {"stage": "to100", "effort_predicted": 12.0, "effort_bucket": "moderate",
         "n_at": 101.0, "t_at": 40.0},
        {"stage": "toEnd", "effort_predicted": 123.0, "effort_bucket": "moderate",
         "n_at": 550.0, "t_at": 240.0},
    ],
}


def test_run_effort_parses_json(release_dir, tmp_path, monkeypatch):
    # Fake predict_effort.py that emits the --json contract.
    (release_dir / "effort" / "predict_effort.py").write_text(
        f"import json; print(json.dumps({_FAKE_PAYLOAD!r}))\n", encoding="utf-8"
    )
    monkeypatch.setattr(snv1.settings, "effort_python", Path(sys.executable))
    nuc = tmp_path / "nuclei"
    nuc.mkdir()
    dats = tmp_path / "dats"
    res = snv1.run_effort(nuc, 0.09, 0.49, dats, "SER_L1")
    assert res["ran"] is True
    assert res["triage"] == "moderate"
    assert res["effort_events"] == 123.0
    assert res["model_version"] == "gtfree_v1"
    assert [st["stage"] for st in res["stages"]] == ["to100", "toEnd"]
    assert (dats / "SER_L1_sn_effort.json").exists()

    # The .txt mirrors what predict_effort.py prints by hand: stage, N_at,
    # t_at, effort.
    report = (dats / "SER_L1_sn_effort.txt").read_text(encoding="utf-8")
    to_end = [ln for ln in report.splitlines() if ln.strip().startswith("toEnd")][0]
    assert to_end.split()[3] == "123.0"


def test_run_effort_rejects_non_json_output(release_dir, tmp_path, monkeypatch):
    (release_dir / "effort" / "predict_effort.py").write_text(
        "print('TRIAGE: curate/moderate')\n", encoding="utf-8"
    )
    monkeypatch.setattr(snv1.settings, "effort_python", Path(sys.executable))
    nuc = tmp_path / "nuclei"
    nuc.mkdir()
    res = snv1.run_effort(nuc, 0.09, 0.49, tmp_path / "dats", "SER_L1")
    assert res["ran"] is False
    assert "JSON" in res["error"]


def test_run_effort_reports_failure(release_dir, tmp_path, monkeypatch):
    (release_dir / "effort" / "predict_effort.py").write_text(
        "import sys; sys.exit(2)\n", encoding="utf-8"
    )
    monkeypatch.setattr(snv1.settings, "effort_python", Path(sys.executable))
    nuc = tmp_path / "nuclei"
    nuc.mkdir()
    res = snv1.run_effort(nuc, 0.09, 0.49, tmp_path / "dats", "SER_L1")
    assert res["ran"] is False


# --------------------------------------------------------------------------
# engine dispatch in step_run_starrynite
# --------------------------------------------------------------------------


def _series(session, name, image_loc):
    s = Series(
        series_name=name, image_loc=str(image_loc), annot_loc=str(image_loc),
        timepts="240",
    )
    session.add(s)
    session.flush()
    return s


def test_step_run_starrynite_dispatches_new(db_session, tmp_path, monkeypatch):
    image_loc = tmp_path / "img"
    image_loc.mkdir()
    s = _series(db_session, "DISP_L1", image_loc)
    run = PipelineStepRun(
        series_id=s.id, step="run_starrynite",
        status=RunStatus.RUNNING, params={"sn_engine": "new"},
    )
    db_session.add(run)
    db_session.flush()
    run_id = run.id
    db_session.commit()

    called = {}

    def fake_new(series_id, series_name, il, rid, log_path):
        called["hit"] = (series_name, rid)

    monkeypatch.setattr(subprocess_steps, "_run_starrynite_new", fake_new)
    subprocess_steps.step_run_starrynite(s.id, "DISP_L1", image_loc, run_id)
    assert called["hit"] == ("DISP_L1", run_id)


def test_step_run_starrynite_default_old_does_not_call_new(db_session, tmp_path, monkeypatch):
    image_loc = tmp_path / "img"
    (image_loc / "tif").mkdir(parents=True)
    s = _series(db_session, "DISP_L2", image_loc)
    run = PipelineStepRun(
        series_id=s.id, step="run_starrynite", status=RunStatus.RUNNING,
    )  # no params -> default old
    db_session.add(run)
    db_session.flush()
    run_id = run.id
    db_session.commit()

    monkeypatch.setattr(
        subprocess_steps, "_run_starrynite_new",
        lambda *a, **k: pytest.fail("new runner must not be called for old engine"),
    )
    # Stub the legacy subprocess so we don't actually launch perl/MATLAB.
    monkeypatch.setattr(subprocess_steps, "_run_with_heartbeat", lambda *a, **k: 0)
    monkeypatch.setattr(subprocess_steps, "_finish_run", lambda *a, **k: None)
    subprocess_steps.step_run_starrynite(s.id, "DISP_L2", image_loc, run_id)


# --------------------------------------------------------------------------
# compute_difficulty step
# --------------------------------------------------------------------------


def _difficulty_series(db_session, name, tmp_path, *, sn_engine=None, with_zip=True):
    image_loc = tmp_path / name
    (image_loc / "dats").mkdir(parents=True)
    if with_zip:
        (image_loc / "dats" / f"{name}.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    s = _series(db_session, name, image_loc)
    sn = PipelineStepRun(series_id=s.id, step="run_starrynite", status=RunStatus.COMPLETE)
    if sn_engine:
        sn.params = {"sn_engine": sn_engine}
    run = PipelineStepRun(series_id=s.id, step="compute_difficulty", status=RunStatus.RUNNING)
    db_session.add_all([sn, run])
    db_session.flush()
    ids = (s.id, run.id)
    db_session.commit()
    return image_loc, ids


def _fake_effort(**over):
    payload = {
        "ran": True, "model_version": "gtfree_v1", "triage": "moderate",
        "effort_events": 123.0, "report_path": None, "json_path": None,
        "stages": [
            {"stage": "to100", "effort_predicted": 12.0, "effort_bucket": "moderate"},
            {"stage": "toEnd", "effort_predicted": 123.0, "effort_bucket": "moderate"},
        ],
    }
    payload.update(over)
    return lambda *a, **k: payload


def test_compute_difficulty_stores_predictions(db_session, tmp_path, monkeypatch):
    image_loc, (sid, run_id) = _difficulty_series(
        db_session, "DIFF_L1", tmp_path, sn_engine="new"
    )
    monkeypatch.setattr(snv1, "run_effort", _fake_effort())
    subprocess_steps.step_compute_difficulty(sid, "DIFF_L1", image_loc, run_id)

    from embryodb.queries import difficulty as q_difficulty

    rows = q_difficulty.get_predictions(db_session, "DIFF_L1")
    assert {r.stage for r in rows} == {"to100", "toEnd"}
    # In-scope run: no out-of-scope tag.
    assert all(r.model_version == "gtfree_v1" for r in rows)
    assert db_session.get(PipelineStepRun, run_id).status == RunStatus.COMPLETE


def test_compute_difficulty_tags_legacy_engine_out_of_scope(
    db_session, tmp_path, monkeypatch
):
    # Default engine is "old" — the model is not calibrated on classic-SN
    # lineages, so its rows must be tagged rather than silently comparable.
    image_loc, (sid, run_id) = _difficulty_series(db_session, "DIFF_L2", tmp_path)
    monkeypatch.setattr(snv1, "run_effort", _fake_effort())
    subprocess_steps.step_compute_difficulty(sid, "DIFF_L2", image_loc, run_id)

    from embryodb.queries import difficulty as q_difficulty

    rows = q_difficulty.get_predictions(db_session, "DIFF_L2")
    assert rows
    assert all(
        r.model_version == "gtfree_v1" + q_difficulty.OUT_OF_SCOPE_SUFFIX
        for r in rows
    )


def test_compute_difficulty_skips_without_raw_lineage(db_session, tmp_path, monkeypatch):
    image_loc, (sid, run_id) = _difficulty_series(
        db_session, "DIFF_L3", tmp_path, with_zip=False
    )
    monkeypatch.setattr(
        snv1, "run_effort",
        lambda *a, **k: pytest.fail("predictor must not run without a lineage"),
    )
    subprocess_steps.step_compute_difficulty(sid, "DIFF_L3", image_loc, run_id)
    # SKIPPED, never FAILED: a failure here would strand run_red_extract and
    # run_measure behind an advisory triage number.
    assert db_session.get(PipelineStepRun, run_id).status == RunStatus.SKIPPED


def test_compute_difficulty_skips_when_predictor_fails(db_session, tmp_path, monkeypatch):
    image_loc, (sid, run_id) = _difficulty_series(db_session, "DIFF_L4", tmp_path)
    monkeypatch.setattr(
        snv1, "run_effort", lambda *a, **k: {"ran": False, "error": "boom"}
    )
    subprocess_steps.step_compute_difficulty(sid, "DIFF_L4", image_loc, run_id)
    row = db_session.get(PipelineStepRun, run_id)
    assert row.status == RunStatus.SKIPPED
    assert "boom" in row.error_excerpt


def test_compute_difficulty_is_a_registered_worker_step():
    from embryodb.pipeline.orchestrate import STEPS
    from embryodb.pipeline.worker import WORKER_STEPS

    # Ordering is load-bearing: the worker's _prerequisite_ok walks WORKER_STEPS,
    # so difficulty must sit after the lineage exists and before extraction.
    assert STEPS.index("run_starrynite") < STEPS.index("compute_difficulty")
    assert STEPS.index("compute_difficulty") < STEPS.index("run_red_extract")
    assert WORKER_STEPS.index("run_starrynite") < WORKER_STEPS.index("compute_difficulty")
    assert WORKER_STEPS.index("compute_difficulty") < WORKER_STEPS.index("run_red_extract")


# --------------------------------------------------------------------------
# params column + rerun threading
# --------------------------------------------------------------------------


def test_pipeline_step_run_params_column_roundtrips(db_session, tmp_path):
    s = _series(db_session, "PARM_L1", tmp_path / "img")
    run = PipelineStepRun(series_id=s.id, step="run_starrynite", params={"sn_engine": "new"})
    db_session.add(run)
    db_session.flush()
    db_session.expire(run)
    assert run.params == {"sn_engine": "new"}


def test_requeue_sets_and_preserves_sn_engine(db_session, tmp_path):
    from embryodb.pipeline.rerun import requeue_series

    s = _series(db_session, "RQ_L1", tmp_path / "img")
    db_session.add(PipelineStepRun(
        series_id=s.id, step="run_starrynite", status=RunStatus.COMPLETE,
    ))
    db_session.flush()

    # Explicit new engine on rerun.
    requeue_series(db_session, ["RQ_L1"], steps=["run_starrynite"], sn_engine="new",
                   refresh_resolution=False)
    run = db_session.query(PipelineStepRun).filter_by(
        series_id=s.id, step="run_starrynite").one()
    assert run.params.get("sn_engine") == "new"
    assert run.status == RunStatus.PENDING

    # Plain rerun (no engine given) must PRESERVE the prior choice.
    requeue_series(db_session, ["RQ_L1"], steps=["run_starrynite"],
                   refresh_resolution=False)
    run = db_session.query(PipelineStepRun).filter_by(
        series_id=s.id, step="run_starrynite").one()
    assert run.params.get("sn_engine") == "new"


def test_requeue_threads_prod_overrides(db_session, tmp_path):
    from embryodb.pipeline.rerun import requeue_series

    s = _series(db_session, "RQP_L1", tmp_path / "img")
    db_session.add(PipelineStepRun(
        series_id=s.id, step="run_starrynite", status=RunStatus.COMPLETE,
    ))
    db_session.flush()

    def sn_run():
        return db_session.query(PipelineStepRun).filter_by(
            series_id=s.id, step="run_starrynite").one()

    requeue_series(
        db_session, ["RQP_L1"], steps=["run_starrynite"], sn_engine="prod",
        prod_overrides={"sn_uniform_threshold": "0.0025"},
        refresh_resolution=False,
    )
    assert sn_run().params == {"sn_engine": "prod", "sn_uniform_threshold": "0.0025"}

    # Re-running with the engine set but no threshold means "global default" --
    # inheriting 0.0025 would run at a value the caller did not ask for.
    requeue_series(db_session, ["RQP_L1"], steps=["run_starrynite"],
                   sn_engine="prod", refresh_resolution=False)
    assert sn_run().params == {"sn_engine": "prod"}


def test_requeue_rejects_bad_prod_override(db_session, tmp_path):
    from embryodb.pipeline.rerun import requeue_series

    _series(db_session, "RQP_L2", tmp_path / "img")
    db_session.flush()
    with pytest.raises(ValueError, match="sn_uniform_threshold"):
        requeue_series(
            db_session, ["RQP_L2"], steps=["run_starrynite"], sn_engine="prod",
            prod_overrides={"sn_uniform_threshold": "dim"},
            refresh_resolution=False,
        )
