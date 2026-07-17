"""Tests for SeriesDifficulty model and difficulty query functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb import effort_manifest as em
from embryodb.models import MicroscopyMetadata, Series, SeriesDifficulty, Status
from embryodb.queries import datasets as q_datasets
from embryodb.queries.difficulty import (
    get_predictions,
    list_difficulty_series,
    upsert_predictions,
)


def _series(session, name, *, edited="150", date_acquired="20250101", annot_loc=""):
    s = Series(
        series_name=name, edited_timepts=edited,
        date_acquired=date_acquired, annot_loc=annot_loc,
    )
    session.add(s)
    session.flush()
    return s


def _predictions(stage="toEnd", effort=500.0, bucket="moderate",
                 model_version="legacy_v1", predicted_at=None):
    return dict(stage=stage, effort_predicted=effort,
                effort_bucket=bucket, model_version=model_version,
                predicted_at=predicted_at)


# --- SeriesDifficulty ORM ---------------------------------------------------

def test_upsert_creates_rows(db_session):
    _series(db_session, "s1")
    rows = upsert_predictions(db_session, "s1", [
        _predictions("to100", 200.0, "easy"),
        _predictions("toEnd", 800.0, "hard"),
    ])
    db_session.flush()
    assert len(rows) == 2
    assert {r.stage for r in rows} == {"to100", "toEnd"}


def test_upsert_replaces_existing(db_session):
    _series(db_session, "s2")
    upsert_predictions(db_session, "s2", [_predictions("toEnd", 500.0, "moderate")])
    db_session.flush()
    upsert_predictions(db_session, "s2", [_predictions("toEnd", 1200.0, "hard")])
    db_session.flush()
    rows = get_predictions(db_session, "s2")
    toend = [r for r in rows if r.stage == "toEnd"]
    assert len(toend) == 1
    assert toend[0].effort_predicted == pytest.approx(1200.0)
    assert toend[0].effort_bucket == "hard"


def test_upsert_unknown_series_raises(db_session):
    with pytest.raises(KeyError, match="not_here"):
        upsert_predictions(db_session, "not_here", [_predictions()])


def test_get_predictions_ordered(db_session):
    _series(db_session, "s3")
    upsert_predictions(db_session, "s3", [
        _predictions("toEnd"), _predictions("to100"), _predictions("to200"),
    ])
    db_session.flush()
    rows = get_predictions(db_session, "s3")
    assert [r.stage for r in rows] == ["to100", "to200", "toEnd"]


def test_get_predictions_missing_series(db_session):
    assert get_predictions(db_session, "nobody") == []


def test_list_difficulty_filter_bucket(db_session):
    _series(db_session, "easy1")
    _series(db_session, "hard1")
    upsert_predictions(db_session, "easy1", [_predictions("toEnd", 100, "easy")])
    upsert_predictions(db_session, "hard1", [_predictions("toEnd", 2000, "hard")])
    db_session.flush()
    hard_rows = list_difficulty_series(db_session, bucket="hard")
    assert len(hard_rows) == 1 and hard_rows[0].effort_bucket == "hard"


# --- effort-manifest date_from filter ---------------------------------------

def test_date_from_filter(db_session, tmp_path):
    for name, date in [("old", "20240101"), ("new1", "20250601"), ("new2", "20260101")]:
        ann = tmp_path / name
        (ann / "dats").mkdir(parents=True)
        _series(db_session, name, date_acquired=date, annot_loc=str(ann))

    rows_all = em.build_manifest(db_session, trusted_lists=(), stat_zips=False)
    rows_2025 = em.build_manifest(db_session, trusted_lists=(), stat_zips=False,
                                  date_from="20250101")
    names_all = {r.series for r in rows_all}
    names_2025 = {r.series for r in rows_2025}
    assert "old" in names_all
    assert "old" not in names_2025
    assert "new1" in names_2025 and "new2" in names_2025
