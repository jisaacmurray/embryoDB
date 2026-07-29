"""Tests for archiving lineage zips before a pipeline step overwrites them.

The failure this guards against: a re-pipeline on a curated series rewrites
``dats/<series>-edit.zip`` from raw tracker output, silently destroying the
AceTree curation saved there.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from embryodb.config import settings
from embryodb.pipeline import annotation_archive as aa


def _zip(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("nuclei/t001-nuclei", body)
    return path


@pytest.fixture
def dats(tmp_path):
    d = tmp_path / "dats"
    d.mkdir()
    return d


def test_archives_both_zips_and_writes_manifest(dats):
    _zip(dats / "S-edit.zip", "curated")
    _zip(dats / "S.zip", "pristine")

    archived = aa.archive_annotations(dats, "S", reason="unit test")

    assert len(archived) == 2
    gen = next((dats / aa.ARCHIVE_DIRNAME).iterdir())
    assert {p.name for p in gen.iterdir()} == {"S-edit.zip", "S.zip", "MANIFEST.txt"}
    manifest = (gen / "MANIFEST.txt").read_text()
    assert "reason: unit test" in manifest
    assert "S-edit.zip" in manifest


def test_archived_copy_preserves_curation_content(dats):
    _zip(dats / "S-edit.zip", "ABa,ABp curated names")
    aa.archive_annotations(dats, "S", reason="r")

    # simulate the destructive rewrite the pipeline would now do
    _zip(dats / "S-edit.zip", "Nuc1 raw tracker output")

    gen = next((dats / aa.ARCHIVE_DIRNAME).iterdir())
    with zipfile.ZipFile(gen / "S-edit.zip") as zf:
        assert zf.read("nuclei/t001-nuclei").decode() == "ABa,ABp curated names"


def test_is_a_copy_not_a_move(dats):
    """The external legacy writer must still find the file it expects."""
    _zip(dats / "S-edit.zip", "x")
    aa.archive_annotations(dats, "S", reason="r")
    assert (dats / "S-edit.zip").is_file()


def test_noop_when_nothing_exists(dats):
    assert aa.archive_annotations(dats, "S", reason="r") == []
    assert not (dats / aa.ARCHIVE_DIRNAME).exists()


def test_disabled_via_flag(dats):
    _zip(dats / "S-edit.zip", "x")
    assert aa.archive_annotations(dats, "S", reason="r", enabled=False) == []
    assert not (dats / aa.ARCHIVE_DIRNAME).exists()


def test_disabled_via_settings(dats, monkeypatch):
    _zip(dats / "S-edit.zip", "x")
    monkeypatch.setattr(settings, "archive_annotations", False)
    assert aa.archive_annotations(dats, "S", reason="r") == []


def test_generations_accumulate(dats, monkeypatch):
    stamps = iter(["20260101T000001Z", "20260101T000002Z", "20260101T000003Z"])
    monkeypatch.setattr(aa, "_prune", lambda *a, **k: None)
    for _ in range(3):
        _zip(dats / "S-edit.zip", "x")
        stamp = next(stamps)
        dest = dats / aa.ARCHIVE_DIRNAME / stamp
        dest.mkdir(parents=True)
        (dest / "S-edit.zip").write_bytes((dats / "S-edit.zip").read_bytes())
    assert len(list((dats / aa.ARCHIVE_DIRNAME).iterdir())) == 3


def test_prune_keeps_newest_n(dats):
    root = dats / aa.ARCHIVE_DIRNAME
    for stamp in ("20260101T000001Z", "20260101T000002Z", "20260101T000003Z"):
        gen = root / stamp
        gen.mkdir(parents=True)
        (gen / "S.zip").write_text("x")

    aa._prune(dats, keep=2)

    assert sorted(p.name for p in root.iterdir()) == [
        "20260101T000002Z",
        "20260101T000003Z",
    ]


def test_prune_zero_keeps_everything(dats):
    root = dats / aa.ARCHIVE_DIRNAME
    (root / "20260101T000001Z").mkdir(parents=True)
    aa._prune(dats, keep=0)
    assert len(list(root.iterdir())) == 1


def test_land_lineage_archives_previous_curation(tmp_path):
    """The sn_engine=new landing path must not clobber curation unrecorded."""
    from embryodb.pipeline import starrynite_v1 as snv1

    scratch_out = tmp_path / "out"
    scratch_out.mkdir()
    _zip(scratch_out / "S_run.zip", "fresh tracker lineage")

    dats = tmp_path / "dats"
    _zip(dats / "S-edit.zip", "PREVIOUS CURATION")

    result = snv1.land_lineage(scratch_out, dats, "S")

    assert "archived" in result
    gen = next((dats / aa.ARCHIVE_DIRNAME).iterdir())
    with zipfile.ZipFile(gen / "S-edit.zip") as zf:
        assert zf.read("nuclei/t001-nuclei").decode() == "PREVIOUS CURATION"
    # and the new lineage did land
    with zipfile.ZipFile(dats / "S-edit.zip") as zf:
        assert zf.read("nuclei/t001-nuclei").decode() == "fresh tracker lineage"
