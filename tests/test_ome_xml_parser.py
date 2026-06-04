"""Tests for the legacy OME-XML metadata + timestamp parsers.

Synthetic XML covers the structural cases; the real-fixture test against
the production ``20140203_sys-1_lit-1i_L1`` SP5 acquisition is skipped
when the fixture isn't on disk.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from embryodb.parsers.microscopy import find_metadata_file, parse_microscopy
from embryodb.parsers.ome_xml import (
    load_image_list,
    parse_acquisition_dates,
    parse_ome_xml_as_metadata,
)
from embryodb.parsers.timestamps import (
    LeicaSP5OmeXmlTimestampParser,
    parse_timestamps,
    parse_timestamps_from_dir,
)


# ---------------------------------------------------------------------------
# Synthetic OME-XML builder — minimum needed to exercise both parsers
# ---------------------------------------------------------------------------


def _synthetic_ome_xml(
    *,
    timepoints: list[tuple[str, str]],
    sp5_microscope: bool = True,
) -> str:
    """Build a small OME-XML 2012-06 document with N Image elements.

    Each ``(name, iso_date)`` tuple in ``timepoints`` becomes one
    ``<Image>``. The Instrument carries one objective (HCX PL APO CS,
    NA 1.3), two PMT detectors, and two lasers (514 and 561 nm) so the
    channel cross-reference has something to resolve.
    """
    images_xml = "".join(
        f"""<Image ID="Image:{i}" Name="{name}">
  <AcquisitionDate>{iso}</AcquisitionDate>
  <InstrumentRef ID="Instrument:0"/>
  <ObjectiveSettings ID="Objective:0:0" RefractiveIndex="1.451"/>
  <Pixels DimensionOrder="XYZCT" ID="Pixels:{i}"
          PhysicalSizeX="0.086" PhysicalSizeY="0.086" PhysicalSizeZ="0.504"
          SizeC="2" SizeT="1" SizeX="712" SizeY="512" SizeZ="67" Type="uint8">
    <Channel Color="-65281" ExcitationWavelength="514" ID="Channel:{i}:0"
             Name="Leica/EYFP" PinholeSize="154.4" SamplesPerPixel="1">
      <LightSourceSettings Attenuation="0.81" ID="LightSource:0:0"/>
      <DetectorSettings Gain="1100.0" ID="Detector:0:0" Offset="0.0"/>
      <LightPath><EmissionFilterRef ID="Filter:0:0"/></LightPath>
    </Channel>
    <Channel Color="-16776961" ExcitationWavelength="561" ID="Channel:{i}:1"
             Name="Leica/TRITC" PinholeSize="154.4" SamplesPerPixel="1">
      <LightSourceSettings Attenuation="0.84" ID="LightSource:0:1"/>
      <DetectorSettings Gain="1100.0" ID="Detector:0:1" Offset="0.0"/>
      <LightPath><EmissionFilterRef ID="Filter:0:1"/></LightPath>
    </Channel>
    <Plane DeltaT="0.0" TheC="0" TheT="0" TheZ="0"/>
  </Pixels>
</Image>"""
        for i, (name, iso) in enumerate(timepoints)
    )
    model = "TCS SP5" if sp5_microscope else "Other"
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2012-06">
  <Instrument ID="Instrument:0">
    <Microscope Model="{model}" Type="Other"/>
    <LightSource ID="LightSource:0:0"><Laser Wavelength="514" Type="Other" LaserMedium="Other"/></LightSource>
    <LightSource ID="LightSource:0:1"><Laser Wavelength="561" Type="Other" LaserMedium="Other"/></LightSource>
    <Detector ID="Detector:0:0" Model="PMT 1" Type="PMT" Zoom="4.0" Offset="0.0"/>
    <Detector ID="Detector:0:1" Model="PMT 2" Type="PMT" Zoom="4.0" Offset="0.0"/>
    <Objective Correction="Other" ID="Objective:0:0" Immersion="Glycerol"
               LensNA="1.3" Model="HCX PL APO CS" NominalMagnification="63"
               SerialNumber="11506194"/>
    <Filter ID="Filter:0:0" Model="ch1"><TransmittanceRange CutIn="524" CutOut="582"/></Filter>
    <Filter ID="Filter:0:1" Model="ch2"><TransmittanceRange CutIn="582" CutOut="700"/></Filter>
  </Instrument>
  {images_xml}
</OME>
"""


