"""Tests for the Phase 2 extensions to the Leica Stellaris metadata parser.

Focuses on the three high-value buckets surfaced in the
"Microscopy details…" dialog plus the scalar acquisition_settings JSON:

- per-active-channel laser + detector cross-reference (items 1+2)
- depth-compensation curves projected per channel (item 3)
- scalar acquisition settings (items 4-10)

The real fixture under ``embryoDB_test_data/`` is a quick smoke check that
the production XML still parses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from embryodb.parsers.leica_metadata import parse_properties_xml


# ---------------------------------------------------------------------------
# Synthetic fixture builder
# ---------------------------------------------------------------------------


def _stellaris_xml_with_channels(
    *,
    include_compensation: bool = True,
    include_acquisition_settings: bool = True,
) -> str:
    """A minimal Stellaris Properties.xml that exercises the Phase 2 extractors.

    Two active channels (Channel 2 / EGFP / 488 nm / HyD S 2 / gain 100;
    Channel 3 / mCherry / 561 nm / HyD S 3 / gain 30). One inactive Detector
    (Channel 1) to confirm it's filtered out. Optional top-level
    CompensationInterpolationPoints with two Z points, ramping intensity for
    both lasers while holding gain constant.
    """
    setting_attrs = ""
    if include_acquisition_settings:
        setting_attrs = (
            ' VersionNumber="20" BitSize="8" Zoom="3" BaseZoom="0.75"'
            ' ScanMode="xyzt" ScanDirectionXName="Bidirectional"'
            ' FlipX="0" FlipY="1" SwapXY="1" RotatorAngle="0 °"'
            ' Begin="-0.00018" End="-1.47e-04" Sections="67"'
            ' CycleCount="2" CycleTime="90" CompleteTime="21519.99"'
            ' FrameTime="0.148" LineTime="0.000187"'
            ' PixelDwellTime="0.0375 µs" IsResonantScanner="1"'
            ' SystemSerialNumber="8200000242" MicroscopeModel="DMI8-CS"'
            ' Pinhole="154.4 µm" PinholeAiry="1.5 AU"'
            ' ObjectiveName="HC PL APO 63x" NumericalAperture="1.3"'
            ' Immersion="GLYC" RefractionIndex="1.46"'
            ' ScanSpeed="8000 Hz" LineAverage="1" FrameAverage="3"'
        )

    compensation_block = ""
    if include_compensation:
        compensation_block = """
    <CompensationInterpolationPoints>
      <IntensityCompArray>
        <IntensityComp Version="3" ZPosition="-2.38418806480225E-10">
          <Aotf>
            <LaserLineSettingArray>
              <LaserLineSetting LaserLine="405" IntensityDev="0" IsVisible="0"/>
              <LaserLineSetting LaserLine="488" IntensityDev="0.2" IsVisible="1"/>
              <LaserLineSetting LaserLine="561" IntensityDev="2.0" IsVisible="1"/>
            </LaserLineSettingArray>
          </Aotf>
          <Detector>
            <DetectorArray>
              <Detector Name="HyD S 1" Gain="2.5"/>
              <Detector Name="HyD S 2" Gain="100"/>
              <Detector Name="HyD S 3" Gain="30"/>
            </DetectorArray>
          </Detector>
        </IntensityComp>
        <IntensityComp Version="3" ZPosition="-3.3E-05">
          <Aotf>
            <LaserLineSettingArray>
              <LaserLineSetting LaserLine="488" IntensityDev="1.0" IsVisible="1"/>
              <LaserLineSetting LaserLine="561" IntensityDev="18.0" IsVisible="1"/>
            </LaserLineSettingArray>
          </Aotf>
          <Detector>
            <DetectorArray>
              <Detector Name="HyD S 2" Gain="100"/>
              <Detector Name="HyD S 3" Gain="30"/>
            </DetectorArray>
          </Detector>
        </IntensityComp>
      </IntensityCompArray>
    </CompensationInterpolationPoints>"""

    return f"""<?xml version='1.0' encoding='utf-8'?>
