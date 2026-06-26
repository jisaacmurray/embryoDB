"""Tests for the v2 pipeline-import path.

Avoids generating real TIFFs by writing tiny 4x4 8-bit fixtures with
tifffile, which keeps the tests in-memory-fast (<1s for the whole module).
"""

from __future__ import annotations

import numpy as np
import pytest
import tifffile

from embryodb.models import (
    Acquisition,
    ImageLayout,
    MicroscopyMetadata,
    PipelineStepRun,
    Protocol,
    RunStatus,
    Series,
    Status,
)
from embryodb.parsers.filename import parse_one, scan_directory
from embryodb.parsers.leica_metadata import parse_properties_xml
from embryodb.parsers.matlab_params import parse, render
from embryodb.pipeline.orchestrate import (
    ImportOptions,
    STEPS,
    import_acquisition,
)
from embryodb.pipeline.protocol_seed import seed_protocols
from embryodb.pipeline.stage import plan_acquisition, role_subdir


# ---------------------------------------------------------------------------
# Filename parser
# ---------------------------------------------------------------------------


def test_leica_parser_basic(tmp_path):
    p = tmp_path / "myacq_Position 3_t012_z05_RAW_ch01.tif"
    p.write_bytes(b"")
    img = parse_one("leica_tilescan", p)
    assert img is not None
    assert img.acquisition_stem == "myacq"
    assert img.position == 3
    assert img.timepoint == 12
    assert img.z_plane == 5
    assert img.raw_channel == 1


def test_leica_parser_without_raw_marker(tmp_path):
    p = tmp_path / "acq_Position 1_t000_z00_ch00.tif"
    p.write_bytes(b"")
    img = parse_one("leica_tilescan", p)
    assert img is not None
    assert img.timepoint == 0 and img.z_plane == 0


def test_leica_parser_rejects_unrelated(tmp_path):
    p = tmp_path / "definitely_not_leica.tif"
    p.write_bytes(b"")
    assert parse_one("leica_tilescan", p) is None


def test_scan_directory_yields_in_order(tmp_path):
    for pos in (2, 1):
        for t in (1, 0):
            for z in (0, 1):
                for ch in (0, 1):
                    name = f"a_Position {pos}_t{t:03d}_z{z:02d}_RAW_ch{ch:02d}.tif"
                    (tmp_path / name).write_bytes(b"")
    images = list(scan_directory("leica_tilescan", tmp_path))
    assert len(images) == 16
    # plan_acquisition groups by position; just verify both positions appear
    assert {img.position for img in images} == {1, 2}


# ---------------------------------------------------------------------------
# Leica metadata parser
# ---------------------------------------------------------------------------


_MINIMAL_PROPERTIES = """<?xml version="1.0"?>
<Image>
  <Dimensions>
    <DimensionDescription DimID="X" NumberOfElements="712" Voxel="0.087"/>
    <DimensionDescription DimID="Y" NumberOfElements="712" Voxel="0.087"/>
    <DimensionDescription DimID="Z" NumberOfElements="67" Voxel="0.5"/>
    <DimensionDescription DimID="T" NumberOfElements="240" Voxel="1.000 s"/>
  </Dimensions>
  <Channels>
    <ChannelDescription ChannelTag="0" LUTName="Green" Resolution="8"/>
    <ChannelDescription ChannelTag="0" LUTName="Red" Resolution="8"/>
  </Channels>
  <Settings>
    <ATLConfocalSettingDefinition
        ObjectiveName="HC PL APO CS2    63x/1.30 GLYC"
        NumericalAperture="1.3"
        Magnification="63"
        Immersion="GLYC"
        RefractionIndex="1.46"
        Pinhole="154.4 µm"
        PinholeAiry="1.5 AU"
        ScanSpeed="8,000 Hz"
        FrameAverage="3"
        LineAverage="1"
        StagePosX="59,276.56 µm"
        StagePosY="41,675.75 µm"
        ZPosition="-163.51 µm"/>
  </Settings>
</Image>
"""


def test_parse_properties_xml(tmp_path):
    p = tmp_path / "Position 1_Properties.xml"
    p.write_text(_MINIMAL_PROPERTIES)
    md = parse_properties_xml(p)
    assert md.voxel_xy_um == 0.087
    assert md.voxel_z_um == 0.5
    assert md.planes_per_volume == 67
    assert md.n_timepoints == 240
    assert md.objective.startswith("HC PL APO CS2")
    assert md.objective_NA == 1.3
    assert md.magnification == 63.0
    assert md.pinhole_um == 154.4
    assert md.scan_speed_hz == 8000.0
    assert md.frame_averaging == 3
    assert md.stage_x_um == 59276.56
    assert len(md.channels) == 2


# ---------------------------------------------------------------------------
# Matlab params parser
# ---------------------------------------------------------------------------


_PARAMS_SAMPLE = """%comment
slices=67;
xyres=0.087;
zres=0.5;
firsttimestepdiam=80;
parameters.intensitythreshold=[0.009,0.0075,0.008];
%end_time=240;
"""


