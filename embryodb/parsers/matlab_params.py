"""Read/write the legacy Matlab StarryNite parameter files.

These live in `/gpfs/fs0/l/murr/parameters/<protocol>` and look like:

    %parameter file for matlab nuclear detection
    conservememory=false;
    slices=67;
    xyres=0.087;
    zres=0.5;
    start_time=1;
    firsttimestepdiam=80;
    parameters.staging=[102,181,251,351,500];
    parameters.intensitythreshold=[0.009,0.0075,...];

Format rules (inferred from the files in the lab parameter dir):
- `%` starts a comment to end of line.
- Statements are `key=value;` separated by newlines.
- Values are scalars, booleans, or `[a,b,c]` literal vectors.
- Keys are either plain identifiers (`xyres`) or dotted
  (`parameters.intensitythreshold`).

We parse into an ordered dict and provide a `render` that reconstructs the
file byte-comparably from {key: raw_value_string} when no edits are made.
For edits (the wizard's per-acquisition overrides), the writer rewrites
just the targeted lines and leaves surrounding comments/whitespace intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_STATEMENT_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*=\s*(.*?);\s*(%.*)?$")


@dataclass
class MatlabParams:
    """Parsed parameter file. `lines` preserves original order + comments so
    we can write back nearly byte-identical when only a few keys change."""

    lines: list[str] = field(default_factory=list)  # original lines, including comments
    values: dict[str, str] = field(default_factory=dict)  # active assignments
    # Per-key index into `lines` for the line that defines it (last assignment wins)
    _line_index: dict[str, int] = field(default_factory=dict)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)

    def get_int(self, key: str) -> int | None:
        v = self.values.get(key)
        if v is None:
            return None
        try:
            return int(v)
        except ValueError:
            return None

    def get_float(self, key: str) -> float | None:
        v = self.values.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    def set(self, key: str, value: str) -> None:
        self.values[key] = value
        new_line = f"{key}={value};"
        if key in self._line_index:
            self.lines[self._line_index[key]] = new_line
        else:
            self.lines.append(new_line)
            self._line_index[key] = len(self.lines) - 1


def parse(text: str) -> MatlabParams:
    out = MatlabParams()
    for i, raw in enumerate(text.splitlines()):
        out.lines.append(raw)
        # Strip leading whitespace to test for "active" lines (`%` first is comment)
        s = raw.lstrip()
        if not s or s.startswith("%"):
            continue
        m = _STATEMENT_RE.match(raw)
        if m is None:
            continue
        key, value = m.group(1), m.group(2).strip()
        out.values[key] = value
        out._line_index[key] = i
    return out


def render(params: MatlabParams) -> str:
    """Render back to text. Preserves order, whitespace, and comments."""
    return "\n".join(params.lines) + "\n"


def load(path: Path) -> MatlabParams:
    return parse(path.read_text(encoding="utf-8"))


# Keys the import wizard surfaces for tuning. Defined here so both the GUI
# and the protocol seeder agree on what's "tunable".
TUNABLE_KEYS: tuple[str, ...] = (
    "start_time",
    "end_time",
    "slices",
    "xyres",
    "zres",
    "firsttimestepdiam",
    "firsttimestepnumcells",
    "parameters.staging",
    "parameters.intensitythreshold",
    "parameters.rangethreshold",
)


def extract_defaults(params: MatlabParams) -> dict[str, str]:
    """Pull the wizard-tunable subset out of a parameter file."""
    return {k: params.values[k] for k in TUNABLE_KEYS if k in params.values}


__all__ = ["MatlabParams", "parse", "render", "load", "TUNABLE_KEYS", "extract_defaults"]