<Data><Image>
  <ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green"
       Min="0" Max="255" Unit=""/>
  <ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Red"
       Min="0" Max="255" Unit=""/>
  <ImageDescription>
    <NumberOfChannels>2</NumberOfChannels>
    <ATLConfocalSettings>
      <ATLConfocalSettingDefinition{setting_attrs}>
        <AotfList>
          <Aotf>
            <LaserLineSetting LaserLine="405" IntensityDev="0" IsVisible="0"/>
            <LaserLineSetting LaserLine="488" IntensityDev="0.200353" IsVisible="1"/>
            <LaserLineSetting LaserLine="561" IntensityDev="2.00013" IsVisible="1"/>
            <LaserLineSetting LaserLine="638" IntensityDev="0" IsVisible="0"/>
          </Aotf>
        </AotfList>
        <DetectorList>
          <Detector Name="HyD S 1" Type="SiPM" Channel="1" ChannelName="Channel 1"
                    IsActive="0" Gain="2.5" Offset="0" Band="(410nm - 415nm)"
                    AcquisitionMode="5" IsEnabled="0">
            <DetectionReferenceLine LaserName="" LaserWavelength="0"/>
          </Detector>
          <Detector Name="HyD S 2" Type="SiPM" Channel="2" ChannelName="Channel 2"
                    IsActive="1" Gain="100" Offset="0" DyeName="Leica/EGFP"
                    Band="(493nm - 554nm)" AcquisitionMode="5" IsEnabled="1">
            <DetectionReferenceLine LaserName="OPSL 488" LaserWavelength="488"/>
          </Detector>
          <Detector Name="HyD S 3" Type="SiPM" Channel="3" ChannelName="Channel 3"
                    IsActive="1" Gain="30" Offset="0" DyeName="Leica/mCherry"
                    Band="(567nm - 750nm)" AcquisitionMode="5" IsEnabled="1">
            <DetectionReferenceLine LaserName="DPSS 561" LaserWavelength="561"/>
          </Detector>
        </DetectorList>
        <LaserArray>
          <Laser LaserName="Diode 405" PowerState="Off" Wavelength="405"/>
          <Laser LaserName="OPSL 488" PowerState="On" Wavelength="488"/>
          <Laser LaserName="DPSS 561" PowerState="On" Wavelength="561"/>
        </LaserArray>{compensation_block}
      </ATLConfocalSettingDefinition>
    </ATLConfocalSettings>
  </ImageDescription>
