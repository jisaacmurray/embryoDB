"""Vendor-pluggable parsers for per-timepoint acquisition timestamps.

Each parser turns a microscope-vendor metadata file into a sequence of
absolute volume timestamps. Today only Leica Stellaris (the modern
``<series>_Position N_Properties.xml`` format that ships
``<TimeStampList>`` as hex Windows FILETIME values) is supported; new
vendors plug in by adding a ``TimestampParser`` implementation and
registering it in :data:`TIMESTAMP_PARSERS`.

Replaces the legacy ``ProcessTime.pl`` / ``Process_Time_Stellaris.pl``
shell scripts. Output goes into the ``volume_timestamps`` table at
import time and into per-series ``TIME{series}.csv`` files for downstream
tools that haven't been ported yet (``LineagePhenotyping/CompareDivTime.pl``).

Design notes:
- Each parser is responsible for a single vendor file format. ``can_parse``
  is a cheap probe (filename + maybe a quick peek); ``parse`` does the
  full work and may raise.
- The registry is ordered. Later parsers can shadow earlier ones if
  needed — keep the most-specific (newest format) first.
- Returning an empty ``TimestampSeries.volumes`` is the parser's way of
  saying "this looked like my format but had no usable data" — the
  orchestrator treats that as a soft failure (no rows written, no CSV).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable
from xml.etree import ElementTree as ET


# Windows FILETIME counts 100-nanosecond intervals since 1601-01-01 UTC.
# The Leica Stellaris export embeds these directly in <TimeStampList>.
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_FILETIME_TICKS_PER_SECOND = 10_000_000


def _filetime_to_datetime(ft: int) -> datetime:
    """Convert a Windows FILETIME tick count to an aware UTC datetime."""
    return _FILETIME_EPOCH + timedelta(microseconds=ft // 10)


@dataclass(frozen=True)
class VolumeTime:
    """One volume's worth of acquired time, rounded to the nearest second.

    ``absolute_seconds`` is relative to the first acquired plane (timepoint 1
    == 0). ``delta_seconds`` is the gap from the previous timepoint (0 for
    the first). Integer-rounded so the output matches the legacy
    ``TIME{series}.csv`` columns byte-for-byte for unchanged data.
    """

    timepoint: int
    absolute_seconds: int
    delta_seconds: int


@dataclass
class TimestampSeries:
    """Result of parsing one vendor metadata file.

    ``acquired_at`` is the absolute UTC start of timepoint 1 when the
    vendor format permits it (Stellaris FILETIMEs do; older formats may
    only give us relative seconds in which case this is None).
    """

    vendor: str = ""
    acquired_at: datetime | None = None
    volumes: list[VolumeTime] = field(default_factory=list)


@runtime_checkable
class TimestampParser(Protocol):
    """Interface every vendor timestamp parser implements."""

    vendor: str

    def can_parse(self, path: Path) -> bool:
        """Cheap probe — file existence + name/format check.

        Reading nothing of the file body is ideal; reading the first KB
        is fine. The full parse runs in ``parse``.
        """
        ...

    def parse(self, path: Path) -> TimestampSeries:
        """Parse the file and return a TimestampSeries.

        Raise for unrecoverable format errors. For "looked right but had
        no data" cases (empty TimeStampList, etc.) return a
        TimestampSeries with empty ``volumes`` so the orchestrator can
        log a soft failure rather than abort the whole import.
        """
        ...


# ---------------------------------------------------------------------------
# Leica Stellaris — modern <TimeStampList> with hex FILETIME entries
# ---------------------------------------------------------------------------


class LeicaStellarisTimestampParser:
    """Parses the modern Stellaris ``<series>_Position N_Properties.xml``.

    Timestamps are packed as space-separated hex Windows FILETIME values
    inside ``<TimeStampList NumberOfTimeStamps="..." FirstTimeStampDate="...">``
    — one entry per acquired plane
    (``planes_per_volume × channels_per_plane × n_timepoints`` entries
    total). The per-volume timestamp is every
    ``planes_per_volume × channels_per_plane``-th entry.

    Older Stellaris exports used ``<TimeStamp RelativeTime="..."/>``
    elements per acquired plane (what the legacy
    ``Process_Time_Stellaris.pl`` regex looked for). When we encounter
    one of those we currently return empty volumes — see the SP5-era
    parser (future) for the right handling.
    """

    vendor = "leica_stellaris"

    def can_parse(self, path: Path) -> bool:
        if not path.is_file():
            return False
        name = path.name
        if "_Position " not in name or not name.endswith("_Properties.xml"):
            return False
        return True

    def parse(self, path: Path) -> TimestampSeries:
        tree = ET.parse(path)
        root = tree.getroot()

        # planes_per_volume from the master scan settings.
        setting = root.find(".//ATLConfocalSettingDefinition")
        planes = 1
        if setting is not None:
            sections = setting.get("Sections", "")
            if sections.isdigit():
                planes = int(sections) or 1

        # channels_per_plane from <NumberOfChannels>.
        nch_el = root.find(".//NumberOfChannels")
        channels = 1
        if nch_el is not None and (nch_el.text or "").strip().isdigit():
            channels = int(nch_el.text.strip()) or 1

        ts_el = root.find(".//TimeStampList")
        if ts_el is None or not (ts_el.text or "").strip():
            return TimestampSeries(vendor=self.vendor)

        # Parse the hex FILETIME stream. Tolerate stray non-hex tokens by
        # skipping them — partial data is still useful and the file is
        # otherwise well-formed XML.
        ft_values: list[int] = []
        for tok in (ts_el.text or "").split():
            try:
                ft_values.append(int(tok, 16))
            except ValueError:
                continue
        if not ft_values:
            return TimestampSeries(vendor=self.vendor)

        per_volume = max(planes * channels, 1)
        first_ft = ft_values[0]
        acquired_at = _filetime_to_datetime(first_ft)
        volumes: list[VolumeTime] = []
        prev_abs = 0
        for idx in range(0, len(ft_values), per_volume):
            ft = ft_values[idx]
            seconds = (ft - first_ft) / _FILETIME_TICKS_PER_SECOND
            abs_s = int(round(seconds))
            delta_s = (abs_s - prev_abs) if volumes else 0
            tp = idx // per_volume + 1
            volumes.append(
                VolumeTime(timepoint=tp, absolute_seconds=abs_s, delta_seconds=delta_s)
            )
            prev_abs = abs_s

        return TimestampSeries(
            vendor=self.vendor,
            acquired_at=acquired_at,
            volumes=volumes,
        )


# ---------------------------------------------------------------------------
# Leica SP5 — OME-XML + imageList.tsv companion
# ---------------------------------------------------------------------------


class LeicaSP5OmeXmlTimestampParser:
    """Parses pre-2021 SP5 acquisitions written as ``dats/omxml.xml.gz`` +
    ``dats/imageList.tsv``.

    The legacy lab pipeline split a multi-position TileScan into one
    ``L<N>`` per position. The same OME-XML file is shared across all
    L's of an acquisition; the per-L ``imageList.tsv`` selects the right
    subset of ``<Image>`` elements (timepoint → image name). For each
    timepoint we read that Image's ``<AcquisitionDate>`` and compute
    seconds since timepoint 1.

    SP5's OME-XML AcquisitionDate is naive (no timezone offset) and
    represents acquisition-machine local time. We don't need UTC for the
    relative timing the legacy ``TIME{series}.csv`` cares about, so the
    naive datetimes pass through unchanged into the relative calculation.
    ``acquired_at`` is set to ``None`` (we don't know which timezone
    the SP5 machine was running on) to be honest about the ambiguity.
    """

    vendor = "leica_sp5_ome_xml"

    def can_parse(self, path: Path) -> bool:
        if not path.is_file():
            return False
        name = path.name
        # `omxml.xml` or `omxml.xml.gz` — the lab's pre-2021 export name.
        if name not in ("omxml.xml", "omxml.xml.gz"):
            return False
        # Companion imageList.tsv must be present alongside; without it we
        # don't know which Image elements belong to this L.
        return (path.parent / "imageList.tsv").is_file()

    def parse(self, path: Path) -> TimestampSeries:
        # Import here so the module's hot path doesn't pay for gzip/csv
        # when there are no SP5 files in play.
        from .ome_xml import load_image_list, parse_acquisition_dates

        image_list = load_image_list(path.parent / "imageList.tsv")
        if not image_list:
            return TimestampSeries(vendor=self.vendor)

        # Walk in timepoint order; OME index does ONE pass through the gzip.
        tps_sorted = sorted(image_list.keys())
        names = [image_list[tp] for tp in tps_sorted]
        dates = parse_acquisition_dates(path, names)

        # Anchor at the first timepoint that resolved. Timepoints with a
        # missing or unparseable date hold their previous delta (matches
        # what the legacy ``ProcessTime.pl`` did when a date slot was
        # blank — see the ``$prev + $prevDif`` fallback in that script).
        first_date = next((d for d in dates if d is not None), None)
        if first_date is None:
            return TimestampSeries(vendor=self.vendor)

        volumes: list[VolumeTime] = []
        prev_abs = 0
        prev_delta = 0
        for tp, d in zip(tps_sorted, dates):
            if d is not None:
                abs_s = int(round((d - first_date).total_seconds()))
            else:
                # Carry the previous delta forward (rather than going
                # backwards or repeating an absolute time).
                abs_s = prev_abs + prev_delta
            delta_s = (abs_s - prev_abs) if volumes else 0
            volumes.append(
                VolumeTime(timepoint=tp, absolute_seconds=abs_s, delta_seconds=delta_s)
            )
            prev_abs = abs_s
            prev_delta = delta_s or prev_delta

        return TimestampSeries(
            vendor=self.vendor,
            # acquired_at left None: SP5 OME-XML AcquisitionDate is naive
            # local time on the acquisition machine. We don't store
            # ambiguous timezone-less datetimes as the absolute anchor.
            acquired_at=None,
            volumes=volumes,
        )


# ---------------------------------------------------------------------------
# 2011-era LASAF SP5 — per-timepoint Properties.xml in info/
# ---------------------------------------------------------------------------


class LeicaSP5InfoTimestampParser:
    """Parses ~2011 SP5 acquisitions exported by LASAF where each timepoint
    landed in its own ``info/<series>_t<NNN>_Properties.xml``.

    The lab pipeline keeps an ``info/`` directory parallel to ``dats/``;
    every timepoint has one file with a ``<StartTime>M/D/YYYY h:mm:ss
    AM/PM.mmm</StartTime>`` element giving the absolute clock time the
    acquisition of that timepoint began.

    Detection: ``can_parse`` is given the ``t001`` file as the
    representative entry (the metadata dispatcher hands that over after
    a sibling walk). ``parse`` then walks every ``*_t<NNN>_Properties.xml``
    sibling, sorts by timepoint, and builds the absolute / delta seconds
    sequence relative to whichever timepoint resolved first.

    Like the OME-XML SP5 parser, ``acquired_at`` is left ``None`` because
    LASAF ``<StartTime>`` is acquisition-machine local time with no
    timezone offset.
    """

    vendor = "leica_sp5_info_xml"
    # Match `<series>_t<NNN>_Properties.xml`. Tolerant of N being 1 or
    # more digits (the lab seems to use t001 consistently, but some
    # vendor exports also do _t1 / _t10 / _t100).
    _TP_FILE_RE = __import__("re").compile(r"_t(\d+)_Properties\.xml$")

    def can_parse(self, path: Path) -> bool:
        if not path.is_file():
            return False
        return (
            self._TP_FILE_RE.search(path.name) is not None
            and path.parent.name == "info"
        )

    def parse(self, path: Path) -> TimestampSeries:
        from xml.etree import ElementTree as ET
        from datetime import datetime

        info_dir = path.parent
        # Find every per-tp Properties.xml in the same info/ dir. Sort by
        # the integer timepoint embedded in the filename so missing
        # timepoints don't shift the rest.
        per_tp: list[tuple[int, Path]] = []
        for entry in info_dir.iterdir():
            if not entry.is_file():
                continue
            m = self._TP_FILE_RE.search(entry.name)
            if m is None:
                continue
            per_tp.append((int(m.group(1)), entry))
        if not per_tp:
            return TimestampSeries(vendor=self.vendor)
        per_tp.sort(key=lambda x: x[0])

        # Parse <StartTime> from each. LASAF formats it as
        #   "M/D/YYYY h:mm:ss AM/PM.mmm"
        # which strptime can handle with %p, but the milliseconds suffix
        # is non-standard — strip it before parsing.
        def _parse_start(text: str) -> datetime | None:
            text = text.strip()
            if "." in text:
                text = text.split(".", 1)[0]
            for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
            return None

        starts: list[tuple[int, datetime | None]] = []
        for tp, file in per_tp:
            try:
                root = ET.parse(file).getroot()
            except (ET.ParseError, OSError):
                starts.append((tp, None))
                continue
            # <StartTime> appears under <Image><ImageDescription>; ignore
            # namespace, search by local-name match for robustness across
            # minor schema drifts.
            text_el = next(
                (el for el in root.iter() if el.tag.endswith("StartTime")),
                None,
            )
            if text_el is None or not (text_el.text or "").strip():
                starts.append((tp, None))
                continue
            starts.append((tp, _parse_start(text_el.text)))

        first = next((d for _, d in starts if d is not None), None)
        if first is None:
            return TimestampSeries(vendor=self.vendor)

        volumes: list[VolumeTime] = []
        prev_abs = 0
        prev_delta = 0
        for tp, d in starts:
            if d is not None:
                abs_s = int(round((d - first).total_seconds()))
            else:
                abs_s = prev_abs + prev_delta
            delta_s = (abs_s - prev_abs) if volumes else 0
            volumes.append(
                VolumeTime(
                    timepoint=tp,
                    absolute_seconds=abs_s,
                    delta_seconds=delta_s,
                )
            )
            prev_abs = abs_s
            prev_delta = delta_s or prev_delta

        return TimestampSeries(
            vendor=self.vendor,
            acquired_at=None,
            volumes=volumes,
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Order matters: later parsers can shadow earlier ones. Keep the
# most-specific / most-recent format first so a misfiled match doesn't
# claim a file that a later parser would handle better.
TIMESTAMP_PARSERS: list[TimestampParser] = [
    LeicaStellarisTimestampParser(),
    LeicaSP5OmeXmlTimestampParser(),
    LeicaSP5InfoTimestampParser(),
]


def parse_timestamps(path: Path) -> TimestampSeries | None:
    """Run a file through the parser registry.

    Returns None if no registered parser claims the file. Returns a
    TimestampSeries (possibly with empty ``volumes``) when one does.
    """
    for parser in TIMESTAMP_PARSERS:
        if parser.can_parse(path):
            return parser.parse(path)
    return None


def parse_timestamps_from_dir(
    metadata_dir: Path, position: int
) -> TimestampSeries | None:
    """Find the matching per-position metadata file in ``metadata_dir`` and parse it.

    Convenience for the import orchestrator: given a series's staged
    ``dats/`` directory and the position number, locate and parse the
    relevant vendor file. Returns None if no candidate was found.
    """
    if not metadata_dir.is_dir():
        return None
    # Try each registered parser's filename probe against every entry.
    # This generalises across vendors without committing to a single
    # naming convention.
    for entry in sorted(metadata_dir.iterdir()):
        if not entry.is_file():
            continue
        # Leica's per-position naming includes the position number; bias
        # toward files that match the requested position. Other vendors
        # may not have a position in the filename — they get parsed too
        # if their parser claims the file.
        if "_Position " in entry.name:
            if f"_Position {position}_" not in entry.name and not entry.name.endswith(
                f"_Position {position}_Properties.xml"
            ):
                continue
        result = parse_timestamps(entry)
        if result is not None and result.volumes:
            return result
    return None


__all__ = [
    "LeicaStellarisTimestampParser",
    "TIMESTAMP_PARSERS",
    "TimestampParser",
    "TimestampSeries",
    "VolumeTime",
    "parse_timestamps",
    "parse_timestamps_from_dir",
]
