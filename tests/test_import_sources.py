"""Tests for import-source reclamation.

The regression that matters is the one CheckImages.pl had: an acquisition whose
L1 staged and whose L2 did not must keep its source, because the old script
deleted the whole directory as soon as any single position looked done.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from embryodb.import_sources import (
    _lif_path,
    plan_source_gc,
    verify_series,
)
from embryodb.models import (
    Acquisition,
    MicroscopyMetadata,
    PipelineStepRun,
    Protocol,
    RunStatus,
    Series,
)


def _staged(tmp_path, name, n_t=2, n_p=3, first_empty=False, drop_last=False):
    """A staged tree. Only the two spot-checked planes need to exist."""
    loc = tmp_path / "staged" / name
    tif = loc / "tif"
    tif.mkdir(parents=True)
    (tif / f"{name}-t001-p01.tif").write_bytes(b"" if first_empty else b"xx")
    if not drop_last:
        (tif / f"{name}-t{n_t:03d}-p{n_p:02d}.tif").write_bytes(b"xx")
    return loc


def _series(
    name,
    loc,
    *,
    n_t=2,
    n_p=3,
    channels=1,
    written=None,
    appended=0,
    status=RunStatus.COMPLETE,
    md=True,
):
    s = Series(series_name=name, image_loc=str(loc))
    if md:
        s.microscopy = MicroscopyMetadata(
            n_timepoints=n_t,
            planes_per_volume=n_p,
            channels_per_plane=channels,
            voxel_xy_um=0.087,
            voxel_z_um=1.0,
        )
    if status is not None:
        s.runs.append(
            PipelineStepRun(
                step="stage_images",
                status=status,
                output_summary={
                    "planes_written": (
                        (n_t + appended) * n_p * channels if written is None else written
                    ),
                    "appended_timepoints": appended,
                },
            )
        )
    return s


def test_lif_path_splits_the_virtual_source():
    assert _lif_path("/murrlab3/Images/a.lif::TileScan 1") == __import__("pathlib").Path(
        "/murrlab3/Images/a.lif"
    )
    assert _lif_path("/murrlab3/Images/20250409_JIM763") is None


def test_fully_staged_series_passes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1")
    assert verify_series(_series("e_L1", loc), src).ok


def test_short_plane_count_blocks(tmp_path):
    """The core check: a truncated extraction must not license a delete."""
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1")
    v = verify_series(_series("e_L1", loc, written=4), src)  # expected 2*3 = 6
    assert not v.ok
    assert any("truncated" in p for p in v.problems)


def test_segmented_acquisition_passes(tmp_path):
    """A movie split across LIF objects is longer than its metadata claims.

    Real case: 20260603_ceh-27_JIM593 staged 241 timepoints from 2 segments
    while the metadata records 240, which an expectation built from metadata
    alone reads as a 67-plane overrun.
    """
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1", n_t=3, n_p=3)
    v = verify_series(
        _series("e_L1", loc, n_t=2, n_p=3, channels=2, appended=1), src
    )
    assert v.ok, v.problems
    assert v.planes_found == 18  # (2+1)t x 3p x 2ch


def test_skipped_channel_passes(tmp_path):
    """A channel given role `skip` is legitimately absent from the staged tree."""
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1")
    v = verify_series(_series("e_L1", loc, channels=2, written=6), src)  # 1 of 2
    assert v.ok, v.problems


def test_more_channels_than_the_source_has_blocks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1")
    v = verify_series(_series("e_L1", loc, channels=2, written=18), src)  # 3 of 2
    assert not v.ok
    assert any("channels" in p for p in v.problems)


def test_missing_plane_on_disk_blocks(tmp_path):
    """Staging counted them, but someone removed the tree afterwards."""
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1", drop_last=True)
    v = verify_series(_series("e_L1", loc), src)
    assert not v.ok
    assert any("last plane missing" in p for p in v.problems)


def test_zero_byte_plane_blocks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1", first_empty=True)
    v = verify_series(_series("e_L1", loc), src)
    assert not v.ok
    assert any("zero-byte" in p for p in v.problems)


def test_missing_metadata_blocks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1")
    v = verify_series(_series("e_L1", loc, md=False), src)
    assert not v.ok
    assert "no microscopy metadata parsed" in v.problems


def test_skipped_stage_images_blocks(tmp_path):
    """`mark-legacy` writes SKIPPED — that means staging never ran."""
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1")
    v = verify_series(_series("e_L1", loc, status=RunStatus.SKIPPED), src)
    assert not v.ok
    assert any("stage_images not complete" in p for p in v.problems)


def test_staged_tree_inside_the_source_blocks(tmp_path):
    """Backfilled-in-place series: deleting the source is deleting the movie."""
    src = tmp_path / "src"
    loc = _staged(src, "e_L1")
    v = verify_series(_series("e_L1", loc), src)
    assert not v.ok
    assert any("inside the source" in p for p in v.problems)


def test_one_bad_position_keeps_the_whole_source(db_session, tmp_path):
    """The CheckImages.pl regression."""
    src = tmp_path / "Images" / "acq1"
    src.mkdir(parents=True)
    good = _staged(tmp_path, "acq1_L1")
    bad = _staged(tmp_path, "acq1_L2")

    proto = Protocol(name="p", parameters_file_path="/tmp/p")
    acq = Acquisition(
        name="acq1",
        source_dir=str(src),
        protocol=proto,
        staged_at=datetime.now(tz=timezone.utc) - timedelta(days=90),
    )
    acq.series_list = [_series("acq1_L1", good), _series("acq1_L2", bad, written=1)]
    db_session.add(acq)
    db_session.flush()

    (verdict,) = plan_source_gc(db_session, roots=(tmp_path / "Images",))
    assert not verdict.ok
    assert any("acq1_L2" in b for b in verdict.blockers)
    # and the healthy position is not itself the reason
    assert not any("acq1_L1" in b for b in verdict.blockers)


def test_grace_period_blocks_a_fresh_import(db_session, tmp_path):
    src = tmp_path / "Images" / "acq2"
    src.mkdir(parents=True)
    proto = Protocol(name="p2", parameters_file_path="/tmp/p")
    acq = Acquisition(
        name="acq2",
        source_dir=str(src),
        protocol=proto,
        staged_at=datetime.now(tz=timezone.utc) - timedelta(days=2),
    )
    acq.series_list = [_series("acq2_L1", _staged(tmp_path, "acq2_L1"))]
    db_session.add(acq)
    db_session.flush()

    (verdict,) = plan_source_gc(db_session, older_than=30, roots=(tmp_path / "Images",))
    assert not verdict.ok
    assert any("grace period" in b for b in verdict.blockers)


def test_source_outside_the_roots_is_never_proposed(db_session, tmp_path):
    """A staged or hand-curated dir that isn't an import source stays untouched."""
    src = tmp_path / "elsewhere" / "acq3"
    src.mkdir(parents=True)
    proto = Protocol(name="p3", parameters_file_path="/tmp/p")
    acq = Acquisition(
        name="acq3",
        source_dir=str(src),
        protocol=proto,
        staged_at=datetime.now(tz=timezone.utc) - timedelta(days=90),
    )
    acq.series_list = [_series("acq3_L1", _staged(tmp_path, "acq3_L1"))]
    db_session.add(acq)
    db_session.flush()

    assert plan_source_gc(db_session, roots=(tmp_path / "Images",)) == []