</Image></Data>
"""


# ---------------------------------------------------------------------------
# Active channels — per-channel laser/detector cross-reference (items 1+2)
# ---------------------------------------------------------------------------


def test_active_channels_filter_inactive_detectors(tmp_path):
    p = tmp_path / "foo_Position 1_Properties.xml"
    p.write_text(_stellaris_xml_with_channels())
    md = parse_properties_xml(p)
    active = [c for c in md.channels if c.get("is_active")]
    assert {c["channel_name"] for c in active} == {"Channel 2", "Channel 3"}
    # Inactive Channel 1 (HyD S 1) is not promoted.
    assert all(c["detector_name"] != "HyD S 1" for c in active)


def test_active_channel_cross_references_laser_and_detector(tmp_path):
    p = tmp_path / "foo_Position 1_Properties.xml"
    p.write_text(_stellaris_xml_with_channels())
    md = parse_properties_xml(p)
    by_ch = {c["channel_name"]: c for c in md.channels if c.get("is_active")}
    egfp = by_ch["Channel 2"]
    assert egfp["dye_name"] == "Leica/EGFP"
    assert egfp["detector_name"] == "HyD S 2"
    assert egfp["detector_gain"] == 100.0
    assert egfp["band"] == "(493nm - 554nm)"
    assert egfp["laser_name"] == "OPSL 488"
    assert egfp["laser_line_nm"] == 488
    assert egfp["laser_power_state"] == "On"
    # AOTF intensity comes from the matching LaserLineSetting. We round at
    # storage so the JSON stays clean (raw 0.200353… → 0.2).
    assert egfp["laser_intensity_dev"] == pytest.approx(0.2)
    # AcquisitionMode 5 → "Photon counting" by the lookup table.
    assert egfp["acquisition_mode"] == "Photon counting"


def test_channel_description_fields_merged(tmp_path):
    p = tmp_path / "foo_Position 1_Properties.xml"
    p.write_text(_stellaris_xml_with_channels())
    md = parse_properties_xml(p)
    egfp = next(c for c in md.channels if c.get("channel_name") == "Channel 2")
    # Legacy ChannelDescription fields ride along on the same entry.
    assert egfp["lut_name"] == "Green"
    assert egfp["resolution_bits"] == "8"


# ---------------------------------------------------------------------------
# Depth-compensation curves (item 3)
# ---------------------------------------------------------------------------


def test_depth_compensation_per_channel_projection(tmp_path):
    p = tmp_path / "foo_Position 1_Properties.xml"
    p.write_text(_stellaris_xml_with_channels())
    md = parse_properties_xml(p)
    curves = md.depth_compensation.get("channels", [])
    by_ch = {c["channel_name"]: c for c in curves}
    assert set(by_ch) == {"Channel 2", "Channel 3"}

    ch2 = by_ch["Channel 2"]
    assert ch2["laser_line_nm"] == 488
    assert ch2["detector_name"] == "HyD S 2"
    # Two Z points; both have intensity_dev (for 488 nm) and gain (for HyD S 2).
    # Points are sorted by z (negative first, then less-negative).
    assert len(ch2["points"]) == 2
    z_values = [p["z_um"] for p in ch2["points"]]
    assert z_values == sorted(z_values)
    # Shallow point (z ≈ 0) ramps to deep point (z ≈ -33 µm) — wait, the
    # second compensation point is -3.3e-5 m = -33 µm. After sort ascending:
    # first is z=-33 µm with intensity 1.0; second is z ≈ 0 with 0.2.
    assert ch2["points"][0]["z_um"] < ch2["points"][1]["z_um"]
    assert ch2["points"][0]["intensity_dev"] == 1.0
    assert ch2["points"][1]["intensity_dev"] == pytest.approx(0.2)
    # Gain held flat at 100 across the curve.
    assert all(p["gain"] == 100.0 for p in ch2["points"])

    ch3 = by_ch["Channel 3"]
    assert ch3["detector_name"] == "HyD S 3"
    # Channel 3 intensity ramps 2.0 → 18.0 (the 9× boost the user described).
    assert ch3["points"][0]["intensity_dev"] == 18.0  # deepest z first after sort
    assert ch3["points"][1]["intensity_dev"] == 2.0
    assert all(p["gain"] == 30.0 for p in ch3["points"])


def test_depth_compensation_missing_block_returns_empty(tmp_path):
    p = tmp_path / "foo_Position 1_Properties.xml"
    p.write_text(_stellaris_xml_with_channels(include_compensation=False))
    md = parse_properties_xml(p)
    assert md.depth_compensation == {}


# ---------------------------------------------------------------------------
# Scalar acquisition_settings (items 4-10)
# ---------------------------------------------------------------------------


def test_acquisition_settings_captures_scalars(tmp_path):
    p = tmp_path / "foo_Position 1_Properties.xml"
    p.write_text(_stellaris_xml_with_channels())
    md = parse_properties_xml(p)
    s = md.acquisition_settings
    assert s["bit_size"] == 8
    assert s["zoom"] == 3.0
    assert s["base_zoom"] == 0.75
    assert s["pixel_dwell_time_us"] == 0.0375
    assert s["scan_mode"] == "xyzt"
    assert s["is_resonant_scanner"] is True
    assert s["scan_direction_x"] == "Bidirectional"
    assert s["flip_x"] is False
    assert s["flip_y"] is True
    assert s["swap_xy"] is True
    assert s["rotator_angle_deg"] == 0.0
    # Begin/End: meters → microns conversion.
    assert s["z_begin_um"] == pytest.approx(-180.0)
    assert s["z_end_um"] == pytest.approx(-147.0, abs=1.0)
    assert s["cycle_time_s"] == 90.0
    assert s["complete_time_s"] == pytest.approx(21519.99, rel=1e-3)
    assert s["frame_time_s"] == pytest.approx(0.148, rel=1e-3)
    assert s["system_serial_number"] == "8200000242"
    assert s["microscope_model"] == "DMI8-CS"
    assert s["software_version"] == "20"


_LASAF_SP5_FIXTURE = """<?xml version="1.0"?>
<Data><Image>
<Channels>
  <ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Red"
       Min="0" Max="255" Unit=""/>
  <ChannelDescription DataType="0" ChannelTag="0" Resolution="8" LUTName="Green"
       Min="0" Max="255" Unit=""/>
