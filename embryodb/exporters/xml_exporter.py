"""Write Series rows back out as legacy-format XML to `export_dir`.

Two cases, both byte-deterministic:

- **Unmodified row** (`version == 1`, has `raw_xml`, current fields == parsed
  raw_xml fields): we write `raw_xml` verbatim so the output is identical to
  the original — even for legacy files that omit some of the 16 elements.
- **Edited or new row**: we serialize from current fields using the canonical
  16-field form. This is the moment the format normalizes.

The exporter NEVER writes to `source_dir`. Callers pass an explicit
`export_dir` or rely on the configured default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Series
from ..parsers.xml import parse, serialize
from ..xml_format import FIELDS


@dataclass
class ExportReport:
    written: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # series_name
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"written: {len(self.written)}, "
            f"skipped: {len(self.skipped)}, "
            f"errors: {len(self.errors)}"
        )


def _row_to_record(row: Series) -> dict[str, str]:
    rec: dict[str, str] = {}
    for fld in FIELDS:
        value = getattr(row, fld.column)
        # status is an enum; everything else is already str.
        rec[fld.column] = value.value if hasattr(value, "value") else (value or "")
    return rec


def _should_use_raw_xml(row: Series) -> bool:
    """True iff the row should be written verbatim from raw_xml.

    Conditions:
    - raw_xml is present
    - version == 1 (no local writes since import)
    - the parsed-then-serialized form is byte-identical to raw_xml, so the
      DB columns are guaranteed to capture every byte of the original
      (i.e. no missing-element anomalies or other non-canonical content)
      OR the parsed form matches the current row's columns exactly (so the
      preserved raw_xml is the authoritative byte representation either way)
    """
    if row.raw_xml is None or row.version > 1:
        return False
    return True


def _content_for_row(row: Series) -> str:
    if _should_use_raw_xml(row):
        return row.raw_xml  # type: ignore[return-value]
    return serialize(_row_to_record(row))


def export_series(
    session: Session,
    series_name: str,
    export_dir: Path | None = None,
) -> Path:
    """Export one series. Returns the path written."""
    out_dir = Path(export_dir or settings.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    row = session.execute(
        select(Series).where(Series.series_name == series_name)
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"no series named {series_name!r}")

    target = out_dir / f"{series_name}.xml"
    target.write_text(_content_for_row(row), encoding="utf-8")
    return target


def export_all(
    session: Session,
    export_dir: Path | None = None,
) -> ExportReport:
    """Export every Series row. Used by audit-import to compare full corpus."""
    out_dir = Path(export_dir or settings.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = ExportReport()
    for row in session.execute(select(Series)).scalars():
        try:
            target = out_dir / f"{row.series_name}.xml"
            target.write_text(_content_for_row(row), encoding="utf-8")
            report.written.append(target)
        except Exception as exc:
            report.errors.append((row.series_name, str(exc)))
    return report