def _write_synthetic(
    tmp_path: Path, timepoints: list[tuple[str, str]], gzip_it: bool = False
) -> tuple[Path, Path]:
    """Write the OME-XML + a matching imageList.tsv to ``tmp_path/dats/``."""
    dats = tmp_path / "dats"
    dats.mkdir()
    body = _synthetic_ome_xml(timepoints=timepoints)
    if gzip_it:
        xml_path = dats / "omxml.xml.gz"
        with gzip.open(xml_path, "wb") as f:
            f.write(body.encode("utf-8"))
    else:
        xml_path = dats / "omxml.xml"
        xml_path.write_text(body, encoding="utf-8")
    ilist = dats / "imageList.tsv"
    ilist.write_text(
        "time\timage\n"
        + "".join(f"{i + 1}\t{name}\n" for i, (name, _) in enumerate(timepoints))
    )
    return xml_path, ilist


# ---------------------------------------------------------------------------
# OME-XML metadata parser
# ---------------------------------------------------------------------------


def test_parse_ome_xml_extracts_instrument_and_objective(tmp_path):
    xml, _ = _write_synthetic(
        tmp_path, [("img0", "2014-02-03T10:26:19")]
    )
    md = parse_ome_xml_as_metadata(xml, image_name="img0")
    assert md.objective == "HCX PL APO CS"
    assert md.objective_NA == 1.3
    assert md.magnification == 63
    assert md.immersion == "Glycerol"
    assert md.refractive_index == 1.451
    assert md.acquisition_settings["microscope_model"] == "TCS SP5"
    assert md.acquisition_settings["objective_serial_number"] == "11506194"


def test_parse_ome_xml_voxel_geometry(tmp_path):
    xml, _ = _write_synthetic(tmp_path, [("img0", "2014-02-03T10:26:19")])
    md = parse_ome_xml_as_metadata(xml, image_name="img0")
    assert md.voxel_xy_um == pytest.approx(0.086)
    assert md.voxel_z_um == pytest.approx(0.504)
    assert md.planes_per_volume == 67
    assert md.x_pixels == 712
    assert md.y_pixels == 512


def test_parse_ome_xml_channels_cross_referenced(tmp_path):
    xml, _ = _write_synthetic(tmp_path, [("img0", "2014-02-03T10:26:19")])
    md = parse_ome_xml_as_metadata(xml, image_name="img0")
    assert len(md.channels) == 2
    eyfp = next(c for c in md.channels if c["dye_name"] == "Leica/EYFP")
    assert eyfp["laser_line_nm"] == 514
    # OME Attenuation 0.81 = 81% blocked → 19% transmitted, which is the
    # value the lab reads at the microscope and what we surface here so
    # the value lines up with Stellaris's IntensityDev semantics.
    assert eyfp["laser_intensity_dev"] == pytest.approx(19.0)
    assert eyfp["detector_name"] == "PMT 1"
    assert eyfp["detector_type"] == "PMT"
    assert eyfp["detector_gain"] == 1100.0
    assert eyfp["band"] == "(524nm - 582nm)"


def test_parse_ome_xml_uses_image_name_hint(tmp_path):
    xml, _ = _write_synthetic(
        tmp_path,
        [
            ("first", "2014-02-03T10:00:00"),
            ("second", "2014-02-03T10:01:00"),
        ],
    )
    md1 = parse_ome_xml_as_metadata(xml, image_name="first")
    md2 = parse_ome_xml_as_metadata(xml, image_name="second")
    # Both pick valid Image elements; assertion is that nothing blows up
    # when the hinted image differs from "first non-DriftAF".
    assert md1.voxel_xy_um == md2.voxel_xy_um
    assert md1.objective == md2.objective


def test_parse_ome_xml_depth_compensation_empty_without_imagelist(tmp_path):
    xml, _ = _write_synthetic(tmp_path, [("img0", "2014-02-03T10:26:19")])
    md = parse_ome_xml_as_metadata(xml, image_name="img0")
    # Without a full L imageList, the parser can't tell which Images
    # belong to this L (multi-position acquisitions share one omxml.xml.gz),
    # so it deliberately emits no curve rather than guess.
    assert md.depth_compensation == {}


