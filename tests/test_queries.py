"""Filter chain composition + dataset CRUD."""

from pathlib import Path

from embryodb.importers.xml_importer import import_dir
from embryodb.models import Status
from embryodb.queries import datasets as q_datasets
from embryodb.queries import series as q_series


_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>

<experiment>
<series name="{name}"/>
<date date="{date}"/>
<person name="{person}"/>
<strain name="{strain}"/>
<treatments desc="none"/>
<redsig value="{gene}"/>
<imageloc loc="/x"/>
<timepts num="240"/>
<annots loc="/x"/>
<acetree config="{name}.xml"/>
<editedby name="{editor}"/>
<editedtimepts num="{tp}"/>
<editedcells num="{cells}"/>
<checkedby name="n/a"/>
<comments text="{comments}"/>
<status case="{status}"/>
</experiment>
"""


def _make_corpus(make_source_dir):
    files = {
        "a.xml": _TEMPLATE.format(
            name="20230101_a", date="20230101", person="alice", strain="N2",
            gene="ceh-37", editor="A", tp="240", cells="200",
            comments="alpha", status="new",
        ),
        "b.xml": _TEMPLATE.format(
            name="20240202_b", date="20240202", person="bob", strain="N2",
            gene="ceh-37", editor="B", tp="240", cells="60",
            comments="beta with elt", status="new",
        ),
        "c.xml": _TEMPLATE.format(
            name="20240303_c", date="20240303", person="alice", strain="JIM113",
            gene="elt-2", editor="A", tp="200", cells="30",
            comments="gamma", status="archived",
        ),
        "d.xml": _TEMPLATE.format(
            name="20240404_d", date="20240404", person="carol", strain="JIM113",
            gene="elt-2", editor="C", tp="n/a", cells="n/a",
            comments="delta", status="deleted",
        ),
    }
    return make_source_dir(files)


def test_filter_by_gene(db_session, make_source_dir) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    out = q_series.list_series(db_session, reporter_gene=["ceh-37"])
    assert {r.series_name for r in out} == {"20230101_a", "20240202_b"}


def test_filter_compose_person_and_status(db_session, make_source_dir) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    out = q_series.list_series(
        db_session,
        person=["alice"],
        status=[Status.NEW, Status.ARCHIVED],
    )
    assert {r.series_name for r in out} == {"20230101_a", "20240303_c"}


def test_text_search_comments_only(db_session, make_source_dir) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    out = q_series.list_series(db_session, text="beta", text_in_comments_only=True)
    assert [r.series_name for r in out] == ["20240202_b"]


def test_text_search_all_fields(db_session, make_source_dir) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    # 'elt' appears in both the gene (elt-2) and one comments field.
    out = q_series.list_series(db_session, text="elt", text_in_comments_only=False)
    assert {r.series_name for r in out} == {"20240202_b", "20240303_c", "20240404_d"}


def test_date_after(db_session, make_source_dir) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    out = q_series.list_series(db_session, date_after="20240101")
    assert {r.series_name for r in out} == {"20240202_b", "20240303_c", "20240404_d"}


def test_distinct_values(db_session, make_source_dir) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    assert q_series.distinct_values(db_session, "person") == ["alice", "bob", "carol"]
    assert q_series.distinct_values(db_session, "reporter_gene") == ["ceh-37", "elt-2"]


def test_validated_series_filter(db_session, make_source_dir) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    out = q_series.validated_series(db_session)
    # a: 200 cells, new -> in
    # b:  60 cells, new -> in
    # c:  30 cells, archived -> excluded (cells < 40)
    # d:  n/a cells, deleted -> excluded (status + non-numeric)
    assert {r.series_name for r in out} == {"20230101_a", "20240202_b"}


def test_dataset_overlap(db_session, make_source_dir, tmp_path: Path) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    ds1 = q_datasets.create(db_session, "alpha", series_names=["20230101_a", "20240202_b"])
    ds2 = q_datasets.create(db_session, "beta", series_names=["20240202_b", "20240303_c"])
    assert {s.series_name for s in ds1.series} == {"20230101_a", "20240202_b"}
    assert {s.series_name for s in ds2.series} == {"20240202_b", "20240303_c"}

    # Remove from one — other should be unaffected (the verification 9 case).
    q_datasets.remove_series(db_session, "alpha", ["20240202_b"])
    ds2_after = q_datasets.get_by_name(db_session, "beta")
    assert "20240202_b" in {s.series_name for s in ds2_after.series}


def test_default_sort_is_most_recent_first(db_session, make_source_dir) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    out = q_series.list_series(db_session)
    dates = [r.date_acquired for r in out]
    # Most recent should be first; the test corpus has 20230101 .. 20240404.
    assert dates == sorted(dates, reverse=True)


def test_filter_by_dataset_id(db_session, make_source_dir) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    ds = q_datasets.create(
        db_session, "subset", series_names=["20230101_a", "20240303_c"]
    )
    out = q_series.list_series(db_session, dataset_id=ds.id)
    assert {r.series_name for r in out} == {"20230101_a", "20240303_c"}
    # The full table still has everything when the filter is not applied
    full = q_series.list_series(db_session)
    assert len(full) == 4


def test_dataset_export_list_file(db_session, make_source_dir, tmp_path: Path) -> None:
    src = _make_corpus(make_source_dir)
    import_dir(db_session, source_dir=src)
    q_datasets.create(
        db_session, "x", series_names=["20230101_a", "20240202_b", "20240303_c"]
    )
    out = tmp_path / "x.list"
    q_datasets.export_list_file(db_session, "x", out)
    lines = out.read_text().splitlines()
    assert lines == ["20230101_a", "20240202_b", "20240303_c"]
