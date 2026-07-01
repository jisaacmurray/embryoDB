"""Single-file dataset-list import (`import_list_file`).

Exercises the efficient single-file path added for the Import menu / CLI
`dataset import-list`, and confirms it shares behavior with the bulk
`import_lists` (create → refresh → unknown-series skipped).
"""

from pathlib import Path

from embryodb.importers.list_importer import import_list_file
from embryodb.importers.xml_importer import import_dir
from embryodb.queries import datasets as q_datasets

_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>

<experiment>
<series name="{name}"/>
<date date="20230101"/>
<person name="alice"/>
<strain name="N2"/>
<treatments desc="none"/>
<redsig value="ceh-37"/>
<imageloc loc="/x"/>
<timepts num="240"/>
<annots loc="/x"/>
<acetree config="{name}.xml"/>
<editedby name="A"/>
<editedtimepts num="240"/>
<editedcells num="200"/>
<checkedby name="n/a"/>
<comments text="c"/>
<status case="new"/>
</experiment>
"""


def _seed(db_session, make_source_dir, names):
    files = {f"{n}.xml": _TEMPLATE.format(name=n) for n in names}
    import_dir(db_session, source_dir=make_source_dir(files))


def test_import_single_list_creates_dataset(db_session, make_source_dir, tmp_path: Path):
    _seed(db_session, make_source_dir, ["20230101_a", "20230202_b"])
    list_file = tmp_path / "20230101_mylist.txt"
    list_file.write_text("# a comment\n20230101_a\n20230202_b\n", encoding="utf-8")

    report = import_list_file(db_session, list_file)

    assert report.inserted == ["20230101_mylist"]
    ds = q_datasets.get_by_name(db_session, "20230101_mylist")
    assert ds is not None
    assert {s.series_name for s in ds.series} == {"20230101_a", "20230202_b"}


def test_import_single_list_name_override(db_session, make_source_dir, tmp_path: Path):
    _seed(db_session, make_source_dir, ["20230101_a"])
    list_file = tmp_path / "raw.txt"
    list_file.write_text("20230101_a\n", encoding="utf-8")

    report = import_list_file(db_session, list_file, name="curated_set")

    assert report.inserted == ["curated_set"]
    assert q_datasets.get_by_name(db_session, "raw") is None
    assert q_datasets.get_by_name(db_session, "curated_set") is not None


def test_import_single_list_records_missing_series(db_session, make_source_dir, tmp_path: Path):
    _seed(db_session, make_source_dir, ["20230101_a"])
    list_file = tmp_path / "partial.txt"
    list_file.write_text("20230101_a\n20239999_ghost\n", encoding="utf-8")

    report = import_list_file(db_session, list_file)

    assert report.missing_series["partial"] == ["20239999_ghost"]
    ds = q_datasets.get_by_name(db_session, "partial")
    assert {s.series_name for s in ds.series} == {"20230101_a"}


def test_reimport_same_list_is_unchanged_then_refreshes(
    db_session, make_source_dir, tmp_path: Path
):
    _seed(db_session, make_source_dir, ["20230101_a", "20230202_b"])
    list_file = tmp_path / "20230101_mylist.txt"
    list_file.write_text("20230101_a\n", encoding="utf-8")
    assert import_list_file(db_session, list_file).inserted == ["20230101_mylist"]

    # Same content → unchanged.
    assert import_list_file(db_session, list_file).unchanged == ["20230101_mylist"]

    # Content changes → refreshed with the new membership.
    list_file.write_text("20230101_a\n20230202_b\n", encoding="utf-8")
    report = import_list_file(db_session, list_file)
    assert report.refreshed == ["20230101_mylist"]
    ds = q_datasets.get_by_name(db_session, "20230101_mylist")
    assert {s.series_name for s in ds.series} == {"20230101_a", "20230202_b"}