def _write_ramped_synthetic(tmp_path: Path) -> Path:
    """Two-channel, three-timepoint synthetic with ramped Attenuation on
    one channel to exercise the time-indexed compensation curve.

    ch0 ramps Attenuation 0.90 → 0.85 → 0.80 (i.e. 10% → 15% → 20%
    transmitted); ch1 holds at 0.97 (3%) flat. Both detectors hold gain
    constant.
    """
    timepoints = [
        ("img_001", "2014-02-03T10:00:00"),
        ("img_002", "2014-02-03T10:01:20"),
        ("img_003", "2014-02-03T10:02:40"),
    ]
    # Manually build with per-image attenuation. Reuse _synthetic_ome_xml
    # as a starting point but override Channel LSS values per image.
    body = _synthetic_ome_xml(timepoints=timepoints)
    # Replace the per-channel Attenuation for each Image (Image:0 .. Image:2)
    # with ramped ch0 / flat ch1 values.
    ramps = {
        0: ("0.90", "0.97"),  # img_001
        1: ("0.85", "0.97"),  # img_002
        2: ("0.80", "0.97"),  # img_003
    }
    for idx, (ch0_a, ch1_a) in ramps.items():
        # Each Image has two Channel blocks in fixed order; replace the
        # first Attenuation="0.81" (ch0) and the second Attenuation="0.84"
        # (ch1) within that image.
        marker = f'<Image ID="Image:{idx}"'
        start = body.index(marker)
        end = body.index("</Image>", start)
        chunk = body[start:end]
        chunk = chunk.replace('Attenuation="0.81"', f'Attenuation="{ch0_a}"', 1)
        chunk = chunk.replace('Attenuation="0.84"', f'Attenuation="{ch1_a}"', 1)
        body = body[:start] + chunk + body[end:]
    dats = tmp_path / "dats"
    dats.mkdir()
    xml_path = dats / "omxml.xml"
    xml_path.write_text(body, encoding="utf-8")
    (dats / "imageList.tsv").write_text(
        "time\timage\n"
        + "".join(f"{i + 1}\t{name}\n" for i, (name, _) in enumerate(timepoints))
    )
    return xml_path


def test_attenuation_converts_to_percent_transmitted(tmp_path):
    """OME Attenuation = fraction blocked; we surface fraction transmitted."""
    xml_path = _write_ramped_synthetic(tmp_path)
    image_list = load_image_list(xml_path.parent / "imageList.tsv")
    md = parse_ome_xml_as_metadata(xml_path, image_list=image_list)
    by_ch = {c["channel_name"]: c for c in md.channels if c.get("is_active")}
    # ch0 first imageList entry has Attenuation=0.90 → 10% transmitted.
    assert by_ch["Leica/EYFP"]["laser_intensity_dev"] == pytest.approx(10.0)
    # ch1 has Attenuation=0.97 → 3% transmitted, flat.
    assert by_ch["Leica/TRITC"]["laser_intensity_dev"] == pytest.approx(3.0)


def test_time_indexed_compensation_curve(tmp_path):
    """Ramped attenuation across imageList tps → curve with change points."""
    xml_path = _write_ramped_synthetic(tmp_path)
    image_list = load_image_list(xml_path.parent / "imageList.tsv")
    md = parse_ome_xml_as_metadata(xml_path, image_list=image_list)

    assert md.depth_compensation.get("axis") == "timepoint"
    by_ch = {c["channel_name"]: c for c in md.depth_compensation["channels"]}

    # ch0 ramps 10% → 15% → 20%: 3 change points.
    eyfp = by_ch["Leica/EYFP"]
    assert [p["timepoint"] for p in eyfp["points"]] == [1, 2, 3]
    intensities = [p["intensity_dev"] for p in eyfp["points"]]
    assert intensities[0] == pytest.approx(10.0)
    assert intensities[1] == pytest.approx(15.0)
    assert intensities[2] == pytest.approx(20.0)

    # ch1 flat → single change point.
    tritc = by_ch["Leica/TRITC"]
    assert len(tritc["points"]) == 1
    assert tritc["points"][0]["intensity_dev"] == pytest.approx(3.0)


def test_transmission_channel_tagged_and_skipped(tmp_path):
    """Channels with no Laser LightSource should be tagged as
    transmission and excluded from the compensation curve so a typical
    1-fluor + 1-DIC acquisition produces a clean dialog."""
    timepoints = [("img0", "2014-02-03T10:26:19")]
    body = _synthetic_ome_xml(timepoints=timepoints)
    # Strip the Laser from the first channel's LightSource so it looks
    # like a transmission/DIC light path. ch1 stays fluorescence.
    body = body.replace(
        '<LightSource ID="LightSource:0:0"><Laser Wavelength="514" '
        'Type="Other" LaserMedium="Other"/></LightSource>',
        '<LightSource ID="LightSource:0:0"><Filament Type="Other"/></LightSource>',
        1,
    )
    dats = tmp_path / "dats"
    dats.mkdir()
    xml = dats / "omxml.xml"
    xml.write_text(body, encoding="utf-8")
    (dats / "imageList.tsv").write_text("time\timage\n1\timg0\n")
    image_list = load_image_list(dats / "imageList.tsv")
    md = parse_ome_xml_as_metadata(xml, image_list=image_list)

    by_ch = {c["channel_name"]: c for c in md.channels if c.get("is_active")}
    assert by_ch["Leica/EYFP"]["channel_type"] == "transmission"
    assert by_ch["Leica/TRITC"]["channel_type"] == "fluorescence"

    # Transmission channels MUST NOT appear in the compensation curve —
    # they have no laser to track.
    curve_channels = {
        c["channel_name"]
        for c in md.depth_compensation.get("channels", [])
    }
    assert "Leica/TRITC" in curve_channels
    assert "Leica/EYFP" not in curve_channels


