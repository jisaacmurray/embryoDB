"""Audit operations: round-trip, duplicates, path validation."""

from pathlib import Path

from embryodb import audits
from embryodb.importers.xml_importer import import_dir
from embryodb.queries import datasets as q_datasets


_T = """<?xml version='1.0' encoding='utf-8'?>

<experiment>
<series name="{name}"/>
<date date="20240101"/>
<person name="p"/>
<strain name="n/a"/>
<treatments desc="n/a"/>
<redsig value="n/a"/>
<imageloc loc="{image_loc}"/>
<timepts num="240"/>
<annots loc="{annot_loc}"/>
<acetree config="{cfg}"/>
<editedby name="n/a"/>
<editedtimepts num="240"/>
<editedcells num="240"/>
<checkedby name="n/a"/>
<comments text="n/a"/>
<status case="new"/>
</experiment>
"""


def test_audit_import_clean(db_session, sample_xml: str, make_source_dir) -> None:
    src = make_source_dir({"20240805_JIM763_L5.xml": sample_xml})
    import_dir(db_session, source_dir=src)
    rep = audits.audit_import(db_session, source_dir=src)
    assert len(rep.matched) == 1
    assert not rep.byte_diffs
    assert not rep.errors


def test_audit_import_detects_local_edit(
    db_session, sample_xml: str, make_source_dir
) -> None:
    src = make_source_dir({"20240805_JIM763_L5.xml": sample_xml})
    import_dir(db_session, source_dir=src)
    # Simulate a local edit: bump version + mutate a field.
    from embryodb.models import Series
    row = db_session.query(Series).one()
    row.comments = "edited!"
    row.version = 2
    db_session.flush()
    rep = audits.audit_import(db_session, source_dir=src)
    assert "20240805_JIM763_L5" in rep.byte_diffs


def test_compare_with_source_returns_match(
    db_session, sample_xml: str, make_source_dir
) -> None:
    src = make_source_dir({"20240805_JIM763_L5.xml": sample_xml})
    import_dir(db_session, source_dir=src)
    matches, generated, source_path = audits.compare_with_source(
        db_session, "20240805_JIM763_L5", source_dir=src
    )
    assert matches
    generated.unlink(missing_ok=True)


def test_find_duplicates_symmetric_diff(db_session, make_source_dir) -> None:
    src = make_source_dir(
        {
            "a.xml": _T.format(name="a", image_loc="/x", annot_loc="/x", cfg="a.xml"),
            "b.xml": _T.format(name="b", image_loc="/x", annot_loc="/x", cfg="b.xml"),
            "orphan.xml": _T.format(
                name="orphan", image_loc="/x", annot_loc="/x", cfg="orphan.xml"
            ),
        }
    )
    import_dir(db_session, source_dir=src)
    # Drop one file to create a row-without-file case.
    (src / "orphan.xml").unlink()
    # Create a file-without-row case.
    (src / "extra.xml").write_text(
        _T.format(name="extra", image_loc="/x", annot_loc="/x", cfg="extra.xml")
    )
    rep = audits.find_duplicates(db_session, source_dir=src)
    assert "extra" in rep.file_without_row
    assert "orphan" in rep.row_without_file


def test_validate_paths(db_session, tmp_path: Path, make_source_dir) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "config.xml").write_text("x")
    src = make_source_dir(
        {
            "good.xml": _T.format(
                name="good",
                image_loc=str(real),
                annot_loc=str(real),
                cfg="config.xml",
            ),
            "bad.xml": _T.format(
                name="bad",
                image_loc="/nowhere/x",
                annot_loc="/nowhere/y",
                cfg="ghost.xml",
            ),
        }
    )
    import_dir(db_session, source_dir=src)
    rep = audits.validate_paths(db_session)
    assert rep.series_count == 2
    assert any(name == "bad" for name, _ in rep.image_missing)
    assert not any(name == "good" for name, _ in rep.image_missing)


def test_validate_paths_for_dataset(db_session, tmp_path: Path, make_source_dir) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "config.xml").write_text("x")
    src = make_source_dir(
        {
            "good.xml": _T.format(
                name="good",
                image_loc=str(real),
                annot_loc=str(real),
                cfg="config.xml",
            ),
            "bad.xml": _T.format(
                name="bad",
                image_loc="/nowhere/x",
                annot_loc="/nowhere/y",
                cfg="ghost.xml",
            ),
        }
    )
    import_dir(db_session, source_dir=src)
    q_datasets.create(db_session, "only-good", series_names=["good"])
    rep = audits.validate_paths(db_session, dataset_name="only-good")
    assert rep.series_count == 1
    assert not rep.image_missing
