"""Python port of GetACD.pl / get_acd.R — stdlib only, no pandas/numpy.

Maps per-embryo cell trajectories into the Richards 2013 reference time/space.

Algorithm matches get_acd.R, including:
  - last-row-wins for AuxInfo (Perl overwrite-loop semantics)
  - one-daughter-per-parent (last occurrence wins)
  - trunc() integer arithmetic for time indexing
  - Bug fixes from the R rewrite: unknown-cell guard before arithmetic,
    division-by-zero on zero cycle length, division-by-zero on empty ratios

It deliberately diverges from GetACD.pl and get_acd.R in two places:

  - ``compute_time_correction`` restricts to fully-observed cell cycles and
    takes a median, where both predecessors take a mean over every cell.
  - ``resolve_z_res`` takes the plane spacing from the DB (or AuxInfo's
    ``zpixres``) instead of hardcoding 0.504 um. Both predecessors parse
    ``zpixres`` and then never use it.

See those functions.
"""

from __future__ import annotations

import csv
import math
import statistics
import sys
from pathlib import Path
from typing import NamedTuple

# The reference (Richards 2013) embryo: major/minor axis in pixels, and the
# pixel size of the movie it was measured on. X and Y are normalized onto this
# embryo (`x /= maj / STD_MAJ`) BEFORE the micron conversion, so XY_RES belongs
# to the reference, not to the movie being processed -- 571*0.087 = 49.7 um and
# 348*0.087 = 30.3 um, the real dimensions of a C. elegans embryo. Overriding
# it per-movie would double-count that movie's pixel size.
STD_MAJ = 571
STD_MIN = 348
XY_RES  = 0.087

# Z gets no such normalization: the CD z column is a plane index and is simply
# multiplied through, so this really is an assertion about the movie's plane
# spacing and must come from the data when it is known. Used only as the last
# fallback -- see resolve_z_res().
DEFAULT_Z_RES = 0.504

# Column order when a CD file has no header (positional Perl read)
DEFAULT_COLS = ("cellTime", "cell", "time", "none", "global", "local",
                "blot", "cross", "z", "x", "y", "size", "gweight")


# ---- Reference data --------------------------------------------------------

class DivTimes(NamedTuple):
    birth:    dict[str, float]  # cell → birth time (normalized)
    division: dict[str, float]  # cell → division time (normalized)
    parent:   dict[str, str]
    daughter: dict[str, str]    # parent → one daughter (last-occurrence wins)


def load_division_times(path: Path | str) -> DivTimes:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Division times file not found: {path}")

    birth: dict[str, float]    = {}
    division: dict[str, float] = {}
    parent: dict[str, str]     = {}
    daughter: dict[str, str]   = {}

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="\t")
        next(reader)  # skip header
        for row in reader:
            if len(row) < 5:
                continue
            cell, _, div_norm, par, birth_val = row[0], row[1], row[2], row[3], row[4]
            try:
                birth[cell] = float(birth_val)
            except (ValueError, TypeError):
                pass
            try:
                division[cell] = float(div_norm)
            except (ValueError, TypeError):
                pass
            if par and par not in ("n/a", "NA", ""):
                parent[cell] = par
                daughter[par] = cell  # last occurrence wins

    return DivTimes(birth=birth, division=division, parent=parent, daughter=daughter)


# ---- CSV I/O ---------------------------------------------------------------

Row = dict[str, str]


def _read_csv(path: Path) -> tuple[list[str], list[Row]]:
    """Return (header_cols, rows).  Detects headerless files."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        raw = list(reader)

    if not raw:
        return [], []

    first_val = raw[0][0] if raw[0] else ""
    # Headerless if first value looks like "CellName:time"
    has_header = not (":" in first_val and first_val.split(":")[-1].isdigit())

    if has_header:
        header = raw[0]
        data_rows = raw[1:]
    else:
        ncols = max(len(r) for r in raw)
        if ncols <= len(DEFAULT_COLS):
            header = list(DEFAULT_COLS[:ncols])
        else:
            header = list(DEFAULT_COLS) + [f"col{i}" for i in range(len(DEFAULT_COLS) + 1, ncols + 1)]
        data_rows = raw

    rows = [dict(zip(header, r)) for r in data_rows if r]
    return header, rows


def _write_csv(path: Path, header: list[str], rows: list[Row]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow([row.get(c, "") for c in header])


# ---- Coordinate transform --------------------------------------------------

def _read_auxinfo(path: Path) -> Row:
    """Return the last non-header data row from AuxInfo (Perl overwrite semantics)."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        rows = list(reader)

    header = rows[0] if rows else []
    data = [dict(zip(header, r)) for r in rows[1:] if r
            and not str(r[0]).lower().find("name") >= 0]
    if not data:
        raise ValueError(f"No data rows in AuxInfo: {path}")
    return data[-1]  # last row wins


