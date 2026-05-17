"""Importer + exporter round-trip: bytes that come out match bytes that went in."""

import filecmp
from pathlib import Path

from embryodb.exporters.xml_exporter import export_all, export_series
from embryodb.importers.xml_importer import import_dir
from embryodb.models import Series


def test_single_series_round_trip(
    db_session, sample_xml: str, make_source_dir, tmp_path: Path
) -> None:
    src = make_source_dir({"20240805_JIM763_L5.xml": sample_xml})
    report = import_dir(db_session, source_dir=src, user="tester")
    assert len(report.inserted) == 1
    assert not report.parse_errors

    out_dir = tmp_path / "out"
    path = export_series(db_session, "20240805_JIM763_L5", export_dir=out_dir)
    assert filecmp.cmp(path, src / "20240805_JIM763_L5.xml", shallow=False)


def test_unescaped_lt_round_trip(
    db_session, sample_xml_unescaped: str, make_source_dir, tmp_path: Path
) -> None:
    src = make_source_dir({"20110509_JIM65_L3.xml": sample_xml_unescaped})
    import_dir(db_session, source_dir=src)
    path = export_series(db_session, "20110509_JIM65_L3", export_dir=tmp_path / "out")
    assert filecmp.cmp(path, src / "20110509_JIM65_L3.xml", shallow=False)


def test_missing_element_preserved_via_raw_xml(
    db_session, sample_xml_missing_status: str, make_source_dir, tmp_path: Path
) -> None:
    """Legacy files missing one of 16 elements must round-trip via raw_xml,
    not via the canonical serializer (which always emits 16)."""
    src = make_source_dir({"20141220_JIM113_L3.xml": sample_xml_missing_status})
    import_dir(db_session, source_dir=src)
    path = export_series(db_session, "20141220_JIM113_L3", export_dir=tmp_path / "out")
    assert filecmp.cmp(path, src / "20141220_JIM113_L3.xml", shallow=False)


def test_bulk_import_export(
    db_session,
    sample_xml: str,
    sample_xml_unescaped: str,
    sample_xml_missing_status: str,
    make_source_dir,
    tmp_path: Path,
) -> None:
    src = make_source_dir(
        {
            "20240805_JIM763_L5.xml": sample_xml,
            "20110509_JIM65_L3.xml": sample_xml_unescaped,
            "20141220_JIM113_L3.xml": sample_xml_missing_status,
        }
    )
    import_dir(db_session, source_dir=src)
    out_dir = tmp_path / "out"
    report = export_all(db_session, export_dir=out_dir)
    assert len(report.written) == 3
    for name in ("20240805_JIM763_L5.xml", "20110509_JIM65_L3.xml", "20141220_JIM113_L3.xml"):
        assert filecmp.cmp(src / name, out_dir / name, shallow=False), f"diff: {name}"


def test_provenance_populated(
    db_session, sample_xml: str, make_source_dir
) -> None:
    src = make_source_dir({"20240805_JIM763_L5.xml": sample_xml})
    import_dir(db_session, source_dir=src, user="tester")
    row = db_session.query(Series).one()
    assert row.xml_source_path == str(src / "20240805_JIM763_L5.xml")
    assert row.xml_hash and len(row.xml_hash) == 64
    assert row.imported_by == "tester"
    assert row.raw_xml == sample_xml
    assert row.version == 1


def test_reimport_unchanged_is_idempotent(
    db_session, sample_xml: str, make_source_dir
) -> None:
    src = make_source_dir({"20240805_JIM763_L5.xml": sample_xml})
    r1 = import_dir(db_session, source_dir=src)
    assert len(r1.inserted) == 1 and not r1.unchanged
    r2 = import_dir(db_session, source_dir=src)
    assert not r2.inserted and len(r2.unchanged) == 1


def test_reimport_drift_flagged(
    db_session, sample_xml: str, make_source_dir
) -> None:
    src = make_source_dir({"20240805_JIM763_L5.xml": sample_xml})
    import_dir(db_session, source_dir=src)
    # Mutate the source file content.
    altered = sample_xml.replace('case="new"', 'case="archived"')
    (src / "20240805_JIM763_L5.xml").write_text(altered, encoding="utf-8")
    r = import_dir(db_session, source_dir=src)
    assert not r.inserted
    assert len(r.drifted) == 1
    assert r.drifted[0][0] == "20240805_JIM763_L5"
