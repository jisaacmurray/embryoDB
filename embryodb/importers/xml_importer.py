"""Bulk-import legacy XML files from `source_dir` into PostgreSQL.

Read-only against `source_dir`. Each imported row carries provenance fields
(xml_source_path, xml_hash, xml_mtime, raw_xml, imported_at, imported_by) so we
can detect drift later and round-trip byte-perfectly on unmodified rows.

Re-running the importer on the same source_dir is safe:
- Existing rows whose source XML is unchanged (hash matches) are skipped.
- Existing rows whose source XML *has* changed since import are flagged for
  review (we don't overwrite local edits silently).
- New files become new rows.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Series, Status
from ..parsers.xml import XmlFormatError, parse


@dataclass
class ImportReport:
    inserted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    drifted: list[tuple[str, str]] = field(default_factory=list)  # (series, reason)
    parse_errors: list[tuple[str, str]] = field(default_factory=list)  # (path, error)
    skipped_duplicates: list[str] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return (
            len(self.inserted)
            + len(self.unchanged)
            + len(self.drifted)
            + len(self.parse_errors)
            + len(self.skipped_duplicates)
        )

    def summary(self) -> str:
        return (
            f"processed: {self.total_processed}, "
            f"inserted: {len(self.inserted)}, "
            f"unchanged: {len(self.unchanged)}, "
            f"drifted: {len(self.drifted)}, "
            f"duplicates: {len(self.skipped_duplicates)}, "
            f"parse errors: {len(self.parse_errors)}"
        )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _coerce_status(raw: str) -> Status:
    """Map a raw XML status string to the Status enum. Unknown values fall
    back to NEW so import never fails on a status anomaly."""
    if not raw:
        return Status.NEW
    try:
        return Status(raw)
    except ValueError:
        return Status.NEW


def import_dir(
    session: Session,
    source_dir: Path | None = None,
    pattern: str = "*.xml",
    user: str | None = None,
) -> ImportReport:
    """Import every XML matching `pattern` under `source_dir`.

    Returns an ImportReport summarizing the outcome per file. Caller commits.
    """
    src = Path(source_dir or settings.source_dir)
    user = user or settings.user
    if not src.is_dir():
        raise FileNotFoundError(f"source_dir does not exist: {src}")

    report = ImportReport()
    seen_names: set[str] = set()

    for path in sorted(src.glob(pattern)):
        try:
            raw_bytes = path.read_bytes()
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            report.parse_errors.append((str(path), f"decode: {exc}"))
            continue

        try:
            record = parse(raw_text)
        except XmlFormatError as exc:
            report.parse_errors.append((str(path), str(exc)))
            continue

        series_name = record.get("series_name", "")
        if not series_name:
            report.parse_errors.append((str(path), "missing or empty <series name>"))
            continue
        if series_name in seen_names:
            report.skipped_duplicates.append(series_name)
            continue
        seen_names.add(series_name)

        digest = _sha256(raw_bytes)
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

        existing = session.execute(
            select(Series).where(Series.series_name == series_name)
        ).scalar_one_or_none()

        if existing is None:
            row = Series(
                series_name=series_name,
                date_acquired=record["date_acquired"],
                person=record["person"],
                strain_name=record["strain_name"],
                treatments=record["treatments"],
                reporter_gene=record["reporter_gene"],
                image_loc=record["image_loc"],
                timepts=record["timepts"],
                annot_loc=record["annot_loc"],
                acetree_config=record["acetree_config"],
                edited_by=record["edited_by"],
                edited_timepts=record["edited_timepts"],
                edited_cells=record["edited_cells"],
                partial_editing_code=record["partial_editing_code"],
                comments=record["comments"],
                status=_coerce_status(record["status"]),
                xml_source_path=str(path),
                xml_hash=digest,
                xml_mtime=mtime,
                raw_xml=raw_text,
                imported_by=user,
                updated_by=user,
                version=1,
            )
            session.add(row)
            report.inserted.append(series_name)
        elif existing.xml_hash == digest:
            report.unchanged.append(series_name)
        else:
            # Source XML differs from what we imported. If the row hasn't been
            # locally edited (version == 1) it's safe to refresh; otherwise we
            # flag it so a human can reconcile.
            reason = (
                f"source hash changed; existing version={existing.version}; "
                f"old_hash={existing.xml_hash}, new_hash={digest}"
            )
            report.drifted.append((series_name, reason))

    return report
