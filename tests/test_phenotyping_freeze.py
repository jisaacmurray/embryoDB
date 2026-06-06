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
