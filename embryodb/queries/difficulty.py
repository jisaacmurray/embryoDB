"""Query functions for per-series curation-difficulty predictions."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Series, SeriesDifficulty

STAGES = ("to100", "to200", "to350", "to600", "toEnd")

# Appended to a model_version when the model was run on lineages it was NOT
# calibrated on (e.g. the new-SN effort model on a legacy classic-SN lineage,
# where it shows documented negative transfer). Keeps the numbers queryable
# while making them impossible to confuse with in-scope predictions.
OUT_OF_SCOPE_SUFFIX = "_oos"


def preferred_model_version(rows: list[SeriesDifficulty]) -> str | None:
    """Which vintage of a series' difficulty rows to trust for display.

    In-scope vintages beat out-of-scope ones: an ``_oos`` row is a model run on
    a lineage it was not calibrated on, and must never mask a properly
    calibrated number. Within a scope class, the newest prediction wins.
    """
    versions = {r.model_version for r in rows}
    if not versions:
        return None
    in_scope = {v for v in versions if not v.endswith(OUT_OF_SCOPE_SUFFIX)}
    return max(
        in_scope or versions,
        key=lambda mv: max(
            (r.predicted_at for r in rows
             if r.model_version == mv and r.predicted_at is not None),
            default=datetime.min.replace(tzinfo=timezone.utc),
        ),
    )


def upsert_predictions(
    session: Session,
    series_name: str,
    predictions: list[dict],
) -> list[SeriesDifficulty]:
    """Insert or replace difficulty rows for a series.

    ``predictions`` is a list of dicts with keys: stage, effort_predicted,
    effort_bucket, model_version, predicted_at (optional ISO string or datetime).
    Existing rows with the same (series_id, stage, model_version) are deleted
    first — SQLite lacks ON CONFLICT DO UPDATE for multi-column constraints
    without dialect-specific syntax, so we do delete + insert instead.
    """
    s = session.query(Series).filter_by(series_name=series_name).first()
    if s is None:
        raise KeyError(f"series {series_name!r} not in DB")

    out = []
    for p in predictions:
        model_ver = p["model_version"]
        stage = p["stage"]
        # Remove any existing row for this (series, stage, model_version).
        session.execute(
            delete(SeriesDifficulty).where(
                SeriesDifficulty.series_id == s.id,
                SeriesDifficulty.stage == stage,
                SeriesDifficulty.model_version == model_ver,
            )
        )
        pred_at = p.get("predicted_at")
        if isinstance(pred_at, str):
            pred_at = datetime.fromisoformat(pred_at)
        if pred_at is None:
            pred_at = datetime.now(timezone.utc)
        row = SeriesDifficulty(
            series_id=s.id,
            stage=stage,
            effort_predicted=p.get("effort_predicted"),
            effort_bucket=p.get("effort_bucket"),
            model_version=model_ver,
            predicted_at=pred_at,
        )
        session.add(row)
        out.append(row)
    return out


def get_predictions(
    session: Session,
    series_name: str,
    model_version: str | None = None,
) -> list[SeriesDifficulty]:
    """Return a series' difficulty rows for ONE model version, ordered by stage.

    A series can carry rows from several model vintages at once (stage sets
    differ between them), so callers that key by stage would collide if handed
    a mixed list. Defaults to :func:`preferred_model_version`.
    """
    s = session.query(Series).filter_by(series_name=series_name).first()
    if s is None:
        return []
    rows = session.execute(
        select(SeriesDifficulty).where(SeriesDifficulty.series_id == s.id)
    ).scalars().all()
    if not rows:
        return []
    if model_version is None:
        model_version = preferred_model_version(rows)
    rows = [r for r in rows if r.model_version == model_version]
    stage_order = {st: i for i, st in enumerate(STAGES)}
    return sorted(rows, key=lambda r: stage_order.get(r.stage, 99))


def list_difficulty_series(
    session: Session,
    *,
    model_version: str | None = None,
    bucket: str | None = None,
) -> list[SeriesDifficulty]:
    """List difficulty rows, optionally filtered by model_version and/or bucket."""
    q = select(SeriesDifficulty)
    if model_version:
        q = q.where(SeriesDifficulty.model_version == model_version)
    if bucket:
        q = q.where(SeriesDifficulty.effort_bucket == bucket)
    return list(session.execute(q).scalars().all())