def test_active_channels_gain_min_max_annotations(tmp_path):
    """When intensity ramps, parser surfaces a min/max range on the
    channel JSON so the GUI's Active-channels table can show it."""
    xml_path = _write_ramped_synthetic(tmp_path)
    image_list = load_image_list(xml_path.parent / "imageList.tsv")
    md = parse_ome_xml_as_metadata(xml_path, image_list=image_list)
    by_ch = {c["channel_name"]: c for c in md.channels if c.get("is_active")}
    eyfp = by_ch["Leica/EYFP"]
    assert eyfp["laser_intensity_dev_min"] == pytest.approx(10.0)
    assert eyfp["laser_intensity_dev_max"] == pytest.approx(20.0)
    # ch1 is flat → min == max.
    tritc = by_ch["Leica/TRITC"]
    assert tritc["laser_intensity_dev_min"] == tritc["laser_intensity_dev_max"]


def test_parse_ome_xml_handles_gzip(tmp_path):
    xml, _ = _write_synthetic(
        tmp_path, [("img0", "2014-02-03T10:26:19")], gzip_it=True
    )
    md = parse_ome_xml_as_metadata(xml, image_name="img0")
    assert md.objective == "HCX PL APO CS"


# ---------------------------------------------------------------------------
# imageList.tsv loader
# ---------------------------------------------------------------------------


def test_load_image_list(tmp_path):
    p = tmp_path / "imageList.tsv"
    p.write_text(
        "time\timage\n"
        "1\tSequence/Job 1_001\n"
        "2\tSequence/Job 1_005\n"
        "3\tSequence/Job 1_009\n"
    )
    out = load_image_list(p)
    assert out == {
        1: "Sequence/Job 1_001",
        2: "Sequence/Job 1_005",
        3: "Sequence/Job 1_009",
    }


def test_load_image_list_missing_returns_empty(tmp_path):
    assert load_image_list(tmp_path / "nope.tsv") == {}


# ---------------------------------------------------------------------------
# SP5 timestamp parser
# ---------------------------------------------------------------------------


def test_sp5_timestamps_basic(tmp_path):
    xml, _ = _write_synthetic(
        tmp_path,
        [
            ("img_001", "2014-02-03T10:26:19"),
            ("img_005", "2014-02-03T10:27:41"),  # +82s
            ("img_009", "2014-02-03T10:29:03"),  # +82s
        ],
        gzip_it=True,
    )
    parser = LeicaSP5OmeXmlTimestampParser()
    assert parser.can_parse(xml)
    result = parser.parse(xml)
    assert result.vendor == "leica_sp5_ome_xml"
    # acquired_at is intentionally None — SP5 OME-XML datetimes are naive.
    assert result.acquired_at is None
    assert len(result.volumes) == 3
    assert result.volumes[0].timepoint == 1
    assert result.volumes[0].absolute_seconds == 0
    assert result.volumes[0].delta_seconds == 0
    assert result.volumes[1].absolute_seconds == 82
    assert result.volumes[1].delta_seconds == 82
    assert result.volumes[2].absolute_seconds == 164
    assert result.volumes[2].delta_seconds == 82


def test_sp5_can_parse_requires_imagelist(tmp_path):
    xml, ilist = _write_synthetic(tmp_path, [("img0", "2014-02-03T10:26:19")])
    parser = LeicaSP5OmeXmlTimestampParser()
    assert parser.can_parse(xml) is True
    # Remove imageList.tsv → parser no longer claims the file.
    ilist.unlink()
    assert parser.can_parse(xml) is False


