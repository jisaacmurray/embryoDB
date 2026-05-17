"""Dataset CRUD + list-file export for legacy Perl/R compatibility."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Dataset, Series


class DatasetError(ValueError):
    pass


def create(
    session: Session, name: str, description: str = "", series_names: Iterable[str] = ()
) -> Dataset:
    if session.execute(select(Dataset).where(Dataset.name == name)).scalar_one_or_none():
        raise DatasetError(f"dataset {name!r} already exists")
    members = _resolve_series(session, series_names)
    ds = Dataset(name=name, description=description, series=members)
    session.add(ds)
    session.flush()
    return ds


def add_series(session: Session, dataset_name: str, series_names: Iterable[str]) -> Dataset:
    ds = _get_or_raise(session, dataset_name)
    existing = {s.series_name for s in ds.series}
    for s in _resolve_series(session, series_names):
        if s.series_name not in existing:
            ds.series.append(s)
    return ds


def remove_series(session: Session, dataset_name: str, series_names: Iterable[str]) -> Dataset:
    ds = _get_or_raise(session, dataset_name)
    drop = set(series_names)
    ds.series = [s for s in ds.series if s.series_name not in drop]
    return ds


def list_datasets(
    session: Session,
    *,
    name_substring: str | None = None,
    tag: str | None = None,
) -> list[Dataset]:
    """List datasets, optionally narrowed by name substring or tag substring.

    Both filters are case-insensitive substring matches; both compose with AND.
    Use `tag` to pick e.g. all "ALZ" datasets, all "JIM497" datasets, etc.
    """
    stmt = select(Dataset).order_by(Dataset.name)
    if name_substring:
        stmt = stmt.where(Dataset.name.ilike(f"%{name_substring}%"))
    if tag:
        stmt = stmt.where(Dataset.tags.ilike(f"%{tag}%"))
    return list(session.execute(stmt).scalars())


def distinct_tags(session: Session) -> list[str]:
    """Distinct tag tokens across all datasets, sorted. Used to populate a
    tag dropdown in the GUI."""
    seen: set[str] = set()
    for (csv,) in session.execute(select(Dataset.tags)).all():
        if not csv:
            continue
        for tok in csv.split(","):
            tok = tok.strip()
            if tok:
                seen.add(tok)
    return sorted(seen, key=str.lower)


def get_by_name(session: Session, name: str) -> Dataset | None:
    return session.execute(select(Dataset).where(Dataset.name == name)).scalar_one_or_none()


def export_list_file(session: Session, name: str, output: Path) -> Path:
    """Write a flat list file (one series_name per line). Matches the shape
    of /murr/lists/ files so existing Perl/R scripts can consume it unchanged."""
    ds = _get_or_raise(session, name)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(s.series_name for s in sorted(ds.series, key=lambda s: s.series_name))
        + "\n",
        encoding="utf-8",
    )
    return output


def export_all_to_dir(
    session: Session, target_dir: Path, *, suffix: str = ""
) -> list[Path]:
    """Bulk-export every Dataset to flat list files under `target_dir`.

    Mirrors the safe-mirror idea for legacy XMLs: writes go to a
    configurable export directory, never to `/murr/lists/` directly. The
    files end up named `<dataset><suffix>` so downstream Perl/R scripts
    that look for `/murr/lists/<name>` keep working when `target_dir`
    eventually replaces it.
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for ds in list_datasets(session):
        if not ds.series:
            continue
        out = target_dir / f"{ds.name}{suffix}"
        out.write_text(
            "\n".join(
                s.series_name for s in sorted(ds.series, key=lambda s: s.series_name)
            )
            + "\n",
            encoding="utf-8",
        )
        written.append(out)
    return written


def _get_or_raise(session: Session, name: str) -> Dataset:
    ds = get_by_name(session, name)
    if ds is None:
        raise DatasetError(f"no dataset named {name!r}")
    return ds


def _resolve_series(session: Session, names: Iterable[str]) -> list[Series]:
    names = list(names)
    if not names:
        return []
    rows = session.execute(
        select(Series).where(Series.series_name.in_(names))
    ).scalars()
    by_name = {r.series_name: r for r in rows}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise DatasetError(f"unknown series: {missing[:5]}{'…' if len(missing) > 5 else ''}")
    return [by_name[n] for n in names]
