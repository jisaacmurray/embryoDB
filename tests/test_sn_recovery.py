"""Tests for StarryNite stub annotation + detection-collapse recovery."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from embryodb import cli, database
from embryodb.models import MicroscopyMetadata, PipelineStepRun, RunStatus, Series
from embryodb.pipeline import sn_recovery as snr
from embryodb.pipeline import stub_annotation as stub

runner = CliRunner()


@pytest.fixture
def fresh_db():
    """Persistent (module-engine) in-memory DB for CLI tests, which open their
    own sessions."""
    database.reset_for_tests("sqlite:///:memory:")
    database.create_all()
    yield


# --- collapse analyzer ------------------------------------------------------


def test_analyze_counts_detects_mid_movie_collapse():
    # peaks early, then falls and stays low (the dim/drift signature)
    counts = {1: 55, 2: 39, 3: 23, 4: 19, 5: 15, 6: 16, 7: 13, 8: 10,
              9: 5, 10: 5, 11: 4, 12: 2}
    rep = snr.analyze_counts(counts)
    assert rep.collapsed is True
    assert rep.peak == 55
    assert rep.total_timepoints == 12
    # tp7=13 is below 0.25*55≈13.75 but > MIN_TRACEABLE(10), so still usable;
    # the low run begins at tp8 (=10) → K = 7
    assert rep.last_usable_timepoint == 7


def test_analyze_counts_more_than_ten_nuclei_never_low():
    # counts dip below 25% of a high peak but stay above 10 → no collapse
    counts = {1: 100, 2: 100, 3: 100}
    counts.update({i: 12 for i in range(4, 12)})  # 12 < 25 but > 10
    rep = snr.analyze_counts(counts)
    assert rep.collapsed is False
    assert rep.last_usable_timepoint == 11


def test_analyze_counts_ignores_legitimate_early_few_cell_stage():
    # a healthy movie ramps up from the 1-cell stage; never collapses
    counts = {i: min(300, i * 6) for i in range(1, 60)}
    rep = snr.analyze_counts(counts)
    assert rep.collapsed is False
    assert rep.last_usable_timepoint == 59  # full range when no collapse


def test_analyze_counts_late_collapse_yields_long_prefix():
    counts = {i: 250 for i in range(1, 180)}
    counts.update({i: 2 for i in range(180, 241)})  # drift out at tp180
    rep = snr.analyze_counts(counts)
    assert rep.collapsed is True
    assert rep.last_usable_timepoint == 179
    assert rep.total_timepoints == 240


def test_analyze_counts_empty():
    rep = snr.analyze_counts({})
    assert rep.collapsed is False
    assert rep.last_usable_timepoint == 0


def test_count_detected_nuclei_reads_lines_and_ignores_diams(tmp_path):
    d = tmp_path / "s_matlabnuclei"
    d.mkdir()
    (d / "matlabnuclei1").write_text("a\nb\nc\n")
    (d / "matlabnuclei2").write_text("x\n\ny\n")  # blank line skipped
    (d / "matlabdiams1").write_text("ignored\nignored\n")
    (d / "matlabnuclei10").write_text("")
    counts = snr.count_detected_nuclei(d)
    assert counts == {1: 3, 2: 2, 10: 0}


# --- stub annotation --------------------------------------------------------


def test_write_stub_nuclei_zip_has_single_timepoint_row(tmp_path):
    zp = stub.write_stub_nuclei_zip(tmp_path / "x.zip")
    with zipfile.ZipFile(zp) as zf:
        assert zf.namelist() == ["nuclei/t001-nuclei"]
        row = zf.read("nuclei/t001-nuclei").decode().strip()
    fields = row.split(",")
    assert fields[0].strip() == "1"  # index
    assert fields[9].strip() == "polar1"  # name
    assert len(fields) >= 20


def _series_with_image_loc(session, name, image_loc, *, timepts="120"):
    s = Series(
        series_name=name, image_loc=str(image_loc), annot_loc=str(image_loc),
        timepts=timepts,
    )
    session.add(s)
    session.flush()
    return s


def test_write_stub_for_series_writes_zips_and_config(db_session, tmp_path):
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    s = _series_with_image_loc(db_session, "stub_L1", image_loc, timepts="200")
    db_session.add(MicroscopyMetadata(
        series_id=s.id, voxel_xy_um=0.09, voxel_z_um=0.5, planes_per_volume=30,
    ))
    db_session.flush()

    rep = stub.write_stub_for_series(s)
    assert rep.n_timepoints == 200
    assert rep.zip_path.exists()
    assert rep.edit_zip_path.exists()
    assert rep.config_path.exists()
    assert s.acetree_config == f"{s.series_name}.xml"
    xml = rep.config_path.read_text()
    assert f"{s.series_name}-edit.zip" in xml
    assert '<end index="200"/>' in xml


# --- recover_series ---------------------------------------------------------


def _write_counts(image_loc: Path, series_name: str, counts: dict[int, int]):
    """Write matlabnuclei<N> (+ matlabdiams<N>) with `c` synthetic nuclei each."""
    d = image_loc / "tif" / f"{series_name}_matlabnuclei"
    d.mkdir(parents=True, exist_ok=True)
    for tp, c in counts.items():
        nuc = "".join(f"{10 + i} \t {20 + i} \t {12} \n" for i in range(c))
        diam = "".join(f"{66} \n" for _ in range(c))
        (d / f"matlabnuclei{tp}").write_text(nuc)
        (d / f"matlabdiams{tp}").write_text(diam)


def test_build_detection_nuclei_zip_passthrough(tmp_path):
    d = tmp_path / "s_matlabnuclei"
    d.mkdir()
    (d / "matlabnuclei1").write_text("312 \t 406 \t 12 \n560 \t 218 \t 14 \n")
    (d / "matlabdiams1").write_text("66 \n62 \n")
    n = stub.build_detection_nuclei_zip(tmp_path / "det.zip", d, 3)
    assert n == 2
    with zipfile.ZipFile(tmp_path / "det.zip") as zf:
        assert zf.namelist() == [
            "nuclei/t001-nuclei", "nuclei/t002-nuclei", "nuclei/t003-nuclei"
        ]
        rows = zf.read("nuclei/t001-nuclei").decode().splitlines()
        assert zf.read("nuclei/t002-nuclei") == b""  # missing tp written empty
    f0 = [p.strip() for p in rows[0].split(",")]
    # index, valid, pred, succ1, succ2, x, y, z, diam, name
    assert f0[:10] == ["1", "1", "-1", "-1", "-1", "312", "406", "12.0", "66", "Nuc1"]
    assert rows[1].split(",")[5].strip() == "560"


def test_recover_auto_short_prefix_writes_stub(db_session, tmp_path):
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    s = _series_with_image_loc(db_session, "rec_L1", image_loc)
    # collapses at ~tp6 → prefix too short → stub
    counts = {1: 55, 2: 39, 3: 23, 4: 19, 5: 15, 6: 13, 7: 4, 8: 2, 9: 2, 10: 2}
    _write_counts(image_loc, "rec_L1", counts)

    res = snr.recover_series(db_session, "rec_L1", action="auto")
    assert res.action == "stub"
    assert (image_loc / "dats" / "rec_L1-edit.zip").exists()
    # detection output exists → stub is built from the real nuclei, not a fake
    with zipfile.ZipFile(image_loc / "dats" / "rec_L1-edit.zip") as zf:
        t1 = zf.read("nuclei/t001-nuclei").decode().splitlines()
    assert len(t1) == 55  # the real tp1 detection count
    assert "Nuc1" in t1[0]


def test_recover_auto_long_prefix_truncates(db_session, tmp_path):
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    (image_loc / "matlabParams").write_text("xyres=0.087;\nzres=0.5;\nend_time=240;\n")
    s = _series_with_image_loc(db_session, "rec_L2", image_loc, timepts="240")
    db_session.add(PipelineStepRun(
        series_id=s.id, step="run_starrynite", status=RunStatus.FAILED,
        error_excerpt="boom",
    ))
    db_session.flush()
    counts = {i: 250 for i in range(1, 180)}
    counts.update({i: 2 for i in range(180, 241)})
    _write_counts(image_loc, "rec_L2", counts)

    res = snr.recover_series(db_session, "rec_L2", action="auto")
    assert res.action == "truncate"
    assert res.last_usable_timepoint == 179
    params = (image_loc / "matlabParams").read_text()
    assert "end_time=179;" in params
    sn = (
        db_session.query(PipelineStepRun)
        .filter_by(series_id=s.id, step="run_starrynite")
        .one()
    )
    assert sn.status == RunStatus.PENDING


def test_recover_stub_forced_even_with_long_prefix(db_session, tmp_path):
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    s = _series_with_image_loc(db_session, "rec_L3", image_loc, timepts="240")
    counts = {i: 250 for i in range(1, 180)}
    counts.update({i: 2 for i in range(180, 241)})
    _write_counts(image_loc, "rec_L3", counts)

    res = snr.recover_series(db_session, "rec_L3", action="stub")
    assert res.action == "stub"
    assert (image_loc / "dats" / "rec_L3.zip").exists()


# --- auto_recover (worker-side automatic recovery) --------------------------


def test_auto_recover_no_collapse_returns_none(db_session, tmp_path):
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    _series_with_image_loc(db_session, "auto_L0", image_loc, timepts="60")
    # healthy ramp-up — never collapses
    _write_counts(image_loc, "auto_L0", {i: min(200, i * 6) for i in range(1, 60)})

    res = snr.auto_recover(db_session, "auto_L0")
    assert res is None


def test_auto_recover_short_prefix_writes_stub(db_session, tmp_path):
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    s = _series_with_image_loc(db_session, "auto_L1", image_loc)
    counts = {1: 55, 2: 39, 3: 23, 4: 19, 5: 15, 6: 13, 7: 4, 8: 2, 9: 2, 10: 2}
    _write_counts(image_loc, "auto_L1", counts)

    res = snr.auto_recover(db_session, "auto_L1")
    assert res.action == "stub"
    assert (image_loc / "dats" / "auto_L1-edit.zip").exists()


def test_auto_recover_long_prefix_truncates_and_requeues(db_session, tmp_path):
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    (image_loc / "matlabParams").write_text("xyres=0.087;\nzres=0.5;\nend_time=240;\n")
    s = _series_with_image_loc(db_session, "auto_L2", image_loc, timepts="240")
    db_session.add(PipelineStepRun(
        series_id=s.id, step="run_starrynite", status=RunStatus.RUNNING,
    ))
    db_session.flush()
    counts = {i: 250 for i in range(1, 180)}
    counts.update({i: 2 for i in range(180, 241)})
    _write_counts(image_loc, "auto_L2", counts)

    res = snr.auto_recover(db_session, "auto_L2")
    assert res.action == "truncate"
    assert res.last_usable_timepoint == 179
    assert "end_time=179;" in (image_loc / "matlabParams").read_text()
    sn = (
        db_session.query(PipelineStepRun)
        .filter_by(series_id=s.id, step="run_starrynite")
        .one()
    )
    assert sn.status == RunStatus.PENDING


def test_auto_recover_end_time_guard_prevents_reloop(db_session, tmp_path):
    """After a prior truncate to end_time=179, stale matlabnuclei files at
    180.. still read as a collapse, but the end_time ceiling makes K=179 not a
    meaningful shortening (179 > 179 - MIN_TRUNCATE_GAIN), so we stub instead of
    re-truncating forever."""
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    (image_loc / "matlabParams").write_text("end_time=179;\n")
    _series_with_image_loc(db_session, "auto_L3", image_loc, timepts="240")
    counts = {i: 250 for i in range(1, 180)}
    counts.update({i: 2 for i in range(180, 241)})  # stale low tail on disk
    _write_counts(image_loc, "auto_L3", counts)

    res = snr.auto_recover(db_session, "auto_L3")
    assert res.action == "stub"


# --- CLI plumbing -----------------------------------------------------------


def test_cli_stub_writes_annotation(fresh_db, tmp_path):
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    with database.session_scope() as s:
        _series_with_image_loc(s, "cli_stub_L1", image_loc, timepts="50")
    res = runner.invoke(cli.app, ["pipeline", "stub", "cli_stub_L1"])
    assert res.exit_code == 0, res.output
    assert (image_loc / "dats" / "cli_stub_L1-edit.zip").exists()


def test_cli_recover_auto_short_prefix_stubs(fresh_db, tmp_path, monkeypatch):
    image_loc = tmp_path / "img"
    (image_loc / "dats").mkdir(parents=True)
    with database.session_scope() as s:
        _series_with_image_loc(s, "cli_rec_L1", image_loc)
    counts = {1: 55, 2: 30, 3: 20, 4: 13, 5: 4, 6: 2, 7: 2, 8: 2}
    _write_counts(image_loc, "cli_rec_L1", counts)
    monkeypatch.setattr("embryodb.pipeline.worker.spawn_worker", lambda: None)
    res = runner.invoke(cli.app, ["pipeline", "recover", "cli_rec_L1"])
    assert res.exit_code == 0, res.output
    assert "stub" in res.output
    assert (image_loc / "dats" / "cli_rec_L1-edit.zip").exists()