def resolve_z_res(
    aux: Row,
    voxel_z_um: float | None = None,
    voxel_xy_um: float | None = None,
) -> tuple[float, str]:
    """Pick the plane spacing (um) to convert the CD z index into microns.

    In order: the DB's measured `voxel_z_um`; else AuxInfo's `zpixres` (the
    z/xy spacing *ratio*) scaled by the DB's `voxel_xy_um`; else the legacy
    constant. GetACD.pl parses `zpixres` and then never uses it, so a movie
    acquired at a different step size got silently rescaled -- 1.007 um stacks
    came out half as deep as they really were.
    """
    if voxel_z_um:
        return float(voxel_z_um), "DB voxel_z_um"
    try:
        zpixres = float(aux.get("zpixres", "") or 0)
    except ValueError:
        zpixres = 0.0
    if zpixres and voxel_xy_um:
        return zpixres * float(voxel_xy_um), "AuxInfo zpixres x DB voxel_xy_um"
    return DEFAULT_Z_RES, "default"


def transform_coordinates(
    rows: list[Row],
    auxinfo_path: Path,
    voxel_z_um: float | None = None,
    voxel_xy_um: float | None = None,
) -> list[Row]:
    aux = _read_auxinfo(auxinfo_path)
    z_res, z_src = resolve_z_res(aux, voxel_z_um, voxel_xy_um)
    print(f"  Z plane spacing: {z_res:.4f} um ({z_src})", file=sys.stderr)

    # AuxInfo columns: name,slope,intercept,xc,yc,maj,min,ang,zc,zslope,time,zpixres,axis
    xc     = float(aux.get("xc", 0))
    yc     = float(aux.get("yc", 0))
    maj    = float(aux.get("maj", STD_MAJ))
    min_ax = float(aux.get("min", STD_MIN))
    ang    = float(aux.get("ang", 0))
    zc     = float(aux.get("zc", 0))
    axis   = str(aux.get("axis", "ADL")).strip()
    phi    = math.pi * ang / 180.0
    cos_p, sin_p = math.cos(phi), math.sin(phi)

    if not all("x" in r and "y" in r and "z" in r for r in rows[:1]):
        print("  Warning: x/y/z columns not present; skipping transform", file=sys.stderr)
        return rows

    result = []
    for row in rows:
        x = float(row["x"]); y = float(row["y"]); z = float(row["z"])
        x -= xc; y -= yc; z -= zc
        new_x = x * cos_p - y * sin_p
        new_y = y * cos_p + x * sin_p
        x, y = new_x, new_y
        x /= maj / STD_MAJ
        y /= min_ax / STD_MIN
        x *= XY_RES; y *= XY_RES; z *= z_res
        if axis == "AVR":
            z = -z; y = -y
        elif axis == "PVL":
            x = -x; y = -y
        elif axis == "PDR":
            x = -x; z = -z
        elif axis == "ADL":
            pass
        else:
            print(f"  Warning: unrecognized axis '{axis}' — no flip applied", file=sys.stderr)
        row = dict(row); row["x"] = str(x); row["y"] = str(y); row["z"] = str(z)
        result.append(row)
    return result


# ---- Time correction -------------------------------------------------------

MIN_COMPLETE_CYCLES = 20
SANE_CORRECTION_RANGE = (0.8, 3.0)


def compute_time_correction(rows: list[Row], div_times: DivTimes) -> float:
    """Estimate this movie's timepoints-to-reference-units scale factor.

    Only cells whose cycle was *fully* observed contribute. A cell whose
    division falls past the end of the movie still has a full model-length
    ``standard`` but a truncated ``observed``, so its ratio is inflated
    without bound -- on 20230328_SYS674_tab-1_L3 those cells reached 133 and
    dragged the old mean-of-everything estimate to 8.1 against a true ~1.6.
    Only leaf cells are scaled by this factor (internal branches take their
    span straight from the model), which is why the error showed up as
    terminal branches stretched ~5x too long.
    """
    by_cell: dict[str, list[int]] = {}
    for r in rows:
        by_cell.setdefault(r["cell"], []).append(int(r["time"]))
    observed_cells = set(by_cell)

    complete: list[float] = []
    all_ratios: list[float] = []
    for cell, times in by_cell.items():
        b = div_times.birth.get(cell, math.nan)
        d = div_times.division.get(cell, math.nan)
        standard = d - b
        observed = max(times) - min(times)
        if math.isnan(standard) or math.isnan(b) or standard <= 0 or observed <= 0:
            continue
        all_ratios.append(standard / observed)
        birth_seen = div_times.parent.get(cell) in observed_cells
        division_seen = div_times.daughter.get(cell) in observed_cells
        if birth_seen and division_seen:
            complete.append(standard / observed)

    if len(complete) >= MIN_COMPLETE_CYCLES:
        correction = statistics.median(complete)
    elif all_ratios:
        correction = statistics.median(all_ratios)
        print(
            f"  Warning: only {len(complete)} fully-observed cell cycles "
            f"(need {MIN_COMPLETE_CYCLES}); falling back to the median over all "
            f"{len(all_ratios)} ratios, which is biased high by truncated cycles",
            file=sys.stderr,
        )
    else:
        print("  Warning: no valid time ratios found; using time_correction = 1.0", file=sys.stderr)
        return 1.0

    lo, hi = SANE_CORRECTION_RANGE
    if not lo <= correction <= hi:
        print(
            f"  Warning: time_correction {correction:.3f} is outside the expected "
            f"range [{lo}, {hi}] -- the lineage is probably too short or too "
            f"fragmented for ACD output to be meaningful",
            file=sys.stderr,
        )
    return correction