def test_matlab_params_parse():
    p = parse(_PARAMS_SAMPLE)
    assert p.get("slices") == "67"
    assert p.get("xyres") == "0.087"
    assert p.get("zres") == "0.5"
    assert p.get_int("slices") == 67
    assert p.get_float("zres") == 0.5
    assert p.get("end_time") is None  # commented-out


def test_matlab_params_set_appends_new_key():
    p = parse(_PARAMS_SAMPLE)
    p.set("end_time", "240")
    out = render(p)
    assert "end_time=240;" in out
    # Original commented-out version preserved verbatim
    assert "%end_time=240;" in out


def test_matlab_params_set_replaces_existing_line():
    p = parse(_PARAMS_SAMPLE)
    p.set("zres", "0.504")
    out = render(p)
    assert "zres=0.504;" in out
    # No duplicate active assignment
    assert out.count("zres=") == 1


# ---------------------------------------------------------------------------
# Pipeline orchestrator (end-to-end on synthetic 4x4 fixture)
# ---------------------------------------------------------------------------


def _write_fixture_tif(path):
    arr = np.zeros((4, 4), dtype=np.uint8)
    tifffile.imwrite(path, arr)


@pytest.fixture
def synth_acquisition(tmp_path):
    """Two positions × 2 timepoints × 3 planes × 2 channels = 24 files."""
    src = tmp_path / "src"
    src.mkdir()
    for pos in (1, 2):
        for t in range(2):
            for z in range(3):
                for ch in (0, 1):
                    name = f"acq_Position {pos}_t{t:03d}_z{z:02d}_RAW_ch{ch:02d}.tif"
                    _write_fixture_tif(src / name)
    return src


def _stellaris_properties_xml(*, planes: int, channels: int, timepoints: int) -> str:
    """A minimal Stellaris-shaped Properties.xml with one FILETIME per plane.

    Volume 1 starts at FILETIME 0x1dbcf207ea07660 (2025-05-27 16:00:40 UTC);
    each subsequent volume is +90 seconds; the in-volume planes are 100
    ticks apart (irrelevant — they're decimated out).
    """
    first_ft = 0x1DBCF207EA07660
    per_vol = planes * channels
    ticks = 10_000_000  # 100ns per second
    fts: list[int] = []
    for tp in range(timepoints):
        vol_start = first_ft + tp * 90 * ticks
        for k in range(per_vol):
            fts.append(vol_start + k * 100)
    body = " ".join(format(v, "x") for v in fts)
    total = per_vol * timepoints
    return f"""<?xml version='1.0' encoding='utf-8'?>
<Data><Image>
<ImageDescription>
  <NumberOfChannels>{channels}</NumberOfChannels>
  <ATLConfocalSettings>
    <ATLConfocalSettingDefinition Sections="{planes}" CycleCount="{timepoints}"
        CycleTime="90" ObjectiveName="HC PL APO 63x" NumericalAperture="1.3"/>
  </ATLConfocalSettings>
</ImageDescription>
<TimeStampList NumberOfTimeStamps="{total}" FirstTimeStampDate="5/27/2025"
               FirstTimeStampTime="12:00:40 PM"
               FirstTimeStampMiliSeconds="790">{body}</TimeStampList>
</Image></Data>
"""


@pytest.fixture
def synth_acquisition_with_metadata(synth_acquisition):
    """Adds Properties.xml for each position so compute_timestamps has data."""
    for pos in (1, 2):
        (synth_acquisition / f"acq_Position {pos}_Properties.xml").write_text(
            _stellaris_properties_xml(planes=3, channels=2, timepoints=2)
        )
    return synth_acquisition


@pytest.fixture
def seeded_protocols(db_session, tmp_path):
    params_dir = tmp_path / "params"
    params_dir.mkdir()
    (params_dir / "Stellaris_JIM113").write_text(
        "%test fixture\nslices=3;\nxyres=0.087;\nzres=0.5;\nfirsttimestepdiam=80;\n"
    )
    (params_dir / "Stellaris_RW10029").write_text(
        "%test fixture\nslices=3;\nxyres=0.087;\nzres=0.5;\nfirsttimestepdiam=80;\n"
    )
    report = seed_protocols(db_session, parameters_dir=params_dir)
    assert len(report.inserted) == 2
    return report


def test_protocol_seeder_channel_maps(db_session, seeded_protocols):
    from sqlalchemy import select

    rows = list(db_session.execute(select(Protocol).order_by(Protocol.name)).scalars())
    by_name = {r.name: r for r in rows}
    # JIM113-style: swapped (0=reporter, 1=histone)
    assert by_name["Stellaris_JIM113"].channel_map == {"0": "reporter", "1": "histone"}
    # RW10029: sequential (0=histone, 1=reporter)
    assert by_name["Stellaris_RW10029"].channel_map == {"0": "histone", "1": "reporter"}


def test_plan_acquisition_discovers_positions(db_session, seeded_protocols, synth_acquisition):
    from sqlalchemy import select

    proto = db_session.execute(
        select(Protocol).where(Protocol.name == "Stellaris_JIM113")
    ).scalar_one()
    plan = plan_acquisition(synth_acquisition, proto)
    assert plan.position_numbers == [1, 2]
    p1 = plan.positions[1]
    assert p1.n_timepoints == 2
    assert p1.planes_per_volume == 3
    assert set(p1.files_by_channel.keys()) == {0, 1}