</Channels>
<HardwareSetting>
  <LDM_Block_Sequential>
    <LDM_Block_Sequential_Master>
      <ATLConfocalSettingDefinition UserSettingName="S54" Begin="2.387e-5" End="5.711e-5" Sections="67">
        <AotfList>
          <Aotf AotfType="Visible">
            <LaserLineSetting LaserLine="488" IntensityDev="0" IsVisible="1" IntensityShow="0.00"/>
            <LaserLineSetting LaserLine="561" IntensityDev="8.99713117" IsVisible="1" IntensityShow="9.00"/>
          </Aotf>
        </AotfList>
        <DetectorList>
          <Detector Channel="1" IsActive="0" Gain="1100" Offset="0" Type="PMT 1" Band="(500nm - 570nm)"/>
          <Detector Channel="2" IsActive="1" Gain="1100" Offset="0" Type="PMT 2" Band="(580nm - 700nm)"/>
        </DetectorList>
        <LaserArray><Laser LaserName="488" Wavelength="488"/><Laser LaserName="561" Wavelength="561"/></LaserArray>
        <CompensationInterpolationPoints/>
        <CompensationDetectorBeginCond>
          <DetectorList><Detector Channel="2" IsActive="1" Gain="1100"/></DetectorList>
        </CompensationDetectorBeginCond>
        <CompensationDetectorEndCond>
          <DetectorList><Detector Channel="2" IsActive="1" Gain="1100"/></DetectorList>
        </CompensationDetectorEndCond>
        <CompensationAotfBeginCond>
          <Aotf><LaserLineSettingArray>
            <LaserLineSetting LaserLine="488" IntensityDev="0"/>
            <LaserLineSetting LaserLine="561" IntensityDev="8.99713117"/>
          </LaserLineSettingArray></Aotf>
        </CompensationAotfBeginCond>
        <CompensationAotfEndCond>
          <Aotf><LaserLineSettingArray>
            <LaserLineSetting LaserLine="488" IntensityDev="0"/>
            <LaserLineSetting LaserLine="561" IntensityDev="8.99713117"/>
          </LaserLineSettingArray></Aotf>
        </CompensationAotfEndCond>
      </ATLConfocalSettingDefinition>
    </LDM_Block_Sequential_Master>
    <LDM_Block_Sequential_List>
      <ATLConfocalSettingDefinition UserSettingName="S56" Begin="2.387e-5" End="5.711e-5" Sections="67">
        <AotfList>
          <Aotf AotfType="Visible">
            <LaserLineSetting LaserLine="488" IntensityDev="4.99908" IsVisible="1" IntensityShow="5.00"/>
            <LaserLineSetting LaserLine="561" IntensityDev="0" IsVisible="1" IntensityShow="0.00"/>
          </Aotf>
        </AotfList>
        <DetectorList>
          <Detector Channel="1" IsActive="1" Gain="1100" Offset="0" Type="PMT 1" Band="(500nm - 570nm)"/>
          <Detector Channel="2" IsActive="0" Gain="0" Offset="0" Type="PMT 2" Band="(580nm - 700nm)"/>
        </DetectorList>
        <LaserArray><Laser LaserName="488" Wavelength="488"/><Laser LaserName="561" Wavelength="561"/></LaserArray>
        <CompensationInterpolationPoints/>
        <CompensationDetectorBeginCond>
          <DetectorList><Detector Channel="1" IsActive="1" Gain="1100"/></DetectorList>
        </CompensationDetectorBeginCond>
        <CompensationDetectorEndCond>
          <DetectorList><Detector Channel="1" IsActive="1" Gain="1100"/></DetectorList>
        </CompensationDetectorEndCond>
        <CompensationAotfBeginCond>
          <Aotf><LaserLineSettingArray>
            <LaserLineSetting LaserLine="488" IntensityDev="2.99700909"/>
            <LaserLineSetting LaserLine="561" IntensityDev="0"/>
          </LaserLineSettingArray></Aotf>
        </CompensationAotfBeginCond>
        <CompensationAotfEndCond>
          <Aotf><LaserLineSettingArray>
            <LaserLineSetting LaserLine="488" IntensityDev="4.99908"/>
            <LaserLineSetting LaserLine="561" IntensityDev="0"/>
          </LaserLineSettingArray></Aotf>
        </CompensationAotfEndCond>
      </ATLConfocalSettingDefinition>
    </LDM_Block_Sequential_List>
  </LDM_Block_Sequential>
