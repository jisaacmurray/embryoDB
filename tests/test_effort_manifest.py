"""Tests for the curation-effort corpus manifest (embryodb/effort_manifest.py)."""

from __future__ import annotations

from pathlib import Path

from embryodb import effort_manifest as em
from embryodb.models import MicroscopyMetadata, Series, Status
from embryodb.queries import datasets as q_datasets


def _series(session, name, *, edited="", code="", cells="", strain="n/a",
            gene="n/a", status=Status.NEW, annot_loc="", xy=None, z=None):
    s = Series(
        series_name=name, edited_timepts=edited, partial_editing_code=code,
        edited_cells=cells, strain_name=strain, reporter_gene=gene,
        status=status, annot_loc=annot_loc,
    )
    session.add(s)
    session.flush()
    if xy is not None or z is not None:
        session.add(MicroscopyMetadata(series_id=s.id, voxel_xy_um=xy, voxel_z_um=z))
        session.flush()
    return s


def _with_dats(tmp_path: Path, name: str, *, raw=True, edit=True) -> Path:
    annot = tmp_path / name
    dats = annot / "dats"
    dats.mkdir(parents=True, exist_ok=True)
    if raw:
        (dats / f"{name}.zip").write_bytes(b"PK\x03\x04raw")
    if edit:
        (dats / f"{name}-edit.zip").write_bytes(b"PK\x03\x04edit")
    return annot


# --- curation_scope bucketing ------------------------------------------------

def test_bucket_partial_from_branch_code(db_session):
    s = _series(db_session, "s_partial", edited="120",
                code="120,ABala:180,ABprp:180")
    scope = em.curation_scope(s)
    assert scope.bucket == em.BUCKET_PARTIAL
    assert scope.global_depth == 120  # bare-int "120" == P0:120
    assert {r.cell_name for r in scope.branch_rules} == {"ABala", "ABprp"}


def test_bucket_whole_code_bare_time(db_session):
    s = _series(db_session, "s_whole", edited="90", code="150")
    scope = em.curation_scope(s)
    assert scope.bucket == em.BUCKET_WHOLE_CODE
    assert scope.global_depth == 150  # P0:150 overrides edited_timepts
    assert scope.branch_rules == []


def test_bucket_time_only(db_session):
    s = _series(db_session, "s_time", edited="200", code="")
    scope = em.curation_scope(s)
    assert scope.bucket == em.BUCKET_TIME_ONLY
    assert scope.global_depth == 200


def test_bucket_time_only_sentinel_code(db_session):
    s = _series(db_session, "s_sentinel", edited="200", code="n/a")
    assert em.curation_scope(s).bucket == em.BUCKET_TIME_ONLY


def test_bucket_initials_legacy_checkedby(db_session):
    s = _series(db_session, "s_init", edited="130", code="jim")
    scope = em.curation_scope(s)
    assert scope.bucket == em.BUCKET_INITIALS
    assert scope.global_depth == 130
    assert scope.partial_raw == "jim"


def test_bucket_uncurated_no_depth(db_session):
    s = _series(db_session, "s_none", edited="n/a", code="n/a")
    assert em.curation_scope(s).bucket == em.BUCKET_UNCURATED


# --- resolution --------------------------------------------------------------

def test_resolution_from_microscopy(db_session):
    s = _series(db_session, "s_res", xy=0.09, z=1.0)
    assert em.resolution_for(s) == (0.09, 1.0)


def test_resolution_fallback(db_session):
    s = _series(db_session, "s_nores")
    assert em.resolution_for(s) == (em.DEFAULT_XY_UM, em.DEFAULT_Z_UM)


# --- manifest build + usable + zip stat --------------------------------------

def test_usable_requires_both_zips(db_session, tmp_path):
    annot = _with_dats(tmp_path, "s_full", raw=True, edit=True)
    _series(db_session, "s_full", edited="150", annot_loc=str(annot))
    rows = em.build_manifest(db_session, trusted_lists=())
    row = next(r for r in rows if r.series == "s_full")
    assert row.raw_zip and row.edit_zip and row.usable


def test_not_usable_missing_edit_zip(db_session, tmp_path):
    annot = _with_dats(tmp_path, "s_noedit", raw=True, edit=False)
    _series(db_session, "s_noedit", edited="150", annot_loc=str(annot))
    rows = em.build_manifest(db_session, trusted_lists=())
    row = next(r for r in rows if r.series == "s_noedit")
    assert row.raw_zip and not row.edit_zip and not row.usable


def test_deleted_series_excluded(db_session, tmp_path):
    annot = _with_dats(tmp_path, "s_del")
    _series(db_session, "s_del", edited="150", annot_loc=str(annot),
            status=Status.DELETED)
    rows = em.build_manifest(db_session, trusted_lists=())
    assert all(r.series != "s_del" for r in rows)


def test_trusted_list_clears_aspirational(db_session, tmp_path):
    annot = _with_dats(tmp_path, "s_trust")
    _series(db_session, "s_trust", edited="200", code="", annot_loc=str(annot))
    q_datasets.create(db_session, "EPIC_murrlab_additions", series_names=["s_trust"])
    rows = em.build_manifest(db_session, trusted_lists=("EPIC_murrlab_additions",))
    row = next(r for r in rows if r.series == "s_trust")
    # time_only bucket, but trusted membership clears the aspirational flag.
    assert row.bucket == em.BUCKET_TIME_ONLY
    assert row.trusted_lists == ["EPIC_murrlab_additions"]
    assert row.aspirational is False


def test_aspirational_flag_when_not_trusted(db_session, tmp_path):
    annot = _with_dats(tmp_path, "s_asp")
    _series(db_session, "s_asp", edited="200", code="bw", annot_loc=str(annot))
    rows = em.build_manifest(db_session, trusted_lists=())
    row = next(r for r in rows if r.series == "s_asp")
    assert row.bucket == em.BUCKET_INITIALS
    assert row.aspirational is True


def test_dataset_restriction(db_session, tmp_path):
    for nm in ("m1", "m2", "other"):
        _series(db_session, nm, edited="150", annot_loc=str(_with_dats(tmp_path, nm)))
    q_datasets.create(db_session, "controls", series_names=["m1", "m2"])
    rows = em.build_manifest(db_session, dataset_names=["controls"], trusted_lists=())
    assert {r.series for r in rows} == {"m1", "m2"}


def test_write_and_summarize(db_session, tmp_path):
    _series(db_session, "u1", edited="150", annot_loc=str(_with_dats(tmp_path, "u1")))
    _series(db_session, "u2", edited="150",
            annot_loc=str(_with_dats(tmp_path, "u2", edit=False)))
    _series(db_session, "u3", edited="n/a", code="n/a")
    rows = em.build_manifest(db_session, trusted_lists=())
    summ = em.summarize(rows)
    assert summ.total == 3
    assert summ.usable == 1
    assert summ.missing_edit == 1
    assert summ.by_bucket[em.BUCKET_UNCURATED] == 1

    out = tmp_path / "manifest.tsv"
    em.write_manifest(rows, out)
    header, *body = out.read_text().splitlines()
    assert header.split("\t") == list(em.MANIFEST_COLUMNS)
    assert len(body) == 3