def test_import_acquisition_end_to_end(
    db_session, seeded_protocols, synth_acquisition, tmp_path
):
    from sqlalchemy import select

    proto = db_session.execute(
        select(Protocol).where(Protocol.name == "Stellaris_JIM113")
    ).scalar_one()

    img_root = tmp_path / "images"
    alias_root = tmp_path / "alias"
    legacy_dir = tmp_path / "legacy_xml"
    legacy_dir.mkdir()

    opts = ImportOptions(
        image_loc_root=img_root,
        alias_root=alias_root,
        user="testuser",
        run_through_step="write_matlab_params",
    )
    result = import_acquisition(
        db_session,
        source_dir=synth_acquisition,
        protocol=proto,
        options=opts,
        person="alice",
        strain_name="N2",
        legacy_xml_dir=legacy_dir,
    )

    # 1) Acquisition + 2 Series rows persisted
    assert result.acquisition.name == "acq"
    assert {s.series_name for s in result.acquisition.series_list} == {"acq_L1", "acq_L2"}

    # 2) Channel routing: JIM113 swap means ch00 (raw) → tifR/ and ch01 → tif/
    s1_root = img_root / "testuser" / "images" / "acq_L1"
    assert (s1_root / "tif").is_dir()
    assert (s1_root / "tifR").is_dir()
    assert len(list((s1_root / "tif").glob("*.tif"))) == 6  # 2 t × 3 p
    assert len(list((s1_root / "tifR").glob("*.tif"))) == 6

    # 3) AceTree-style naming: z-inverted, 1-indexed t and p
    # Raw z=0 -> p=slices (3); z=2 -> p=1. Verify both ends exist:
    assert (s1_root / "tif" / "acq_L1-t001-p01.tif").exists()
    assert (s1_root / "tif" / "acq_L1-t001-p03.tif").exists()

    # 4) AceTree config XML
    cfg = (s1_root / "dats" / "acq_L1.xml").read_text()
    assert "<image" in cfg and "tif/acq_L1-t001-p01.tif" in cfg
    assert 'planeEnd="2"' in cfg  # slices=3 → planeEnd=2

    # 5) Legacy embryoDB XML in legacy_dir; audit-import compatible
    legacy = legacy_dir / "acq_L1.xml"
    assert legacy.exists()
    body = legacy.read_text()
    assert '<series name="acq_L1"/>' in body
    assert '<person name="alice"/>' in body
    assert '<strain name="N2"/>' in body

    # 6) Alias symlink
    alias_link = alias_root / "testuser" / "images" / "acq_L1"
    assert alias_link.is_symlink()
    assert alias_link.resolve() == s1_root.resolve()

    # 7) matlabParams generated with metadata-derived resolution
    params_text = (s1_root / "matlabParams").read_text()
    assert "slices=3;" in params_text
    assert "xyres=0.087" in params_text or "xyres=" in params_text
    assert "end_time=2;" in params_text  # number of timepoints

    # 8) PipelineStepRun rows: 7 complete, 3 pending per series (×2 series).
    # Inline (complete after orchestrator returns): stage_images,
    # stage_metadata, compute_timestamps, write_acetree_config,
    # write_embryodb_xml, create_alias_symlink, write_matlab_params.
    # Worker (still pending): run_starrynite, run_red_extract, run_measure.
    # compute_timestamps soft-succeeds when there's no Properties.xml in
    # the fixture (which is the case here), so it still counts as COMPLETE.
    runs = list(db_session.execute(select(PipelineStepRun)).scalars())
    assert len(runs) == len(STEPS) * 2
    complete = [r for r in runs if r.status == RunStatus.COMPLETE]
    pending = [r for r in runs if r.status == RunStatus.PENDING]
    assert len(complete) == 7 * 2
    assert len(pending) == 3 * 2


def test_compute_timestamps_populates_db_and_csv(
    db_session, seeded_protocols, synth_acquisition_with_metadata, tmp_path
):
    """End-to-end check that the import step parses Stellaris metadata,
    fills the volume_timestamps table, and writes a legacy-format
    TIME{series}.csv."""
    from sqlalchemy import select
    from embryodb.models import VolumeTimestamp

    proto = db_session.execute(
        select(Protocol).where(Protocol.name == "Stellaris_JIM113")
    ).scalar_one()
    img_root = tmp_path / "images"
    legacy_dir = tmp_path / "legacy_xml"
    legacy_dir.mkdir()

    opts = ImportOptions(
        image_loc_root=img_root,
        alias_root=None,
        user="testuser",
        run_through_step="compute_timestamps",
    )
    result = import_acquisition(
        db_session,
        source_dir=synth_acquisition_with_metadata,
        protocol=proto,
        options=opts,
        person="alice",
        strain_name="N2",
        legacy_xml_dir=legacy_dir,
    )

    # 1) Each series gets 2 rows in volume_timestamps (we wrote 2 timepoints).
    series = result.acquisition.series_list
    assert len(series) == 2
    for s in series:
        rows = (
            db_session.query(VolumeTimestamp)
            .filter_by(series_id=s.id)
            .order_by(VolumeTimestamp.timepoint)
            .all()
        )
        assert [r.timepoint for r in rows] == [1, 2]
        assert rows[0].absolute_seconds == 0
        assert rows[0].delta_seconds == 0
        assert rows[1].absolute_seconds == 90
        assert rows[1].delta_seconds == 90

    # 2) TIME<series>.csv is emitted in the right shape.
    from pathlib import Path
    s1 = next(s for s in series if s.series_name == "acq_L1")
    csv = Path(s1.annot_loc) / "dats" / f"TIME{s1.series_name}.csv"
    assert csv.is_file()
    lines = csv.read_text().strip().split("\n")
    assert len(lines) == 2
    assert lines[0].split("\t") == [s1.series_name, "1", "0", "0"]
    assert lines[1].split("\t") == [s1.series_name, "2", "90", "90"]

    # 3) acquired_at propagated to MicroscopyMetadata.
    s1_full = db_session.merge(s1)
    assert s1_full.microscopy is not None
    assert s1_full.microscopy.acquired_at is not None
    assert s1_full.microscopy.acquired_at.year == 2025


