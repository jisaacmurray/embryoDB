"""Tests for the 2011-era LASAF SP5 timestamp parser + dispatcher
integration.

The format stores per-timepoint Properties.xml files in an ``info/``
directory parallel to ``dats/``. Each file's ``<StartTime>`` element
gives the wall-clock time the acquisition of that timepoint began;
walking the whole directory lets us recover the per-tp timestamps the
modern Stellaris ``<TimeStampList>`` provides natively.

The metadata-side parser is the existing Stellaris ``parse_properties_xml``
(2011 LASAF reuses the same ``ATLConfocalSettingDefinition`` schema), so
those tests live in ``test_pipeline.py`` / ``test_leica_metadata_parser.py``;
this module focuses on the LASAF-specific timestamp + dispatcher logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb.parsers.microscopy import find_metadata_file, parse_microscopy
from embryodb.parsers.timestamps import (
    LeicaSP5InfoTimestampParser,
    parse_timestamps,
    parse_timestamps_from_dir,
)


# ---------------------------------------------------------------------------
# Synthetic LASAF per-tp Properties.xml builder
# ---------------------------------------------------------------------------


def _lasaf_tp_xml(start_time_text: str) -> str:
    """A minimal LASAF per-tp Properties.xml. Only the bits the parser
    actually reads — ``<StartTime>`` (timestamp) plus skeletal scaffolding
    so an XML parse succeeds."""
    return f"""<?xml version="1.0"?>
<Data>
  <Image>
    <ImageDescription>
      <Name>tp synthetic</Name>
      <StartTime>{start_time_text}</StartTime>
      <EndTime>{start_time_text}</EndTime>
    </ImageDescription>
  </Image>