def test_sp5_carries_delta_over_missing_dates(tmp_path):
    # Three timepoints; the second image has no corresponding <Image> in
    # the OME-XML. Parser should hold the previous delta forward rather
    # than producing a giant negative delta.
    xml_path, _ = _write_synthetic(
        tmp_path,
        [
            ("img_001", "2014-02-03T10:26:19"),
            ("img_009", "2014-02-03T10:29:03"),  # tp 3 in imageList below
        ],
    )
    # Rewrite imageList to claim a 3-tp series where tp 2 references a
    # non-existent Image. The parser must keep tp 3 at the right time.
    (xml_path.parent / "imageList.tsv").write_text(
        "time\timage\n1\timg_001\n2\timg_missing\n3\timg_009\n"
    )
    result = LeicaSP5OmeXmlTimestampParser().parse(xml_path)
    assert len(result.volumes) == 3
    assert result.volumes[0].absolute_seconds == 0
    # tp 2 has no date — first absolute carry: prev_abs(0) + prev_delta(0) = 0
    # The parser then keeps tp 3 at the actual ~164s timestamp.
    assert result.volumes[2].absolute_seconds == 164


def test_sp5_in_registry_via_parse_timestamps_from_dir(tmp_path):
    xml, _ = _write_synthetic(
        tmp_path,
        [
            ("img_001", "2014-02-03T10:26:19"),
            ("img_005", "2014-02-03T10:27:41"),
        ],
        gzip_it=True,
    )
    # Registry should find the file via dir-walk in parse_timestamps_from_dir.
    result = parse_timestamps_from_dir(xml.parent, position=1)
    assert result is not None
    assert result.vendor == "leica_sp5_ome_xml"
    assert len(result.volumes) == 2


# ---------------------------------------------------------------------------
# Microscopy dispatcher
# ---------------------------------------------------------------------------


def test_parse_microscopy_dispatches_to_ome_xml(tmp_path):
    xml, _ = _write_synthetic(tmp_path, [("img0", "2014-02-03T10:26:19")])
    md = parse_microscopy(xml, dats_dir=xml.parent)
    assert md is not None
    assert md.objective == "HCX PL APO CS"


def test_find_metadata_file_prefers_stellaris(tmp_path):
    """Stellaris Properties.xml beats SP5 omxml when both exist in the
    same dats/ (would only happen during a migration; defensive)."""
    dats = tmp_path / "dats"
    dats.mkdir()
    (dats / "acq_Position 1_Properties.xml").write_text("<x/>")
    (dats / "omxml.xml").write_text("<x/>")
    found = find_metadata_file(tmp_path, position=1)
    assert found is not None
    assert found.name.endswith("_Position 1_Properties.xml")


def test_find_metadata_file_falls_back_to_omxml(tmp_path):
    dats = tmp_path / "dats"
    dats.mkdir()
    (dats / "omxml.xml.gz").write_bytes(b"")  # only OME-XML present
    found = find_metadata_file(tmp_path, position=1)
    assert found is not None
    assert found.name == "omxml.xml.gz"


def test_find_metadata_file_returns_none_for_empty_dir(tmp_path):
    (tmp_path / "dats").mkdir()
    assert find_metadata_file(tmp_path, position=1) is None


# ---------------------------------------------------------------------------
# Real fixture sanity check
# ---------------------------------------------------------------------------


_REAL_DATS = Path(
    "/gpfs/fs0/u/azach/images/20140203_sys-1_lit-1i_L1/dats"
)


@pytest.mark.skipif(
    not (_REAL_DATS / "omxml.xml.gz").exists()
    or not (_REAL_DATS / "imageList.tsv").exists(),
    reason="real SP5 fixture not present on this host",
)
def test_real_sp5_fixture_parses():
    omxml = _REAL_DATS / "omxml.xml.gz"
    ilist = _REAL_DATS / "imageList.tsv"
    image_list = load_image_list(ilist)
    # imageList.tsv for this series has ~210 timepoints.
    assert len(image_list) > 50

    md = parse_microscopy(omxml, dats_dir=_REAL_DATS)
    assert md is not None
    assert md.objective == "HCX PL APO CS"
    assert md.acquisition_settings["microscope_model"] == "TCS SP5"
    # SP5 channels: EYFP / TRITC on PMTs.
    dyes = {c["dye_name"] for c in md.channels}
    assert any("EYFP" in d or "TRITC" in d for d in dyes)
    # All channels report a PMT detector for SP5.
    assert all(c["detector_type"] == "PMT" for c in md.channels)

    ts = parse_timestamps(omxml)
    assert ts is not None
    assert ts.vendor == "leica_sp5_ome_xml"
    assert len(ts.volumes) == len(image_list)
    # First tp is anchored at 0; deltas should be ~80s for embryo imaging.
    assert ts.volumes[0].absolute_seconds == 0
    assert 60 <= ts.volumes[1].delta_seconds <= 120