def test_clean_acquisition_is_proposed(db_session, tmp_path):
    src = tmp_path / "Images" / "acq4"
    src.mkdir(parents=True)
    (src / "raw.tif").write_bytes(b"x" * 100)
    proto = Protocol(name="p4", parameters_file_path="/tmp/p")
    acq = Acquisition(
        name="acq4",
        source_dir=str(src),
        protocol=proto,
        staged_at=datetime.now(tz=timezone.utc) - timedelta(days=90),
    )
    acq.series_list = [_series("acq4_L1", _staged(tmp_path, "acq4_L1"))]
    db_session.add(acq)
    db_session.flush()

    (verdict,) = plan_source_gc(db_session, roots=(tmp_path / "Images",))
    assert verdict.ok, verdict.blockers
    assert verdict.kind == "dir"
    assert verdict.bytes_reclaimed == 100


def test_dirs_only_skips_lif_sources(db_session, tmp_path):
    lif = tmp_path / "Images" / "a.lif"
    lif.parent.mkdir(parents=True)
    lif.write_bytes(b"x")
    proto = Protocol(name="p5", parameters_file_path="/tmp/p")
    acq = Acquisition(
        name="a",
        source_dir=f"{lif}::TileScan 1",
        parser_name="lif",
        protocol=proto,
        staged_at=datetime.now(tz=timezone.utc) - timedelta(days=90),
    )
    acq.series_list = [_series("a_L1", _staged(tmp_path, "a_L1"))]
    db_session.add(acq)
    db_session.flush()

    assert plan_source_gc(db_session, roots=(tmp_path / "Images",), include_lif=False) == []
    # with LIFs included it is considered, but an unreadable stub blocks it
    (verdict,) = plan_source_gc(db_session, roots=(tmp_path / "Images",))
    assert verdict.kind == "lif"
    assert not verdict.ok


def test_resumed_staging_counts_skipped_planes(tmp_path):
    """A resumed run writes nothing: the planes were already on disk.

    Real case: several 20260716_JIM801 positions record planes_written=0 with a
    full complement in planes_skipped. Reading only planes_written calls a
    fully-staged movie empty.
    """
    src = tmp_path / "src"
    src.mkdir()
    loc = _staged(tmp_path, "e_L1")
    s = _series("e_L1", loc, written=0)
    s.runs[0].output_summary = {"planes_written": 0, "planes_skipped": 6}
    assert verify_series(s, src).ok
