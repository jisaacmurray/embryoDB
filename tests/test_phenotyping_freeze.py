"""Tests for the LineagePhenotyping freeze (embryodb/phenotyping/freeze.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb.models import Dataset, Series, VolumeTimestamp
from embryodb.phenotyping import REFERENCE_SERIES, freeze_dataset
from embryodb.phenotyping.freeze import default_output_base
from embryodb.queries import datasets as q_datasets


def _make_series(session, name: str, annot_loc: Path, kinds=("CD", "ACD", "TIME")) -> Series:
    """Create a Series with a dats/ dir containing the requested file kinds."""
    dats = annot_loc / "dats"
    dats.mkdir(parents=True, exist_ok=True)
    for kind in kinds:
        (dats / f"{kind}{name}.csv").write_text(f"{kind} data for {name}\n", encoding="utf-8")
    s = Series(series_name=name, annot_loc=str(annot_loc))
    session.add(s)
    session.flush()
    return s


def _make_dataset(session, name: str, series_names: list[str]) -> Dataset:
    return q_datasets.create(session, name, series_names=series_names)


@pytest.fixture
def reference_series(db_session, tmp_path):
    """The Sulston reference, always available in the DB."""
    return _make_series(db_session, REFERENCE_SERIES, tmp_path / REFERENCE_SERIES)


def test_freeze_copies_csvs_and_emits_artifacts(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_mut_L1", tmp_path / "mut1")
    _make_series(db_session, "20240102_mut_L2", tmp_path / "mut2")
    _make_dataset(db_session, "mutds", ["20240101_mut_L1", "20240102_mut_L2"])

    out = tmp_path / "out"
    report = freeze_dataset(db_session, "mutds", output_base=out)

    target = out / "mutds"
    assert report.target_dir == target
    assert target.is_dir()
    # CSVs from both series + the reference landed at the top level.
    assert (target / "CD20240101_mut_L1.csv").exists()
    assert (target / "ACD20240102_mut_L2.csv").exists()
    assert (target / f"CD{REFERENCE_SERIES}.csv").exists()
    # Config, list, report emitted.
    assert report.config_path == target / "configs" / "mutds.yaml"
    assert report.config_path.exists()
    assert report.list_path == target / "mutds.list"
    assert report.list_path.exists()
    assert report.report_path.exists()
    assert report.reference_included is True


def test_config_points_into_freeze_tree(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_mut_L1", tmp_path / "mut1")
    _make_dataset(db_session, "mutds", ["20240101_mut_L1"])

    out = tmp_path / "out"
    report = freeze_dataset(db_session, "mutds", output_base=out)
    text = report.config_path.read_text()
    target = out / "mutds"
    assert f'data_dir: "{target}"' in text
    assert f'output_dir: "{target / "mutds"}"' in text
    assert "name: \"mutds\"" in text


def test_reference_series_included_even_if_not_member(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_mut_L1", tmp_path / "mut1")
    _make_dataset(db_session, "mutds", ["20240101_mut_L1"])

    report = freeze_dataset(db_session, "mutds", output_base=tmp_path / "out")
    names = {s.series_name for s in report.series}
    assert REFERENCE_SERIES in names
    assert (report.target_dir / f"CD{REFERENCE_SERIES}.csv").exists()


def test_missing_time_uses_minutes_per_timepoint(db_session, tmp_path, reference_series):
    # Series without a TIME file and without DB timestamps.
    _make_series(db_session, "20240101_notime", tmp_path / "notime", kinds=("CD", "ACD"))
    _make_dataset(db_session, "mutds", ["20240101_notime"])

    report = freeze_dataset(
        db_session, "mutds", output_base=tmp_path / "out", minutes_per_timepoint=1.5
    )
    assert report.minutes_per_timepoint == 1.5
    assert "minutes_per_timepoint: 1.5" in report.config_path.read_text()
    sf = next(s for s in report.series if s.series_name == "20240101_notime")
    assert sf.has_time is False
    assert any("no TIME file" in w for w in sf.warnings)


def test_missing_time_regenerated_from_db_timestamps(db_session, tmp_path, reference_series):
    s = _make_series(db_session, "20240101_db", tmp_path / "db", kinds=("CD", "ACD"))
    s.volume_timestamps = [
        VolumeTimestamp(timepoint=1, absolute_seconds=0, delta_seconds=0),
        VolumeTimestamp(timepoint=2, absolute_seconds=90, delta_seconds=90),
        VolumeTimestamp(timepoint=3, absolute_seconds=180, delta_seconds=90),
    ]
    db_session.flush()
    _make_dataset(db_session, "mutds", ["20240101_db"])

    report = freeze_dataset(db_session, "mutds", output_base=tmp_path / "out")
    time_file = report.target_dir / "TIME20240101_db.csv"
    assert time_file.exists()
    assert "20240101_db\t1\t0\t0" in time_file.read_text()
    sf = next(s for s in report.series if s.series_name == "20240101_db")
    assert sf.has_time is True
    assert sf.time_from_db is True
    # 90s deltas -> 1.5 min inferred fallback for the YAML.
    assert report.minutes_per_timepoint == 1.5


def test_missing_cd_is_hard_error_for_series(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_nocd", tmp_path / "nocd", kinds=("ACD", "TIME"))
    _make_dataset(db_session, "mutds", ["20240101_nocd"])

    report = freeze_dataset(db_session, "mutds", output_base=tmp_path / "out")
    sf = next(s for s in report.series if s.series_name == "20240101_nocd")
    assert sf.skipped is True
    assert "CD" in sf.error
    assert sf.copied == []
    assert not (report.target_dir / "ACD20240101_nocd.csv").exists()


def test_missing_acd_warns(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_noacd", tmp_path / "noacd", kinds=("CD", "TIME"))
    _make_dataset(db_session, "mutds", ["20240101_noacd"])

    report = freeze_dataset(db_session, "mutds", output_base=tmp_path / "out")
    sf = next(s for s in report.series if s.series_name == "20240101_noacd")
    assert sf.skipped is False
    assert any("ACD" in w for w in sf.warnings)


def test_unknown_dataset_raises(db_session, tmp_path):
    with pytest.raises(q_datasets.DatasetError):
        freeze_dataset(db_session, "nope", output_base=tmp_path / "out")


def test_default_output_base_is_user_specific():
    base = default_output_base("alice")
    assert base == Path("/murrlab3/alice/phenotyping")


# --- expression (CA) file resolution ---------------------------------------

_SCHEMA = "cellTime,cell,time,none,global,local,blot,cross,z,x,y,size,gweight"


def _write_cd(path: Path, rows: list[tuple[str, int, float]]) -> None:
    """Write a minimal per-timepoint CD file: rows of (cell, time, blot)."""
    lines = [_SCHEMA]
    for cell, t, blot in rows:
        lines.append(f"{cell}:{t},{cell},{t},0,0,0,{blot},0,0,0,0,0,0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_ca(path: Path, rows: list[tuple[str, float]]) -> None:
    """Write a one-row-per-cell CA file: rows of (cell, blot)."""
    lines = [_SCHEMA]
    for cell, blot in rows:
        lines.append(f"{cell}:1,{cell},1,0,0,0,{blot},0,0,0,0,0,0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_ca_blot(path: Path) -> dict[str, str]:
    import csv

    with path.open(newline="") as f:
        return {r["cell"]: r["blot"] for r in csv.DictReader(f)}


def test_expression_file_passthrough_when_already_ca(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_mut_L1", tmp_path / "mut1")
    _make_dataset(db_session, "mutds", ["20240101_mut_L1"])
    ca = tmp_path / "myexpr.csv"
    _write_ca(ca, [("ABal", 100), ("ABar", 200)])

    report = freeze_dataset(
        db_session, "mutds", output_base=tmp_path / "out", expression_file=ca
    )
    assert report.expression_path == report.target_dir / "expression.csv"
    # One-row-per-cell input is copied verbatim.
    assert report.expression_path.read_text() == ca.read_text()
    assert 'expression_file: "expression.csv"' in report.config_path.read_text()


def test_expression_file_cd_collapsed_to_truncated_mean(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_mut_L1", tmp_path / "mut1")
    _make_dataset(db_session, "mutds", ["20240101_mut_L1"])
    cd = tmp_path / "CDsrc.csv"
    # ABal mean = (10+21)/2 = 15.5 -> trunc 15; ABar single value 7 -> 7.
    _write_cd(cd, [("ABal", 1, 10), ("ABal", 2, 21), ("ABar", 1, 7)])

    report = freeze_dataset(
        db_session, "mutds", output_base=tmp_path / "out", expression_file=cd
    )
    blot = _read_ca_blot(report.expression_path)
    assert blot == {"ABal": "15", "ABar": "7"}
    assert "collapsed to CA" in report.expression_source


def test_expression_series_uses_series_ca(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_mut_L1", tmp_path / "mut1")
    _make_dataset(db_session, "mutds", ["20240101_mut_L1"])
    # A separate series whose CA we want to borrow.
    src = _make_series(db_session, "20240301_reporter", tmp_path / "rep", kinds=("CD",))
    _write_ca(Path(src.annot_loc) / "dats" / "CA20240301_reporter.csv",
              [("ABal", 50), ("ABar", 60)])

    report = freeze_dataset(
        db_session, "mutds", output_base=tmp_path / "out",
        expression_series="20240301_reporter",
    )
    blot = _read_ca_blot(report.expression_path)
    assert blot == {"ABal": "50", "ABar": "60"}
    assert "CA20240301_reporter.csv" in report.expression_source


def test_expression_series_generates_from_cd_when_no_ca(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_mut_L1", tmp_path / "mut1")
    _make_dataset(db_session, "mutds", ["20240101_mut_L1"])
    src = _make_series(db_session, "20240301_reporter", tmp_path / "rep", kinds=())
    _write_cd(Path(src.annot_loc) / "dats" / "CD20240301_reporter.csv",
              [("ABal", 1, 4), ("ABal", 2, 9)])  # mean 6.5 -> 6

    report = freeze_dataset(
        db_session, "mutds", output_base=tmp_path / "out",
        expression_series="20240301_reporter",
    )
    assert _read_ca_blot(report.expression_path) == {"ABal": "6"}
    assert "generated from CD20240301_reporter.csv" in report.expression_source


def test_no_expression_leaves_config_unset(db_session, tmp_path, reference_series):
    _make_series(db_session, "20240101_mut_L1", tmp_path / "mut1")
    _make_dataset(db_session, "mutds", ["20240101_mut_L1"])

    report = freeze_dataset(db_session, "mutds", output_base=tmp_path / "out")
    assert report.expression_path is None
    assert "expression_file:" not in report.config_path.read_text()
