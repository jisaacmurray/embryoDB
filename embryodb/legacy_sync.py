"""Auto-sync the legacy embryoDB XML when the new DB changes.

The new GUI is the source of truth for series metadata, but the legacy
Java/Perl tools (Tree1, Measure1, RedExtractor1, …) all open
``/gpfs/fs0/l/murr/embryoDB/<series>.xml`` and read fields like
``iRecord[11]`` (``edited_timepts``) from it. If the user edits in the new
GUI without re-exporting, those tools drift — Tree1 starts using a stale
end time, Measure picks the wrong scale, etc.

Until the legacy tools are ported (the v3 milestone), every GUI save path
re-writes the legacy XML so the two systems stay in lock-step. The
serializer is byte-identical for unchanged rows (raw_xml verbatim) and
canonical for edited rows.

Provenance fields (``xml_hash``, ``xml_source_path``, ``xml_mtime``,
``raw_xml``) are updated on the row before commit, so ``audit-import``
keeps reporting clean.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import database
from .config import settings
from .exporters.xml_exporter import _content_for_row
from .fsutil import safe_write_text
from .queries import series as q_series


def sync_legacy_xml(series_name: str) -> Path | None:
    """Re-write ``<source_dir>/<series_name>.xml`` from current DB state.

    Returns the path written, or None if the series no longer exists in the
    DB. Failures (filesystem permission, missing source_dir) are caught and
    logged to stderr — the calling save path should not be aborted because
    the legacy mirror failed to update.

    The sync opens its own session so callers don't need to worry about
    write-after-commit ordering. The file is written after the DB session
    commits, ensuring the file never points to a state the DB has rolled
    back to.
    """
    try:
        with database.session_scope() as session:
            row = q_series.get_by_name(session, series_name)
            if row is None:
                return None
            content = _content_for_row(row)
            target = Path(settings.source_dir) / f"{series_name}.xml"
            # Update provenance fields inside the same transaction as the
            # eventual file write so audit-import keeps round-tripping clean.
            # raw_xml is kept in sync so an immediate re-read reflects the
            # new canonical form for legacy series whose first edit just
            # landed (i.e., version went from 1 → 2).
            row.xml_source_path = str(target)
            row.xml_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            row.xml_mtime = datetime.now(tz=timezone.utc)
            row.raw_xml = content
        # Session committed; write the file under safe permissions.
        safe_write_text(target, content)
        return target
    except Exception as exc:
        # Mirror failure must not break the save UX. Log and continue.
        print(
            f"legacy_sync: failed to sync {series_name!r}: {exc}",
            file=sys.stderr,
        )
        return None


def sync_many(series_names: list[str]) -> tuple[int, int]:
    """Sync each name. Returns (n_written, n_failed)."""
    written = 0
    failed = 0
    for name in series_names:
        result = sync_legacy_xml(name)
        if result is None:
            failed += 1
        else:
            written += 1
    return written, failed


__all__ = ["sync_legacy_xml", "sync_many"]