</HardwareSetting>
</Image></Data>
"""


def test_lasaf_sp5_multi_atl_finds_both_channels(tmp_path):
    """Sequential-scan LASAF puts each channel's active detector in a
    DIFFERENT ATLConfocalSettingDefinition. The parser must walk all of
    them to surface both."""
    from embryodb.parsers.leica_metadata import parse_properties_xml
    p = tmp_path / "info" / "synth_t001_Properties.xml"
    p.parent.mkdir()
    p.write_text(_LASAF_SP5_FIXTURE)
    md = parse_properties_xml(p)
    active = [c for c in md.channels if c.get("is_active")]
    assert len(active) == 2
    by_ch = {c["channel_number"]: c for c in active}
    # Both channels carry a detector + the right laser line.
    assert by_ch[1]["laser_line_nm"] == 488
    assert by_ch[1]["detector_name"] == "PMT 1"
    assert by_ch[2]["laser_line_nm"] == 561
    assert by_ch[2]["detector_name"] == "PMT 2"


def test_lasaf_sp5_intensity_rounded_and_uses_begin_value(tmp_path):
    """For LASAF SP5 with compensation, ``laser_intensity_dev`` should be
    the begin-of-stack value (3.0 for our fixture) rather than the
    post-compensation top-level AotfList value (5.0). The min/max
    annotation captures the range."""
    from embryodb.parsers.leica_metadata import parse_properties_xml
    p = tmp_path / "info" / "synth_t001_Properties.xml"
    p.parent.mkdir()
    p.write_text(_LASAF_SP5_FIXTURE)
    md = parse_properties_xml(p)
    by_ch = {c["channel_number"]: c for c in md.channels if c.get("is_active")}
    eyfp = by_ch[1]  # 488 channel
    # Begin value of the compensation ramp (3.0%), not the top-level
    # AotfList "current" of 5.0%.
    assert eyfp["laser_intensity_dev"] == pytest.approx(3.0)
    assert eyfp["laser_intensity_dev_min"] == pytest.approx(3.0)
    assert eyfp["laser_intensity_dev_max"] == pytest.approx(5.0)
    # Channel 2 is flat at 9% — no min/max gap.
    tritc = by_ch[2]
    assert tritc["laser_intensity_dev"] == pytest.approx(9.0)
    assert tritc["laser_intensity_dev_min"] == tritc["laser_intensity_dev_max"]


def test_lasaf_sp5_depth_compensation_curve(tmp_path):
    """``CompensationAotfBeginCond`` / ``EndCond`` should produce a 2-point
    Z curve per channel. Duplicate curves (master + sequential pointing
    to the same channel) should be deduped to the widest-spread one."""
    from embryodb.parsers.leica_metadata import parse_properties_xml
    p = tmp_path / "info" / "synth_t001_Properties.xml"
    p.parent.mkdir()
    p.write_text(_LASAF_SP5_FIXTURE)
    md = parse_properties_xml(p)
    curves = md.depth_compensation.get("channels", [])
    by_ch = {c["channel_number"]: c for c in curves}
    # Channel 1 (488) ramps 3 → 5%; Channel 2 (561) is flat 9 → 9%.
    assert by_ch[1]["points"][0]["intensity_dev"] == pytest.approx(3.0)
    assert by_ch[1]["points"][1]["intensity_dev"] == pytest.approx(5.0)
    assert by_ch[2]["points"][0]["intensity_dev"] == pytest.approx(9.0)
    assert by_ch[2]["points"][1]["intensity_dev"] == pytest.approx(9.0)


def test_laser_intensity_rounded_to_one_decimal(tmp_path):
    """Storage layer rounds to 1 decimal so the JSON stays clean
    ('9.0' not '8.99713117255692')."""
    from embryodb.parsers.leica_metadata import parse_properties_xml
    p = tmp_path / "info" / "synth_t001_Properties.xml"
    p.parent.mkdir()
    p.write_text(_LASAF_SP5_FIXTURE)
    md = parse_properties_xml(p)
    for c in md.channels:
        if c.get("laser_intensity_dev") is None:
            continue
        # Round-trip: round(stored, 1) == stored confirms one decimal max.
        stored = c["laser_intensity_dev"]
        assert round(stored, 1) == stored, (
            f"intensity {stored} stored with >1 decimal of precision"
        )


def test_acquisition_settings_drops_empty_values(tmp_path):
    p = tmp_path / "foo_Position 1_Properties.xml"
    p.write_text(_stellaris_xml_with_channels(include_acquisition_settings=False))
    md = parse_properties_xml(p)
    # No attrs → no entries (None/empty drop out so the blob stays compact).
    assert md.acquisition_settings == {}


# ---------------------------------------------------------------------------
# Real fixture sanity check (skipped when not present on the host)
# ---------------------------------------------------------------------------


_REAL_FIXTURE = Path(
    "/murrlab/gpfs/fs0/l/murr/new_tools/embryoDB_test_data/"
    "20250527_JIM783_efl-3_test/"
    "20250527_JIM783_efl-3_test_Position 1_Properties.xml"
)


@pytest.mark.skipif(
    not _REAL_FIXTURE.exists(),
    reason="real Stellaris fixture not present on this host",
)
def test_real_fixture_phase2_extracts():
    md = parse_properties_xml(_REAL_FIXTURE)
    # Active channels: Channel 2 (EGFP / 488) and Channel 3 (mCherry / 561).
    active = [c for c in md.channels if c.get("is_active")]
    assert len(active) == 2
    by_ch = {c["channel_name"]: c for c in active}
    assert by_ch["Channel 2"]["laser_line_nm"] == 488
    assert by_ch["Channel 3"]["laser_line_nm"] == 561
    # Depth compensation produces a per-channel curve for each active channel.
    curves = md.depth_compensation.get("channels", [])
    assert len(curves) == 2
    # Scalar acquisition_settings populated.
    assert md.acquisition_settings.get("system_serial_number")
    assert md.acquisition_settings.get("bit_size") == 8