</Data>
"""


def _write_lasaf_series(tmp_path: Path, starts: dict[int, str]) -> Path:
    """Write a ``info/`` directory with one ``_t<NNN>_Properties.xml`` per
    entry. Returns the path to the smallest-tp file (the t001 representative).
    """
    info = tmp_path / "info"
    info.mkdir()
    paths: list[Path] = []
    for tp, start in sorted(starts.items()):
        p = info / f"synth_t{tp:03d}_Properties.xml"
        p.write_text(_lasaf_tp_xml(start), encoding="utf-8")
        paths.append(p)
    return paths[0]


# ---------------------------------------------------------------------------
# can_parse + dispatcher detection
# ---------------------------------------------------------------------------


def test_can_parse_only_inside_info_dir(tmp_path):
    parser = LeicaSP5InfoTimestampParser()
    info = tmp_path / "info"
    info.mkdir()
    p_good = info / "x_t001_Properties.xml"
    p_good.write_text(_lasaf_tp_xml("2/9/2011 6:35:50 PM"))
    p_outside = tmp_path / "x_t001_Properties.xml"
    p_outside.write_text(_lasaf_tp_xml("2/9/2011 6:35:50 PM"))
    assert parser.can_parse(p_good) is True
    # Same filename but wrong parent dir — must not claim it.
    assert parser.can_parse(p_outside) is False


def test_find_metadata_file_picks_lasaf_info(tmp_path):
    """When only `info/*_t<NNN>_Properties.xml` is present, the dispatcher
    returns the smallest-tp file as the representative."""
    (tmp_path / "dats").mkdir()  # exists but empty
    _write_lasaf_series(
        tmp_path,
        {
            5: "2/9/2011 6:35:50 PM",
            10: "2/9/2011 6:36:00 PM",
            1: "2/9/2011 6:35:30 PM",
        },
    )
    found = find_metadata_file(tmp_path, position=1)
    assert found is not None
    assert found.name == "synth_t001_Properties.xml"
    assert found.parent.name == "info"


def test_find_metadata_file_prefers_stellaris_over_info(tmp_path):
    """If both info/ AND modern Properties.xml exist (during a migration),
    Stellaris wins."""
    dats = tmp_path / "dats"
    dats.mkdir()
    (dats / "acq_Position 1_Properties.xml").write_text("<x/>")
    _write_lasaf_series(tmp_path, {1: "2/9/2011 6:35:30 PM"})
    found = find_metadata_file(tmp_path, position=1)
    assert found is not None
    assert found.name.endswith("_Position 1_Properties.xml")


def test_parse_microscopy_dispatches_to_lasaf_info(tmp_path):
    p = _write_lasaf_series(tmp_path, {1: "2/9/2011 6:35:30 PM"})
    md = parse_microscopy(p)
    # The shim is just parse_properties_xml; on this minimal fixture it
    # produces an empty-but-valid LeicaMetadata. No exception is the
    # primary assertion.
    assert md is not None
    assert md.objective == ""  # no <ATLConfocalSettingDefinition> in fixture
    assert md.channels == []


# ---------------------------------------------------------------------------
# Timestamp extraction across an info/ dir
# ---------------------------------------------------------------------------


def test_lasaf_timestamps_basic(tmp_path):
    """Three timepoints 60s apart should produce 0, 60, 120 absolute seconds."""
    p = _write_lasaf_series(
        tmp_path,
        {
            1: "2/9/2011 6:35:30 PM",
            2: "2/9/2011 6:36:30 PM",
            3: "2/9/2011 6:37:30 PM",
        },
    )
    result = LeicaSP5InfoTimestampParser().parse(p)
    assert result.vendor == "leica_sp5_info_xml"
    # LASAF <StartTime> is naive local time; absolute UTC anchor stays None.
    assert result.acquired_at is None
    assert [v.timepoint for v in result.volumes] == [1, 2, 3]
    assert result.volumes[0].absolute_seconds == 0
    assert result.volumes[0].delta_seconds == 0
    assert result.volumes[1].absolute_seconds == 60
    assert result.volumes[1].delta_seconds == 60
    assert result.volumes[2].absolute_seconds == 120


def test_lasaf_timestamps_handle_milliseconds_suffix(tmp_path):
    """LASAF appends ``.mmm`` after the time; strptime can't read that.
    The parser must strip it before parsing."""
    p = _write_lasaf_series(
        tmp_path,
        {
            1: "2/9/2011 6:35:30 PM.429",
            2: "2/9/2011 6:36:30 PM.812",
        },
    )
    result = LeicaSP5InfoTimestampParser().parse(p)
    assert len(result.volumes) == 2
    assert result.volumes[1].absolute_seconds == 60


def test_lasaf_timestamps_24h_format_also_works(tmp_path):
    """Some exports use 24-hour clock instead of 12h AM/PM."""
    p = _write_lasaf_series(
        tmp_path,
        {1: "2/9/2011 18:35:30", 2: "2/9/2011 18:36:30"},
    )
    result = LeicaSP5InfoTimestampParser().parse(p)
    assert result.volumes[1].absolute_seconds == 60


def test_lasaf_missing_tp_holds_previous_delta(tmp_path):
    """If one timepoint's file is unparseable, the next valid tp should
    still land at the right absolute time (mirrors the OME-XML parser's
    behaviour)."""
    info = tmp_path / "info"
    info.mkdir()
    # t001 valid; t002 broken; t003 valid 120s after t001.
    (info / "x_t001_Properties.xml").write_text(_lasaf_tp_xml("2/9/2011 6:35:30 PM"))
    (info / "x_t002_Properties.xml").write_text("<not xml>")
    (info / "x_t003_Properties.xml").write_text(_lasaf_tp_xml("2/9/2011 6:37:30 PM"))
    result = LeicaSP5InfoTimestampParser().parse(info / "x_t001_Properties.xml")
    assert len(result.volumes) == 3
    assert result.volumes[2].absolute_seconds == 120


def test_lasaf_via_registry_and_parse_timestamps_from_dir(tmp_path):
    """The LASAF parser plugs into the registry and can be found through
    parse_timestamps_from_dir when given the info/ directory."""
    p = _write_lasaf_series(
        tmp_path,
        {1: "2/9/2011 6:35:30 PM", 2: "2/9/2011 6:36:30 PM"},
    )
    result = parse_timestamps(p)
    assert result is not None
    assert result.vendor == "leica_sp5_info_xml"

    result_via_dir = parse_timestamps_from_dir(p.parent, position=1)
    assert result_via_dir is not None
    assert result_via_dir.vendor == "leica_sp5_info_xml"
    assert len(result_via_dir.volumes) == 2


# ---------------------------------------------------------------------------
# Real fixture sanity check
# ---------------------------------------------------------------------------


_REAL_INFO = Path(
    "/gpfs/fs0/u/jmurr/images/20110209_UP2051_mls-2_L2/info"
)


@pytest.mark.skipif(
    not _REAL_INFO.exists(),
    reason="real LASAF SP5 fixture not present on this host",
)
def test_real_lasaf_fixture():
    # The dispatcher should find the t001 file as the representative.
    annot = _REAL_INFO.parent
    md_file = find_metadata_file(annot, position=1)
    assert md_file is not None
    assert md_file.parent.name == "info"
    assert md_file.name.endswith("_t001_Properties.xml")

    # Microscopy metadata: 2011 schema lacks <DetectionReferenceLine> so
    # laser cross-reference comes back empty; the active detector is
    # still found (PMT 2, the active channel in this acquisition).
    md = parse_microscopy(md_file)
    assert md is not None
    active = [c for c in md.channels if c.get("is_active")]
    assert len(active) >= 1
    # Detector name falls back to Type ("PMT 2") when Name is absent
    # in the 2011 schema.
    assert active[0]["detector_name"]

    # Timestamp extraction: ~256 timepoints with reasonable cycle time.
    ts = parse_timestamps(md_file)
    assert ts is not None
    assert ts.vendor == "leica_sp5_info_xml"
    assert len(ts.volumes) > 100
    assert ts.volumes[0].absolute_seconds == 0
    assert 30 <= ts.volumes[1].delta_seconds <= 200
