"""Tests for the vendor-pluggable timestamp parser registry.

The Leica Stellaris parser is the primary target. We exercise:
- the can_parse / parse contract,
- decimation by planes*channels,
- absolute + delta seconds rounding,
- the acquired_at conversion from Windows FILETIME,
- graceful handling of empty / malformed inputs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from embryodb.parsers.timestamps import (
    LeicaStellarisTimestampParser,
    TIMESTAMP_PARSERS,
    TimestampSeries,
    VolumeTime,
    parse_timestamps,
    parse_timestamps_from_dir,
)


# A made-up Stellaris-shaped XML. Three timepoints × 2 planes × 2 channels =
# 12 plane timestamps; the per-volume decimation factor is 4, so timepoints
# land at indices 0, 4, 8.
#
# Hex FILETIMEs (100ns since 1601-01-01 UTC):
#   0x1dbcf207ea07660  = 2025-05-27 16:00:40.79 UTC  (= timepoint 1 start)
#   + 90 seconds       = 0x1dbcf208ad0c1e0           (timepoint 2 ≈ 90s later)
#   + 90 seconds       = 0x1dbcf2096edcc60          (timepoint 3 ≈ 90s later)
# (intermediate plane stamps are just monotonic — exact values don't matter
# because the decimation only ever samples one per volume.)
_FIRST_FT = 0x1DBCF207EA07660
_T2_FT = _FIRST_FT + 90 * 10_000_000  # 90s later
_T3_FT = _FIRST_FT + 180 * 10_000_000  # 180s after t1


def _ft_hex(v: int) -> str:
    # Stellaris writes lowercase hex without 0x; mimic that.
    return format(v, "x")


def _make_stellaris_xml(
    *,
    planes: int,
    channels: int,
    timepoints: int,
    extra_filetimes: list[int] | None = None,
) -> str:
    per_volume = planes * channels
    total = per_volume * timepoints
    fts: list[int] = []
    if extra_filetimes is not None:
        fts = extra_filetimes
    else:
        # Generate one stamp per acquired plane: t1 starts at _FIRST_FT, t2
        # at _T2_FT, t3 at _T3_FT — fill the gaps with monotonic increments
        # 100 µs apart so the decimation can be observed.
        per_tp_starts = [_FIRST_FT, _T2_FT, _T3_FT][:timepoints]
        for tp_start in per_tp_starts:
            for k in range(per_volume):
                fts.append(tp_start + k * 100)  # 100 ticks = 10 µs (irrelevant)
    body = " ".join(_ft_hex(v) for v in fts[:total])
    return f"""<?xml version='1.0' encoding='utf-8'?>
<Data>
  <Image>
    <ImageDescription>
      <Dimensions/>
      <NumberOfChannels>{channels}</NumberOfChannels>
      <ATLConfocalSettings>
        <ATLConfocalSettingDefinition Sections="{planes}" CycleCount="{timepoints}"
            CycleTime="90" ObjectiveName="HC PL APO 63x" NumericalAperture="1.3"
            Pinhole="154.4 µm" ScanSpeed="8000 Hz" LineAverage="1" FrameAverage="3">
        </ATLConfocalSettingDefinition>
      </ATLConfocalSettings>
    </ImageDescription>
    <TimeStampList NumberOfTimeStamps="{total}"
                   FirstTimeStampDate="5/27/2025"
                   FirstTimeStampTime="12:00:40 PM"
                   FirstTimeStampMiliSeconds="790">{body}</TimeStampList>
  </Image>