# ---- Time range for one cell -----------------------------------------------

def _determine_time_range(
    cell: str, div_times: DivTimes,
    obs_min: int, obs_max: int,
    time_correction: float,
    has_daughter: bool, has_parent: bool,
) -> tuple[int, int] | None:
    birth_t    = div_times.birth.get(cell, math.nan)
    division_t = div_times.division.get(cell, math.nan)

    if math.isnan(birth_t):
        return None

    alt_length = max(1, math.trunc(1 + time_correction * (obs_max - obs_min)))

    start_out = birth_t
    end_out   = birth_t

    if not math.isnan(division_t):
        length_std = division_t - birth_t
        end_out    = division_t
        if not has_daughter:
            if alt_length < length_std:
                end_out = birth_t + alt_length
            else:
                print(f"  Note: {cell} should have divided but didn't", file=sys.stderr)
    else:
        end_out = birth_t + alt_length

    if not has_parent and not math.isnan(division_t) and division_t - alt_length > birth_t:
        start_out = division_t - alt_length

    return int(round(start_out)), int(round(end_out))


# ---- Time-warp one cell ----------------------------------------------------

def _time_warp_cell(
    cell_rows: list[Row], cell_name: str,
    start_out: int, end_out: int,
) -> list[Row]:
    cell_rows  = sorted(cell_rows, key=lambda r: int(r["time"]))
    n          = len(cell_rows)
    max_idx    = n - 1
    length_out = end_out - start_out
    result     = []

    for t in range(start_out, end_out + 1):
        if length_out == 0:
            idx = 0
        else:
            idx = min(n - 1, math.trunc(max_idx * (t - start_out) / length_out))
        row = dict(cell_rows[idx])
        row["time"] = str(t)
        row["cell"] = cell_name
        row["cellTime"] = f"{cell_name}:{t}"
        result.append(row)
    return result


# ---- Process one series ----------------------------------------------------

def process_series(
    input_file: Path | str,
    auxinfo_file: Path | str | None,
    div_times: DivTimes,
    voxel_z_um: float | None = None,
    voxel_xy_um: float | None = None,
) -> tuple[list[str], list[Row]] | None:
    """Return (header, rows) for the ACD output, or None if no output produced."""
    input_file = Path(input_file)
    header, rows = _read_csv(input_file)

    # Drop cellTime column from data (will be recomputed)
    if "cellTime" in header:
        header = [c for c in header if c != "cellTime"]
        rows = [{k: v for k, v in r.items() if k != "cellTime"} for r in rows]

    if auxinfo_file is not None:
        print(f"  Applying coordinate transform from: {Path(auxinfo_file).name}",
              file=sys.stderr)
        rows = transform_coordinates(
            rows, Path(auxinfo_file), voxel_z_um, voxel_xy_um
        )

    time_correction = compute_time_correction(rows, div_times)
    print(f"  Time correction factor: {time_correction:.4f}", file=sys.stderr)

    # Group by cell
    by_cell: dict[str, list[Row]] = {}
    for r in rows:
        by_cell.setdefault(r["cell"], []).append(r)

    observed_cells = set(by_cell)
    result_rows: list[Row] = []

    for cell, cell_rows in by_cell.items():
        times = [int(r["time"]) for r in cell_rows]
        obs_min, obs_max = min(times), max(times)

        daughter    = div_times.daughter.get(cell)
        parent_cell = div_times.parent.get(cell)
        has_daughter = daughter is not None and daughter in observed_cells
        has_parent   = parent_cell is not None and parent_cell in observed_cells

        rng = _determine_time_range(
            cell, div_times, obs_min, obs_max,
            time_correction, has_daughter, has_parent,
        )
        if rng is None:
            continue

        result_rows.extend(_time_warp_cell(cell_rows, cell, rng[0], rng[1]))

    if not result_rows:
        print(f"  Warning: no output produced for {input_file}", file=sys.stderr)
        return None

    out_header = ["cellTime"] + header
    return out_header, result_rows