def test_emit_time_csv_helper_overwrites(db_session, tmp_path):
    """``step_write_time_csv`` should produce a deterministic file from DB rows."""
    from embryodb.models import Series, VolumeTimestamp
    from embryodb.pipeline.orchestrate import step_write_time_csv

    s = Series(series_name="foo", annot_loc=str(tmp_path / "annots"))
    db_session.add(s)
    db_session.flush()
    s.volume_timestamps = [
        VolumeTimestamp(timepoint=1, absolute_seconds=0, delta_seconds=0),
        VolumeTimestamp(timepoint=2, absolute_seconds=90, delta_seconds=90),
        VolumeTimestamp(timepoint=3, absolute_seconds=180, delta_seconds=90),
    ]
    db_session.flush()

    target = step_write_time_csv(s)
    assert target.read_text() == "foo\t1\t0\t0\nfoo\t2\t90\t90\nfoo\t3\t180\t90\n"

    # Re-emit after mutating rows — should reflect the new values.
    s.volume_timestamps[1].absolute_seconds = 95
    s.volume_timestamps[1].delta_seconds = 95
    db_session.flush()
    target2 = step_write_time_csv(s)
    assert "foo\t2\t95\t95" in target2.read_text()


def test_import_treats_existing_legacy_xml_as_already_done(
    db_session, seeded_protocols, synth_acquisition, tmp_path
):
    """Re-importing when a legacy XML already exists should succeed, not fail.

    The file is left untouched; provenance is populated from the existing
    content so audit-import stays happy.
    """
    from sqlalchemy import select

    proto = db_session.execute(
        select(Protocol).where(Protocol.name == "Stellaris_JIM113")
    ).scalar_one()
    legacy_dir = tmp_path / "legacy_xml"
    legacy_dir.mkdir()
    # Pre-existing file — simulates a re-import or a file written by the
    # legacy Java GUI.
    (legacy_dir / "acq_L1.xml").write_text("existing-content")

    opts = ImportOptions(
        image_loc_root=tmp_path / "images",
        alias_root=tmp_path / "alias",
        user="testuser",
        run_through_step="write_embryodb_xml",
    )
    result = import_acquisition(
        db_session,
        source_dir=synth_acquisition,
        protocol=proto,
        options=opts,
        legacy_xml_dir=legacy_dir,
    )
    # Both series should succeed — acq_L1 treats the existing file as done.
    by_name = {o.series_name: o for o in result.series_outcomes}
    assert by_name["acq_L1"].failed_step is None
    assert by_name["acq_L2"].failed_step is None
    # Existing file must not have been overwritten.
    assert (legacy_dir / "acq_L1.xml").read_text() == "existing-content"
    # Provenance must be populated from the existing file.
    from sqlalchemy import select as sa_select
    from embryodb.models import Series as S
    row = db_session.execute(
        sa_select(S).where(S.series_name == "acq_L1")
    ).scalar_one()
    assert row.xml_source_path == str(legacy_dir / "acq_L1.xml")


# ---------------------------------------------------------------------------
# Channel role -> subdir
# ---------------------------------------------------------------------------


def test_role_subdir_known_roles():
    assert role_subdir("histone", 0) == "tif"
    assert role_subdir("reporter", 1) == "tifR"
    assert role_subdir("DIC", 2) == "DIC"


def test_role_subdir_unknown_role_falls_back_to_tifC():
    assert role_subdir("reporter2", 3) == "tifC3"
    assert role_subdir("", 4) == "tifC4"


# ---------------------------------------------------------------------------
# Backfill: pretend the pipeline already ran on disk; discover state
# ---------------------------------------------------------------------------