</Data>
"""


# ---------------------------------------------------------------------------
# can_parse contract
# ---------------------------------------------------------------------------


def test_can_parse_accepts_stellaris_filename(tmp_path):
    p = tmp_path / "20240101_test_Position 3_Properties.xml"
    p.write_text("<Data/>")
    assert LeicaStellarisTimestampParser().can_parse(p)


def test_can_parse_rejects_other_filenames(tmp_path):
    parser = LeicaStellarisTimestampParser()
    assert not parser.can_parse(tmp_path / "Position 3.xml")
    assert not parser.can_parse(tmp_path / "20240101_test_Properties.xml")
    assert not parser.can_parse(tmp_path / "missing_Position 3_Properties.xml")


# ---------------------------------------------------------------------------
# parse contract
# ---------------------------------------------------------------------------


def test_parse_basic_three_timepoints(tmp_path):
    p = tmp_path / "20250527_test_Position 1_Properties.xml"
    p.write_text(_make_stellaris_xml(planes=2, channels=2, timepoints=3))
    result = LeicaStellarisTimestampParser().parse(p)
    assert isinstance(result, TimestampSeries)
    assert result.vendor == "leica_stellaris"
    assert len(result.volumes) == 3
    assert result.volumes[0] == VolumeTime(timepoint=1, absolute_seconds=0, delta_seconds=0)
    assert result.volumes[1] == VolumeTime(timepoint=2, absolute_seconds=90, delta_seconds=90)
    assert result.volumes[2] == VolumeTime(timepoint=3, absolute_seconds=180, delta_seconds=90)


def test_parse_acquired_at_is_utc_from_first_filetime(tmp_path):
    p = tmp_path / "20250527_test_Position 1_Properties.xml"
    p.write_text(_make_stellaris_xml(planes=1, channels=1, timepoints=1))
    result = LeicaStellarisTimestampParser().parse(p)
    # 0x1dbcf207ea07660 = 2025-05-27 16:00:40.790998 UTC (sub-second precision
    # comes from the underlying 100ns tick). We only care about second
    # resolution here.
    assert result.acquired_at is not None
    assert result.acquired_at.tzinfo == timezone.utc
    assert result.acquired_at.replace(microsecond=0) == datetime(
        2025, 5, 27, 16, 0, 40, tzinfo=timezone.utc
    )


def test_parse_decimates_by_planes_times_channels(tmp_path):
    # 67 planes × 2 channels = 134 entries per volume. Make 2 timepoints =
    # 268 entries; ensure timepoint 2 corresponds to entry 134 (not 67).
    p = tmp_path / "20250527_test_Position 2_Properties.xml"
    per_vol = 67 * 2
    fts = [_FIRST_FT + i * 100 for i in range(per_vol)] + [
        _T2_FT + i * 100 for i in range(per_vol)
    ]
    p.write_text(
        _make_stellaris_xml(
            planes=67, channels=2, timepoints=2, extra_filetimes=fts
        )
    )
    result = LeicaStellarisTimestampParser().parse(p)
    assert [v.timepoint for v in result.volumes] == [1, 2]
    assert result.volumes[1].absolute_seconds == 90


def test_parse_empty_timestamplist_returns_empty(tmp_path):
    p = tmp_path / "20250527_test_Position 1_Properties.xml"
    p.write_text(
        _make_stellaris_xml(planes=1, channels=1, timepoints=0, extra_filetimes=[])
    )
    result = LeicaStellarisTimestampParser().parse(p)
    assert result.volumes == []


def test_parse_skips_malformed_hex_tokens(tmp_path):
    # One valid FILETIME, one garbage token, one valid — the parser should
    # keep the valid ones and ignore garbage rather than aborting.
    p = tmp_path / "20250527_test_Position 1_Properties.xml"
    body = f"{_ft_hex(_FIRST_FT)} not-hex {_ft_hex(_T2_FT)}"
    xml = (
        f"<Data><Image>"
        f"<ImageDescription>"
        f"<NumberOfChannels>1</NumberOfChannels>"
        f'<ATLConfocalSettings><ATLConfocalSettingDefinition Sections="1"/>'
        f"</ATLConfocalSettings>"
        f"</ImageDescription>"
        f'<TimeStampList NumberOfTimeStamps="3" FirstTimeStampDate="5/27/2025" '
        f'FirstTimeStampTime="12:00:40 PM" FirstTimeStampMiliSeconds="790">'
        f"{body}</TimeStampList>"
        f"</Image></Data>"
    )
    p.write_text(xml)
    result = LeicaStellarisTimestampParser().parse(p)
    assert [v.absolute_seconds for v in result.volumes] == [0, 90]


# ---------------------------------------------------------------------------
# Registry + directory probe
# ---------------------------------------------------------------------------


def test_parse_timestamps_dispatches_via_registry(tmp_path):
    p = tmp_path / "20250527_test_Position 1_Properties.xml"
    p.write_text(_make_stellaris_xml(planes=1, channels=1, timepoints=2))
    result = parse_timestamps(p)
    assert result is not None
    assert result.vendor == "leica_stellaris"
    assert len(result.volumes) == 2


def test_parse_timestamps_returns_none_for_unrecognized(tmp_path):
    p = tmp_path / "random.txt"
    p.write_text("not xml")
    assert parse_timestamps(p) is None


def test_parse_timestamps_from_dir_picks_matching_position(tmp_path):
    dats = tmp_path / "dats"
    dats.mkdir()
    # Two positions in the same dir; only Position 2 should be picked when
    # requested.
    (dats / "x_Position 1_Properties.xml").write_text(
        _make_stellaris_xml(planes=1, channels=1, timepoints=2)
    )
    (dats / "x_Position 2_Properties.xml").write_text(
        _make_stellaris_xml(planes=1, channels=1, timepoints=3)
    )
    result = parse_timestamps_from_dir(dats, position=2)
    assert result is not None
    assert len(result.volumes) == 3


def test_registry_has_at_least_stellaris():
    assert any(
        p.vendor == "leica_stellaris" for p in TIMESTAMP_PARSERS
    )


# ---------------------------------------------------------------------------
# Real fixture (if available) — confirms the parser handles a production file
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
def test_parse_real_stellaris_fixture():
    result = parse_timestamps(_REAL_FIXTURE)
    assert result is not None
    assert result.vendor == "leica_stellaris"
    # 67 planes × 2 channels × 240 timepoints = 32160 plane timestamps,
    # decimated to 240 volume timestamps.
    assert len(result.volumes) == 240
    assert result.volumes[0].timepoint == 1
    assert result.volumes[0].absolute_seconds == 0
    # Cycle time is configured at 90s; the first delta should be in that ballpark.
    assert 80 <= result.volumes[1].delta_seconds <= 100
    # acquired_at should land in 2025.
    assert result.acquired_at is not None
    assert result.acquired_at.year == 2025
