"""Tests for legacy XML auto-sync.

Verifies that re-syncing after a DB edit lands the new field values in
``source_dir/<series>.xml`` so the legacy Java/Perl tools (Tree1, Measure,
etc.) read the current values rather than the stale ones from the original
import.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb import database, legacy_sync
from embryodb.config import settings
from embryodb.importers.xml_importer import import_dir
from embryodb.queries import series as q_series


@pytest.fixture
def source_dir(tmp_path, monkeypatch, make_source_dir, sample_xml, db_session):
    """A populated source-dir with one imported series. settings.source_dir
    is monkeypatched to point here for the duration of the test."""
    src = make_source_dir({"20240805_JIM763_L5.xml": sample_xml})
    monkeypatch.setattr(settings, "source_dir", src)
    import_dir(db_session, source_dir=src)
    db_session.commit()
    return src


def test_sync_updates_xml_after_field_edit(source_dir):
    name = "20240805_JIM763_L5"
    # Edit a field directly in the DB
    with database.session_scope() as s:
        row = q_series.get_by_name(s, name)
        row.partial_editing_code = "200,ABala:240,MSpapp:240"
        row.edited_timepts = "242"
        row.version = (row.version or 1) + 1

    target = legacy_sync.sync_legacy_xml(name)
    assert target is not None
    assert target == source_dir / f"{name}.xml"

    written = target.read_text(encoding="utf-8")
    # New values are in the file
    assert 'checkedby name="200,ABala:240,MSpapp:240"' in written
    assert 'editedtimepts num="242"' in written
    # Old values are gone
    assert "190" not in written.split('editedtimepts num="')[1].split('"')[0]


def test_sync_updates_provenance(source_dir):
    name = "20240805_JIM763_L5"
    with database.session_scope() as s:
        row = q_series.get_by_name(s, name)
        row.partial_editing_code = "NEW:1"
        row.version = (row.version or 1) + 1

    legacy_sync.sync_legacy_xml(name)

    with database.session_scope() as s:
        row = q_series.get_by_name(s, name)
        assert row.xml_source_path == str(source_dir / f"{name}.xml")
        # raw_xml mirrors the new canonical form
        assert row.raw_xml is not None
        assert 'checkedby name="NEW:1"' in row.raw_xml
        # hash is freshly computed (not the legacy hash from the original file)
        import hashlib
        assert row.xml_hash == hashlib.sha256(
            row.raw_xml.encode("utf-8")
        ).hexdigest()


def test_sync_missing_series_returns_none(source_dir):
    assert legacy_sync.sync_legacy_xml("nope-not-a-series") is None


def test_sync_overwrites_existing_file(source_dir):
    name = "20240805_JIM763_L5"
    target = source_dir / f"{name}.xml"
    original = target.read_text(encoding="utf-8")

    with database.session_scope() as s:
        row = q_series.get_by_name(s, name)
        row.comments = "CHANGED FROM TEST"
        row.version = (row.version or 1) + 1

    legacy_sync.sync_legacy_xml(name)
    after = target.read_text(encoding="utf-8")
    assert after != original
    assert 'comments text="CHANGED FROM TEST"' in after


def test_sync_many_aggregates_results(source_dir):
    name = "20240805_JIM763_L5"
    written, failed = legacy_sync.sync_many([name, "ghost1", "ghost2"])
    assert written == 1
    assert failed == 2


def test_sync_does_not_raise_on_unwritable_source_dir(
    source_dir, monkeypatch, tmp_path
):
    # Point source_dir at a path inside an existing file (so mkdir would fail).
    bad = tmp_path / "notadir"
    bad.write_text("blocking file")
    monkeypatch.setattr(settings, "source_dir", bad)

    # Should swallow the error rather than crash the GUI's save path.
    result = legacy_sync.sync_legacy_xml("20240805_JIM763_L5")
    assert result is None