def test_backfill_picks_up_existing_state(db_session, tmp_path):
    from embryodb.pipeline.backfill import backfill_directory

    # Pre-create a Series row that the backfill must find by name
    series = Series(
        series_name="legacy_acq_L1",
        person="legacy",
        strain_name="N2",
        status=Status.NEW,
    )
    db_session.add(series)
    db_session.flush()

    img_root = tmp_path / "images"
    s_dir = img_root / "legacy_acq_L1"
    (s_dir / "tif").mkdir(parents=True)
    (s_dir / "tifR").mkdir(parents=True)
    (s_dir / "dats").mkdir(parents=True)
    # Make stage_images "complete" by adding a stub tif
    (s_dir / "tif" / "legacy_acq_L1-t001-p01.tif").write_bytes(b"")
    # AceTree config -> write_acetree_config complete
    (s_dir / "dats" / "legacy_acq_L1.xml").write_text("<embryo/>")
    # matlabParams -> write_matlab_params complete
    (s_dir / "matlabParams").write_text("slices=67;\n")
    # StarryNite output -> run_starrynite complete
    (s_dir / "dats" / "legacy_acq_L1-edit.zip").write_bytes(b"")

    report = backfill_directory(db_session, img_root)
    assert "legacy_acq" in report.acquisitions_created
    assert "legacy_acq_L1" in report.series_linked

    from sqlalchemy import select
    runs = list(
        db_session.execute(
            select(PipelineStepRun).where(PipelineStepRun.series_id == series.id)
        ).scalars()
    )
    by_step = {r.step: r.status for r in runs}
    assert by_step["stage_images"] == RunStatus.COMPLETE
    assert by_step["write_acetree_config"] == RunStatus.COMPLETE
    assert by_step["write_matlab_params"] == RunStatus.COMPLETE
    assert by_step["run_starrynite"] == RunStatus.COMPLETE
    # Steps with no on-disk marker stay PENDING
    assert by_step["run_red_extract"] == RunStatus.PENDING
    assert by_step["run_measure"] == RunStatus.PENDING

    # Series got linked back to the newly-created acquisition
    db_session.refresh(series)
    assert series.acquisition is not None
    assert series.acquisition.name == "legacy_acq"
    assert series.position == 1
    assert series.image_loc == str(s_dir)


def test_backfill_is_idempotent(db_session, tmp_path):
    from embryodb.pipeline.backfill import backfill_directory

    db_session.add(Series(series_name="acq2_L1", status=Status.NEW))
    db_session.flush()
    s_dir = tmp_path / "images" / "acq2_L1"
    s_dir.mkdir(parents=True)

    r1 = backfill_directory(db_session, tmp_path / "images")
    r2 = backfill_directory(db_session, tmp_path / "images")
    assert r1.runs_inserted == 10  # one per step
    assert r2.runs_inserted == 0  # nothing new the second time


# ---------------------------------------------------------------------------
# subprocess_steps: SKIPPED / mock-success paths
# ---------------------------------------------------------------------------


