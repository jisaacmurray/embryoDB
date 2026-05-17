"""Raw-image filename parser registry.

Microscope filename conventions vary; the legacy Perl pipeline accumulated
if/regex branches for the variants we encountered. This module exposes a
small registry of named parsers. Each parser:

- Returns a `RawImage` for any filename it recognises.
- Returns `None` (or raises `ValueError`) for filenames it doesn't.

`Acquisition.parser_name` records which parser was used, so re-running on
the same source dir is deterministic. New microscope formats become new
entries here without forking the pipeline.

The default parser, `leica_tilescan`, matches the current Leica Stellaris
filenames:

    20250527_JIM783_efl-3_Position 1_t000_z00_RAW_ch00.tif

`Position N` is 1-indexed (positions in the TileScan). `tNNN` and `zNN`
are 0-indexed (we map to 1-indexed `t/p` at staging time, matching the
StarryNite / AceTree convention). `chNN` is 0-indexed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


@dataclass(frozen=True)
class RawImage:
    """One raw plane file from a microscope acquisition."""

    path: Path
    acquisition_stem: str  # the part of the filename before "_Position N"
    position: int  # 1-indexed
    timepoint: int  # 0-indexed (matches the raw filename)
    z_plane: int  # 0-indexed
    raw_channel: int  # 0-indexed (ch00, ch01, …)


@dataclass(frozen=True)
class ParserInfo:
    name: str
    description: str
    extensions: tuple[str, ...]


_PARSERS: dict[str, tuple[ParserInfo, Callable[[Path], RawImage | None]]] = {}


def register(info: ParserInfo, fn: Callable[[Path], RawImage | None]) -> None:
    _PARSERS[info.name] = (info, fn)


def list_parsers() -> list[ParserInfo]:
    return [info for info, _ in _PARSERS.values()]


def parse_one(parser_name: str, path: Path) -> RawImage | None:
    """Apply a named parser to one path. Raises KeyError on unknown parser."""
    return _PARSERS[parser_name][1](path)


def scan_directory(parser_name: str, source: Path) -> Iterable[RawImage]:
    """Yield every recognized raw image under `source` (non-recursive)."""
    fn = _PARSERS[parser_name][1]
    for child in sorted(source.iterdir()):
        if not child.is_file():
            continue
        if not any(child.name.endswith(ext) for ext in _PARSERS[parser_name][0].extensions):
            continue
        img = fn(child)
        if img is not None:
            yield img


# ---------------------------------------------------------------------------
# Default: Leica TileScan
# ---------------------------------------------------------------------------

# Matches:  <stem>_Position <N>_t<NNN>_z<NN>[_RAW]_ch<NN>.tif
# `_RAW` is an optional Leica-export marker some acquisitions include.
_LEICA_RE = re.compile(
    r"^(?P<stem>.+?)_Position\s+(?P<position>\d+)_t(?P<t>\d+)_z(?P<z>\d+)(?:_RAW)?_ch(?P<ch>\d+)\.tif$",
    re.IGNORECASE,
)


def _parse_leica_tilescan(path: Path) -> RawImage | None:
    m = _LEICA_RE.match(path.name)
    if m is None:
        return None
    return RawImage(
        path=path,
        acquisition_stem=m.group("stem"),
        position=int(m.group("position")),
        timepoint=int(m.group("t")),
        z_plane=int(m.group("z")),
        raw_channel=int(m.group("ch")),
    )


register(
    ParserInfo(
        name="leica_tilescan",
        description="Leica Stellaris TileScan: <stem>_Position N_tNNN_zNN[_RAW]_chNN.tif",
        extensions=(".tif", ".TIF", ".tiff", ".TIFF"),
    ),
    _parse_leica_tilescan,
)


__all__ = [
    "RawImage",
    "ParserInfo",
    "register",
    "list_parsers",
    "parse_one",
    "scan_directory",
]
