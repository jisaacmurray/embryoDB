"""Query helpers for Series.

All read/filter logic lives here so the GUI, CLI, and (future) FastAPI all
route through the same functions. Keeps the eventual REST tier a thin wrapper.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import distinct, func, or_, select
from sqlalchemy.orm import Session

from ..models import Series, Status, dataset_series_table


def list_series(
    session: Session,
    *,
    person: Iterable[str] | None = None,
    strain: Iterable[str] | None = None,
    treatments: Iterable[str] | None = None,
    edited_by: Iterable[str] | None = None,
    reporter_gene: Iterable[str] | None = None,
    status: Iterable[Status] | None = None,
    date_before: str | None = None,
    date_after: str | None = None,
    text: str | None = None,
    text_in_comments_only: bool = True,
    dataset_id: int | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Series]:
    """Apply the 8-filter chain. Each filter is independent; multiple values
    within one filter are OR'd; filters compose with AND.

    `dataset_id`, when supplied, additionally restricts the result to
    members of that Dataset (JOIN through the dataset_series association).
    """
    stmt = select(Series)
    if dataset_id is not None:
        stmt = stmt.join(
            dataset_series_table,
            dataset_series_table.c.series_id == Series.id,
        ).where(dataset_series_table.c.dataset_id == dataset_id)
    if person:
        stmt = stmt.where(Series.person.in_(list(person)))
    if strain:
        stmt = stmt.where(Series.strain_name.in_(list(strain)))
    if treatments:
        stmt = stmt.where(Series.treatments.in_(list(treatments)))
    if edited_by:
        stmt = stmt.where(Series.edited_by.in_(list(edited_by)))
    if reporter_gene:
        stmt = stmt.where(Series.reporter_gene.in_(list(reporter_gene)))
    if status:
        stmt = stmt.where(Series.status.in_(list(status)))
    if date_before:
        stmt = stmt.where(Series.date_acquired < date_before)
    if date_after:
        stmt = stmt.where(Series.date_acquired > date_after)
    if text:
        like = f"%{text}%"
        if text_in_comments_only:
            stmt = stmt.where(Series.comments.ilike(like))
        else:
            stmt = stmt.where(
                or_(
                    Series.comments.ilike(like),
                    Series.series_name.ilike(like),
                    Series.person.ilike(like),
                    Series.strain_name.ilike(like),
                    Series.treatments.ilike(like),
                    Series.reporter_gene.ilike(like),
                    Series.edited_by.ilike(like),
                    Series.partial_editing_code.ilike(like),
                )
            )
    # Default sort: most recent acquisition first, then series_name as a
    # stable tiebreaker. The Qt browser can reorder client-side after fetch.
    stmt = stmt.order_by(Series.date_acquired.desc(), Series.series_name.asc()).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())


def distinct_values(session: Session, column: str) -> list[str]:
    """Distinct non-empty values of one column, sorted. Used to populate filter
    picklists and auto-complete combos. Whitelist of columns we expose."""
    allowed = {
        "person",
        "strain_name",
        "treatments",
        "edited_by",
        "reporter_gene",
    }
    if column not in allowed:
        raise ValueError(f"column {column!r} is not exposed for distinct queries")
    col = getattr(Series, column)
    stmt = select(distinct(col)).where(col != "").order_by(col)
    return [v for (v,) in session.execute(stmt)]


def get_by_name(session: Session, series_name: str) -> Series | None:
    return session.execute(
        select(Series).where(Series.series_name == series_name)
    ).scalar_one_or_none()


def count(session: Session) -> int:
    return session.execute(select(func.count(Series.id))).scalar_one()


def count_by_status(session: Session) -> dict[Status, int]:
    stmt = select(Series.status, func.count(Series.id)).group_by(Series.status)
    return {status: n for status, n in session.execute(stmt)}


# --- v1.5 candidate: MakeDB.pm replacement -----------------------------------

_VALID_STATUSES = {
    Status.NEW,
    Status.IN_PROGRESS,
    Status.ARCHIVED,  # legacy MakeDB.pm includes archived series too
}


def validated_series(session: Session) -> list[Series]:
    """Quality filter mirroring tools3/MakeDB.pm.

    Criteria:
    - status not in {deleted, arc1/2/3, del1}
    - edited_cells is numeric and >= 40
    - edited_timepts is numeric

    Used to scope analyses to "real" series. Exposed here in v1 (the body is
    one query) but only relied on starting v1.5.
    """
    rows = session.execute(
        select(Series).where(Series.status.in_(_VALID_STATUSES))
    ).scalars()
    out: list[Series] = []
    for row in rows:
        try:
            cells = int(row.edited_cells)
            int(row.edited_timepts)
        except (TypeError, ValueError):
            continue
        if cells < 40:
            continue
        out.append(row)
    return out