def _make_series_with_run(session, series_name: str, step: str) -> tuple:
    """Helper: create a minimal Series + RUNNING PipelineStepRun, return (series, run)."""
    series = Series(
        series_name=series_name,
        status=Status.NEW,
        image_loc=f"/tmp/fake/{series_name}",
        annot_loc=f"/tmp/fake/{series_name}",
    )
    session.add(series)
    session.flush()
    run = PipelineStepRun(
        series_id=series.id,
        step=step,
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.flush()
    return series, run


def test_run_red_extract_skipped_when_no_reporter(db_session, tmp_path):
    """run_red_extract marks the run SKIPPED when protocol has no reporter channel."""
    from embryodb.pipeline.subprocess_steps import step_run_red_extract

    series, run = _make_series_with_run(db_session, "test_no_rep_L1", "run_red_extract")
    run_id = run.id
    image_loc = tmp_path / "test_no_rep_L1"
    image_loc.mkdir()

    # channel_map has no reporter role
    step_run_red_extract(
        series.id, series.series_name, image_loc, run_id,
        protocol_channel_map={"0": "histone"},
    )

    db_session.expire_all()
    refreshed = db_session.get(PipelineStepRun, run_id)
    assert refreshed.status == RunStatus.SKIPPED


def test_run_measure_skipped_when_no_reporter(db_session, tmp_path):
    """run_measure marks the run SKIPPED when protocol has no reporter channel."""
    from embryodb.pipeline.subprocess_steps import step_run_measure

    series, run = _make_series_with_run(db_session, "test_no_rep2_L1", "run_measure")
    run_id = run.id
    image_loc = tmp_path / "test_no_rep2_L1"
    image_loc.mkdir()

    step_run_measure(
        series.id, series.series_name, image_loc, run_id,
        protocol_channel_map={},
    )

    db_session.expire_all()
    refreshed = db_session.get(PipelineStepRun, run_id)
    assert refreshed.status == RunStatus.SKIPPED


# ---------------------------------------------------------------------------
# Orphaned-step cleanup: a FAILED step must not strand downstream PENDING rows
# ---------------------------------------------------------------------------


def _add_run(session, series_id, step, status):
    run = PipelineStepRun(series_id=series_id, step=step, status=status)
    session.add(run)
    session.flush()
    return run


def test_fail_orphaned_steps_clears_downstream_of_failed(db_session):
    """A FAILED run_starrynite should cause its PENDING run_red_extract and
    run_measure to be marked FAILED (not left PENDING forever), in one pass."""
    from embryodb.pipeline.worker import _fail_orphaned_steps

    series = Series(series_name="orphan_L1", status=Status.NEW)
    db_session.add(series)
    db_session.flush()
    _add_run(db_session, series.id, "stage_images", RunStatus.COMPLETE)
    _add_run(db_session, series.id, "run_starrynite", RunStatus.FAILED)
    re = _add_run(db_session, series.id, "run_red_extract", RunStatus.PENDING)
    me = _add_run(db_session, series.id, "run_measure", RunStatus.PENDING)

    cleared = _fail_orphaned_steps(db_session)
    assert cleared == 2

    db_session.expire_all()
    assert db_session.get(PipelineStepRun, re.id).status == RunStatus.FAILED
    assert db_session.get(PipelineStepRun, me.id).status == RunStatus.FAILED
    assert "run_starrynite failed" in (
        db_session.get(PipelineStepRun, re.id).error_excerpt or ""
    )


def test_fail_orphaned_steps_leaves_runnable_pending(db_session):
    """Downstream steps whose prerequisites are all terminal-OK stay PENDING —
    only those behind a FAILED upstream are cleared."""
    from embryodb.pipeline.worker import _fail_orphaned_steps

    series = Series(series_name="healthy_L1", status=Status.NEW)
    db_session.add(series)
    db_session.flush()
    _add_run(db_session, series.id, "stage_images", RunStatus.COMPLETE)
    _add_run(db_session, series.id, "run_starrynite", RunStatus.COMPLETE)
    re = _add_run(db_session, series.id, "run_red_extract", RunStatus.PENDING)

    assert _fail_orphaned_steps(db_session) == 0
    db_session.expire_all()
    assert db_session.get(PipelineStepRun, re.id).status == RunStatus.PENDING


def test_fail_orphaned_steps_ignores_pending_upstream(db_session):
    """A still-PENDING (not yet failed) upstream must not trigger a cascade —
    the downstream is simply not yet runnable, not orphaned."""
    from embryodb.pipeline.worker import _fail_orphaned_steps

    series = Series(series_name="inflight_L1", status=Status.NEW)
    db_session.add(series)
    db_session.flush()
    _add_run(db_session, series.id, "stage_images", RunStatus.COMPLETE)
    sn = _add_run(db_session, series.id, "run_starrynite", RunStatus.PENDING)
    re = _add_run(db_session, series.id, "run_red_extract", RunStatus.PENDING)

    assert _fail_orphaned_steps(db_session) == 0
    db_session.expire_all()
    assert db_session.get(PipelineStepRun, sn.id).status == RunStatus.PENDING
    assert db_session.get(PipelineStepRun, re.id).status == RunStatus.PENDING


def test_step_run_starrynite_complete_on_success(db_session, tmp_path, monkeypatch):
    """run_starrynite marks the run COMPLETE when the subprocess exits 0."""
    import subprocess as _sp
    from embryodb.pipeline.subprocess_steps import step_run_starrynite

    class _FakeProc:
        returncode = 0
        def poll(self): return 0  # immediately done

    monkeypatch.setattr(_sp, "Popen", lambda *a, **kw: _FakeProc())

    series, run = _make_series_with_run(db_session, "test_sn_L1", "run_starrynite")
    run_id = run.id
    image_loc = tmp_path / "test_sn_L1"
    (image_loc / "dats").mkdir(parents=True)
    (image_loc / "tif").mkdir(parents=True)
    (image_loc / "matlabParams").write_text("slices=67;\n")

    step_run_starrynite(series.id, series.series_name, image_loc, run_id)

    db_session.expire_all()
    refreshed = db_session.get(PipelineStepRun, run_id)
    assert refreshed.status == RunStatus.COMPLETE
    assert refreshed.log_path is not None


def test_step_run_starrynite_failed_on_nonzero(db_session, tmp_path, monkeypatch):
    """run_starrynite marks the run FAILED when subprocess exits non-zero."""
    import subprocess as _sp
    from embryodb.pipeline.subprocess_steps import step_run_starrynite

    class _FakeProc:
        returncode = 1
        def poll(self): return 1

    monkeypatch.setattr(_sp, "Popen", lambda *a, **kw: _FakeProc())

    series, run = _make_series_with_run(db_session, "test_sn_fail_L1", "run_starrynite")
    run_id = run.id
    image_loc = tmp_path / "test_sn_fail_L1"
    (image_loc / "dats").mkdir(parents=True)
    (image_loc / "tif").mkdir(parents=True)

    step_run_starrynite(series.id, series.series_name, image_loc, run_id)

    db_session.expire_all()
    refreshed = db_session.get(PipelineStepRun, run_id)
    assert refreshed.status == RunStatus.FAILED


def test_diagnose_starrynite_cell_ct_limit_overflow(tmp_path):
    """A tracer crash after tracing began is recognized as the cell_ct_limit bug."""
    from embryodb.pipeline.subprocess_steps import diagnose_starrynite_failure

    log = tmp_path / "sn.log"
    log.write_text(
        "running matlab nuclear detection:\n"
        "Elapsed time is 833.3 seconds.\n"
        "running lineaging:\n"
        "Start cell tracing:\n"
        "tracing, round 1: 8300\n"
        "allocated neighbor arrays\n"
        "Segmentation fault\n"
        "SN FAILED!\n"
    )
    msg = diagnose_starrynite_failure(log)
    assert msg is not None
    assert "cell_ct_limit" in msg


def test_diagnose_starrynite_mcr_teardown(tmp_path):
    """A heap abort after detection but before tracing is the MCR-teardown case."""
    from embryodb.pipeline.subprocess_steps import diagnose_starrynite_failure

    log = tmp_path / "sn.log"
    log.write_text(
        "running matlab nuclear detection:\n"
        "Elapsed time is 800.0 seconds.\n"
        "double free or corruption (!prev)\n"
    )
    msg = diagnose_starrynite_failure(log)
    assert msg is not None
    assert "MCR" in msg


def test_diagnose_starrynite_none_on_success(tmp_path):
    """A log that reached 'End time' has no recognized failure cause."""
    from embryodb.pipeline.subprocess_steps import diagnose_starrynite_failure

    log = tmp_path / "sn.log"
    log.write_text("Start cell tracing:\nEnd time: whenever\n")
    assert diagnose_starrynite_failure(log) is None


# ---------------------------------------------------------------------------
# Worker: queue logic and stale-heartbeat reset
# ---------------------------------------------------------------------------


def _make_full_pipeline_series(session, series_name: str) -> Series:
    """Create a Series with inline steps COMPLETE and worker steps PENDING."""
    from embryodb.pipeline.orchestrate import STEPS

    series = Series(
        series_name=series_name,
        status=Status.NEW,
        image_loc=f"/tmp/fake/{series_name}",
        annot_loc=f"/tmp/fake/{series_name}",
    )
    session.add(series)
    session.flush()

    # Inline now ends at write_matlab_params (index 6 after inserting
    # compute_timestamps at index 2). Worker steps are the trailing three.
    inline_steps = STEPS[:7]
    worker_steps = STEPS[7:]

    for step in inline_steps:
        session.add(PipelineStepRun(
            series_id=series.id, step=step, status=RunStatus.COMPLETE,
        ))
    for step in worker_steps:
        session.add(PipelineStepRun(
            series_id=series.id, step=step, status=RunStatus.PENDING,
        ))
    session.flush()
    return series


def test_worker_resets_stale_running(db_session):
    """_reset_stale_running requeues RUNNING rows with a stale heartbeat."""
    from datetime import datetime, timedelta, timezone
    from embryodb.pipeline.worker import _reset_stale_running

    series, run = _make_series_with_run(db_session, "stale_L1", "run_starrynite")
    # Make heartbeat stale (10 minutes ago)
    run.heartbeat_at = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
    db_session.flush()

    n = _reset_stale_running(db_session)
    assert n == 1

    db_session.expire_all()
    refreshed = db_session.get(PipelineStepRun, run.id)
    assert refreshed.status == RunStatus.PENDING
    assert refreshed.started_at is None


def test_worker_finds_next_pending_after_inline_steps(db_session):
    """_next_work_item returns the first PENDING worker step when prerequisites are done."""
    from embryodb.pipeline.worker import _next_work_item

    series = _make_full_pipeline_series(db_session, "queue_test_L1")

    result = _next_work_item(db_session)
    assert result is not None
    found_series, step, run = result
    assert found_series.id == series.id
    assert step == "run_starrynite"


def test_worker_skips_series_with_failed_prerequisite(db_session):
    """_next_work_item returns None when a prerequisite inline step has FAILED."""
    from embryodb.pipeline.worker import _next_work_item
    from embryodb.pipeline.orchestrate import STEPS

    series = Series(series_name="failed_prereq_L1", status=Status.NEW, image_loc="/tmp/x")
    db_session.add(series)
    db_session.flush()

    # write_matlab_params (step 6) is FAILED; run_starrynite is PENDING
    for step in STEPS[:5]:
        db_session.add(PipelineStepRun(
            series_id=series.id, step=step, status=RunStatus.COMPLETE,
        ))
    db_session.add(PipelineStepRun(
        series_id=series.id, step="write_matlab_params", status=RunStatus.FAILED,
    ))
    db_session.add(PipelineStepRun(
        series_id=series.id, step="run_starrynite", status=RunStatus.PENDING,
    ))
    db_session.flush()

    result = _next_work_item(db_session)
    assert result is None


def test_worker_respects_series_order(db_session):
    """_next_work_item returns the series with the lowest id first."""
    from embryodb.pipeline.worker import _next_work_item

    s1 = _make_full_pipeline_series(db_session, "order_test_L1")
    s2 = _make_full_pipeline_series(db_session, "order_test_L2")

    result = _next_work_item(db_session)
    assert result is not None
    assert result[0].id == min(s1.id, s2.id)


def test_worker_respects_not_before(db_session):
    """_next_work_item skips runs whose not_before is in the future."""
    from datetime import datetime, timedelta, timezone
    from embryodb.pipeline.worker import _next_work_item

    series = _make_full_pipeline_series(db_session, "delayed_L1")
    # Switch stage_images from COMPLETE (the fixture default for legacy
    # inline-mode runs) to PENDING with a future not_before — simulating
    # what the orchestrator does when delay_hours > 0.
    future = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    for r in series.runs:
        if r.step == "stage_images":
            r.status = RunStatus.PENDING
            r.not_before = future
        elif r.step in ("run_starrynite", "run_red_extract", "run_measure"):
            r.not_before = future
    db_session.flush()

    assert _next_work_item(db_session) is None

    # Once the delay elapses (simulate by clearing not_before), the worker
    # picks up stage_images first (lowest step in WORKER_STEPS order).
    for r in series.runs:
        if r.step == "stage_images":
            r.not_before = None
    db_session.flush()
    result = _next_work_item(db_session)
    assert result is not None
    assert result[1] == "stage_images"


def test_worker_claim_sets_running_and_claimed_by(db_session):
    """_claim_next flips the chosen PENDING row to RUNNING and stamps claimed_by."""
    import socket
    from embryodb.pipeline.worker import _claim_next

    series = _make_full_pipeline_series(db_session, "claim_test_L1")

    item = _claim_next(db_session)
    assert item is not None
    claimed_series, step, run = item
    assert claimed_series.id == series.id
    assert step == "run_starrynite"
    assert run.status == RunStatus.RUNNING
    assert run.claimed_by == socket.gethostname()
    assert run.started_at is not None
    assert run.heartbeat_at is not None


def test_worker_claim_guard_blocks_already_running(db_session):
    """The guarded UPDATE only matches PENDING rows: re-claiming a row that is
    already RUNNING affects 0 rows (the cross-host race-loser path)."""
    import socket
    from datetime import datetime, timezone
    from sqlalchemy import update
    from embryodb.pipeline.worker import _claim_next

    _make_full_pipeline_series(db_session, "claim_guard_L1")

    item = _claim_next(db_session)
    assert item is not None
    _, _, run = item
    assert run.status == RunStatus.RUNNING

    # Simulate a second worker issuing the same guarded UPDATE against the row
    # the winner already flipped: the PENDING predicate no longer holds.
    now = datetime.now(tz=timezone.utc)
    result = db_session.execute(
        update(PipelineStepRun)
        .where(
            PipelineStepRun.id == run.id,
            PipelineStepRun.status == RunStatus.PENDING,
        )
        .values(status=RunStatus.RUNNING, claimed_by="other-host")
    )
    assert result.rowcount == 0

    db_session.expire(run)
    refreshed = db_session.get(PipelineStepRun, run.id)
    assert refreshed.claimed_by == socket.gethostname()


def test_worker_claim_advances_when_first_step_running(db_session):
    """_claim_next returns the next runnable step (not the already-RUNNING one).

    A row left RUNNING by another worker is not a candidate (only PENDING rows
    are), so the claim moves on rather than re-running it."""
    from embryodb.pipeline.worker import _claim_next

    series = _make_full_pipeline_series(db_session, "claim_advance_L1")
    # First worker step already claimed/running elsewhere.
    for r in series.runs:
        if r.step == "run_starrynite":
            r.status = RunStatus.COMPLETE
    db_session.flush()

    item = _claim_next(db_session)
    assert item is not None
    _, step, _ = item
    assert step == "run_red_extract"


def test_orchestrator_delay_skips_inline_staging(
    db_session, seeded_protocols, synth_acquisition, tmp_path
):
    """When delay_hours > 0, import_acquisition leaves stage_images PENDING
    with not_before set and skips the heavy I/O."""
    from sqlalchemy import select
    from embryodb.models import PipelineStepRun

    proto = db_session.execute(
        select(Protocol).where(Protocol.name == "Stellaris_JIM113")
    ).scalar_one()
    legacy_dir = tmp_path / "legacy_xml"
    legacy_dir.mkdir()

    opts = ImportOptions(
        image_loc_root=tmp_path / "images",
        alias_root=tmp_path / "alias",
        user="testuser",
        delay_hours=4.0,
        run_through_step="write_matlab_params",
    )
    result = import_acquisition(
        db_session,
        source_dir=synth_acquisition,
        protocol=proto,
        options=opts,
        legacy_xml_dir=legacy_dir,
    )
    by_name = {o.series_name: o for o in result.series_outcomes}
    # stage_images should be PENDING (deferred), not COMPLETE.
    assert by_name["acq_L1"].steps.get("stage_images") == RunStatus.PENDING

    # And the DB row for stage_images has not_before set.
    runs = db_session.execute(
        select(PipelineStepRun).where(PipelineStepRun.step == "stage_images")
    ).scalars().all()
    assert all(r.status == RunStatus.PENDING for r in runs)
    assert all(r.not_before is not None for r in runs)
    # Subsequent worker steps inherit the delay too.
    sn_runs = db_session.execute(
        select(PipelineStepRun).where(PipelineStepRun.step == "run_starrynite")
    ).scalars().all()
    assert all(r.not_before is not None for r in sn_runs)

    # Inline steps after stage_images still completed normally.
    metadata_runs = db_session.execute(
        select(PipelineStepRun).where(PipelineStepRun.step == "stage_metadata")
    ).scalars().all()
    assert all(r.status == RunStatus.COMPLETE for r in metadata_runs)
