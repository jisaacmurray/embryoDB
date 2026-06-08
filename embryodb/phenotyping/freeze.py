"""Freeze a Dataset's per-embryo CSVs into a self-contained directory.

This is the embryoDB-native replacement for ``LineagePhenotyping/GetFiles.pl``,
which simply copied ``<annots>/dats/*.csv`` for each series in a list into a
target directory. Here we additionally:

- default the target to a **user-specific** tree
  (``/murrlab3/<user>/phenotyping/<dataset>/``), overridable;
- always include the Sulston reference series, which
  ``CompareDivTime.pl`` / ``build_inputs.R`` regresses every series against;
- regenerate ``TIME<series>.csv`` from the DB when it is missing on disk but
  ``volume_timestamps`` exist, and otherwise record a ``minutes_per_timepoint``
  fallback so frame index can be converted to minutes downstream;
- emit a ready-to-run ``configs/<dataset>.yaml`` for ``run_pipeline.R`` whose
  ``data_dir`` / ``output_dir`` point into the same user-specific tree, a legacy
  list file, and a ``freeze_report.txt`` manifest.

The freeze directory is self-contained: ``build_inputs.R`` then
``run_pipeline.R`` both read and write under it with no relocation.
"""

from __future__ import annotations

import csv
import io
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from .. import fsutil
from ..config import settings
from ..models import Series
from ..queries import datasets as q_datasets
from ..queries import series as q_series

# CompareDivTime.pl regresses every series' observed division times against
# this reference column, so it must be present in every freeze.
REFERENCE_SERIES = "20081128_sulston"

# User-specific default, mirroring embryoDB's image convention
# (/murrlab3/<user>/images/<series>/).
DEFAULT_OUTPUT_ROOT = Path("/murrlab3")

# File-type prefixes we report per series. The freeze copies *all* *.csv, but
# these are the ones build_inputs.R / run_pipeline.R care about.
_FILE_KINDS = ("CD", "ACD", "AuxInfo", "TIME")

# Per-cell expression file that run_pipeline.R reads via ReadPeakExpression.
EXPRESSION_FILENAME = "expression.csv"

# When generating a CA (one-row-per-cell) from a per-timepoint CD/SCD/ACD, look
# for an existing CA in this preference order before falling back to generation.
_EXPRESSION_SOURCE_KINDS = ("CA", "SCA", "SCD", "CD", "ACD")

# Expression/measurement columns collapsed by truncated-mean when generating a
# CA. The real lab CA files store the truncated mean of these over a cell's
# observed timepoints (the CD_to_CA.pl x10 scaling is a stale convention not
# present in current data). Other columns (cellTime/cell/time and the spatial
# x/y/z/size/gweight) are copied from the first/last observed row for parity.
_CA_MEAN_COLS = ("none", "global", "local", "blot", "cross")
_CA_LAST_COLS = ("z", "x", "y", "size", "gweight", "zraw")


@dataclass
class SeriesFreeze:
    """Per-series outcome of the freeze."""

    series_name: str
    annot_loc: str
    is_reference: bool = False
    copied: list[str] = field(default_factory=list)
    kinds_present: set[str] = field(default_factory=set)
    has_time: bool = False
    time_from_db: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def skipped(self) -> bool:
        return self.error is not None


@dataclass
class FreezeReport:
    """Summary of a freeze run, returned to the CLI/GUI for display."""

    dataset_name: str
    target_dir: Path
    config_path: Path
    list_path: Path
    report_path: Path
    series: list[SeriesFreeze]
    reference_included: bool
    minutes_per_timepoint: float | None
    # Per-cell expression (CA) file written into the freeze, if any, plus a
    # human-readable note on where it came from (provided/series/generated).
    expression_path: Path | None = None
    expression_source: str | None = None

    @property
    def n_copied(self) -> int:
        return sum(len(s.copied) for s in self.series)

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        for s in self.series:
            out.extend(f"{s.series_name}: {w}" for w in s.warnings)
            if s.error:
                out.append(f"{s.series_name}: {s.error}")
        return out


def default_output_base(user: str | None = None) -> Path:
    """``/murrlab3/<user>/phenotyping`` — the per-user freeze root."""
    return DEFAULT_OUTPUT_ROOT / (user or settings.user) / "phenotyping"


def freeze_dataset(
    session: Session,
    dataset_name: str,
    output_base: Path | str | None = None,
    minutes_per_timepoint: float | None = None,
    expression_file: Path | str | None = None,
    expression_series: str | None = None,
) -> FreezeReport:
    """Freeze every series of ``dataset_name`` (plus the reference series).

    Parameters
    ----------
    session:
        Open SQLAlchemy session.
    dataset_name:
        Name of an existing Dataset.
    output_base:
        Parent directory for the freeze; the freeze lands in
        ``<output_base>/<dataset_name>/``. Defaults to
        ``/murrlab3/<user>/phenotyping``.
    minutes_per_timepoint:
        Manual frame->minutes conversion for series lacking TIME info. When
        omitted, a value is inferred from the DB's ``volume_timestamps`` deltas
        if any series has them; otherwise it is left unset and the report warns.
    expression_file:
        Path to a per-cell expression (CA) file to use for peak-expression
        annotation. If the file is per-timepoint (a CD/SCD/ACD, i.e. cells
        repeat), it is collapsed to a CA via truncated-mean. Mutually
        exclusive with ``expression_series``; ``expression_file`` wins.
    expression_series:
        Name of a series whose expression should be used. Prefers that
        series' ``CA<series>.csv``; otherwise generates a CA from its
        SCD/CD/ACD. Ignored when ``expression_file`` is given.
    """
    ds = q_datasets.get_by_name(session, dataset_name)
    if ds is None:
        raise q_datasets.DatasetError(f"no dataset named {dataset_name!r}")

    base = Path(output_base) if output_base is not None else default_output_base()
    target_dir = base / dataset_name
    fsutil.ensure_dir(target_dir)

    members = q_series.list_series(session, dataset_id=ds.id)

    # Always include the reference series, even if not a dataset member.
    by_name: dict[str, Series] = {s.series_name: s for s in members}
    reference_included = REFERENCE_SERIES in by_name
    if not reference_included:
        ref = q_series.get_by_name(session, REFERENCE_SERIES)
        if ref is not None:
            by_name[REFERENCE_SERIES] = ref
            reference_included = True

    # Resolve a global minutes_per_timepoint fallback for the YAML.
    mpt = minutes_per_timepoint
    if mpt is None:
        mpt = _infer_minutes_per_timepoint(by_name.values())

    results: list[SeriesFreeze] = []
    for name in sorted(by_name):
        results.append(
            _freeze_one(by_name[name], target_dir, is_reference=(name == REFERENCE_SERIES))
        )

    expr_path, expr_source = _resolve_expression(
        session, target_dir, expression_file, expression_series
    )

    config_path = _write_config(
        target_dir, dataset_name, mpt, expression_rel=(expr_path.name if expr_path else None)
    )
    list_path = _write_list(session, dataset_name, target_dir)
    report = FreezeReport(
        dataset_name=dataset_name,
        target_dir=target_dir,
        config_path=config_path,
        list_path=list_path,
        report_path=target_dir / "freeze_report.txt",
        series=results,
        reference_included=reference_included,
        minutes_per_timepoint=mpt,
        expression_path=expr_path,
        expression_source=expr_source,
    )
    _write_report(report)
    return report


def _freeze_one(series: Series, target_dir: Path, *, is_reference: bool) -> SeriesFreeze:
    sf = SeriesFreeze(
        series_name=series.series_name,
        annot_loc=series.annot_loc,
        is_reference=is_reference,
    )

    if not series.annot_loc:
        sf.error = "no annot_loc set; cannot locate dats/"
        return sf
    dats = Path(series.annot_loc) / "dats"
    if not dats.is_dir():
        sf.error = f"dats directory not found: {dats}"
        return sf

    csvs = sorted(dats.glob("*.csv"))
    kinds = {kind for kind in _FILE_KINDS if (dats / f"{kind}{series.series_name}.csv").exists()}
    sf.kinds_present = set(kinds)

    # CD is the lineage table; without it the series contributes nothing.
    if "CD" not in kinds:
        sf.error = "missing CD file; series skipped"
        return sf

    for src in csvs:
        fsutil.safe_copy(src, target_dir / src.name)
        sf.copied.append(src.name)

    if "ACD" not in kinds:
        sf.warnings.append("missing ACD file; positions cannot be built for this series")

    if "TIME" in kinds:
        sf.has_time = True
    elif series.volume_timestamps:
        # Regenerate TIME<series>.csv from the DB so the freeze is complete.
        fname = f"TIME{series.series_name}.csv"
        fsutil.safe_write_text(target_dir / fname, _time_csv_text(series))
        sf.copied.append(fname)
        sf.kinds_present.add("TIME")
        sf.has_time = True
        sf.time_from_db = True
    else:
        sf.warnings.append(
            "no TIME file and no DB timestamps; build_inputs.R will use "
            "minutes_per_timepoint from the config"
        )

    return sf


def _resolve_expression(
    session: Session,
    target_dir: Path,
    expression_file: Path | str | None,
    expression_series: str | None,
) -> tuple[Path | None, str | None]:
    """Write ``expression.csv`` into the freeze and describe its origin.

    Returns ``(path, source_note)`` or ``(None, None)`` when neither option is
    supplied (run_pipeline.R then falls back to a bundled default).
    """
    if expression_file is not None:
        src = Path(expression_file)
        if not src.is_file():
            raise FileNotFoundError(f"expression file not found: {src}")
        text, collapsed = _expression_text_from_file(src)
        note = f"provided file {src.name}" + (" (collapsed to CA)" if collapsed else "")
    elif expression_series is not None:
        text, note = _expression_text_from_series(session, expression_series)
    else:
        return None, None

    out = target_dir / EXPRESSION_FILENAME
    fsutil.safe_write_text(out, text)
    return out, note


def _expression_text_from_file(src: Path) -> tuple[str, bool]:
    """Load an expression source file, collapsing per-timepoint rows to a CA
    if cells repeat. Returns ``(csv_text, was_collapsed)``."""
    rows = _read_rows(src)
    if rows is None:
        raise ValueError(f"expression file is empty or unreadable: {src}")
    fieldnames, records = rows
    if _is_per_cell(records):
        return src.read_text(), False
    return _collapse_to_ca(fieldnames, records), True


def _expression_text_from_series(session: Session, series_name: str) -> tuple[str, str]:
    """Find a series' expression source on disk and return CA text + a note."""
    series = q_series.get_by_name(session, series_name)
    if series is None:
        raise ValueError(f"no series named {series_name!r}")
    if not series.annot_loc:
        raise ValueError(f"series {series_name!r} has no annot_loc; cannot locate dats/")
    dats = Path(series.annot_loc) / "dats"
    for kind in _EXPRESSION_SOURCE_KINDS:
        cand = dats / f"{kind}{series_name}.csv"
        if cand.is_file():
            text, collapsed = _expression_text_from_file(cand)
            tag = "generated from" if collapsed else "from"
            return text, f"{tag} {kind}{series_name}.csv (series {series_name})"
    raise FileNotFoundError(
        f"series {series_name!r} has no CA/SCA/SCD/CD/ACD file under {dats}"
    )


def _read_rows(src: Path) -> tuple[list[str], list[dict[str, str]]] | None:
    with src.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return None
        return list(reader.fieldnames), list(reader)


def _is_per_cell(records: list[dict[str, str]]) -> bool:
    """A CA file has one row per cell; a CD/SCD/ACD repeats cells over time."""
    seen: set[str] = set()
    for r in records:
        c = r.get("cell")
        if c in seen:
            return False
        if c is not None:
            seen.add(c)
    return True


def _collapse_to_ca(fieldnames: list[str], records: list[dict[str, str]]) -> str:
    """Collapse per-timepoint rows to one row per cell (a CA file).

    The expression/measurement columns become the truncated mean over the
    cell's observed timepoints (matching the lab's CA files); cellTime/cell/time
    come from the cell's first row and the spatial columns from its last row.
    Cell order follows first appearance in the input.
    """
    order: list[str] = []
    grouped: dict[str, list[dict[str, str]]] = {}
    for r in records:
        c = r.get("cell")
        if c is None:
            continue
        if c not in grouped:
            grouped[c] = []
            order.append(c)
        grouped[c].append(r)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for c in order:
        cell_rows = grouped[c]
        first, last = cell_rows[0], cell_rows[-1]
        out = dict(first)  # carries cellTime/cell/time + any non-numeric cols
        for col in _CA_MEAN_COLS:
            if col in fieldnames:
                out[col] = _trunc_mean(cell_rows, col)
        for col in _CA_LAST_COLS:
            if col in fieldnames and col in last:
                out[col] = last[col]
        writer.writerow(out)
    return buf.getvalue()


def _trunc_mean(rows: list[dict[str, str]], col: str) -> str:
    vals: list[float] = []
    for r in rows:
        v = r.get(col)
        if v is None or v == "":
            continue
        try:
            vals.append(float(v))
        except ValueError:
            continue
    if not vals:
        return rows[0].get(col, "") or ""
    return str(math.trunc(sum(vals) / len(vals)))


def _time_csv_text(series: Series) -> str:
    """Legacy TIME<series>.csv body from volume_timestamps (matches
    orchestrate.step_write_time_csv byte-for-byte)."""
    lines = [
        f"{series.series_name}\t{vt.timepoint}\t{vt.absolute_seconds}\t{vt.delta_seconds}"
        for vt in series.volume_timestamps
    ]
    return ("\n".join(lines) + "\n") if lines else ""


def _infer_minutes_per_timepoint(series_iter) -> float | None:
    """Median inter-timepoint delta (minutes) across any series with DB
    timestamps. Returns None when no series has usable timestamps."""
    deltas: list[int] = []
    for s in series_iter:
        for vt in s.volume_timestamps:
            if vt.delta_seconds and vt.delta_seconds > 0:
                deltas.append(vt.delta_seconds)
    if not deltas:
        return None
    return round(statistics.median(deltas) / 60.0, 4)


def _write_config(
    target_dir: Path,
    name: str,
    mpt: float | None,
    expression_rel: str | None = None,
) -> Path:
    """Emit configs/<name>.yaml: a ready-to-run run_pipeline.R config whose
    data_dir/output_dir live under the same user-specific tree."""
    lines = [
        "# Auto-generated by `embryodb phenotyping freeze`.",
        "# Run input-building then analysis from this freeze directory:",
        f"#   Rscript build_inputs.R {target_dir} {name}",
        f"#   Rscript run_pipeline.R {target_dir / 'configs' / (name + '.yaml')}",
        "",
        f"name: {_yaml_str(name)}",
        f"data_dir: {_yaml_str(str(target_dir))}",
        f"output_dir: {_yaml_str(str(target_dir / name))}",
    ]
    if expression_rel is not None:
        lines.append(
            "# Per-cell expression (CA) file, resolved relative to data_dir."
        )
        lines.append(f"expression_file: {_yaml_str(expression_rel)}")
    if mpt is not None:
        lines.append(
            "# Frame-index -> minutes fallback for series lacking TIME info."
        )
        lines.append(f"minutes_per_timepoint: {mpt}")
    lines += [
        "",
        "params:",
        "  expCutoff:     200",
        "  sig:           3",
        "  microns:       5",
        "  posDevTime:    250",
        "  minDivTime:    70",
        "  peak_recalc:   true",
        "  dim_reduction: true",
        "  subdir_layout: by_kind",
        "  defect_trees:  true",
        "",
    ]
    config_path = target_dir / "configs" / f"{name}.yaml"
    fsutil.safe_write_text(config_path, "\n".join(lines))
    return config_path


def _write_list(session: Session, name: str, target_dir: Path) -> Path:
    """Legacy flat list file (one series_name per line) via export_list_file,
    then apply the project's permission discipline."""
    list_path = target_dir / f"{name}.list"
    q_datasets.export_list_file(session, name, list_path)
    fsutil.chmod_if_possible(list_path, fsutil.DEFAULT_FILE_MODE)
    fsutil.chgrp_if_possible(list_path)
    return list_path


def _write_report(report: FreezeReport) -> Path:
    lines = [
        f"Freeze report for dataset: {report.dataset_name}",
        f"Target dir: {report.target_dir}",
        f"Config: {report.config_path}",
        f"List: {report.list_path}",
        f"Reference series ({REFERENCE_SERIES}) included: {report.reference_included}",
        f"minutes_per_timepoint (fallback): "
        f"{report.minutes_per_timepoint if report.minutes_per_timepoint is not None else 'unset'}",
        f"Expression file: "
        f"{report.expression_source if report.expression_source else 'unset (run_pipeline.R uses bundled default)'}",
        f"Files copied: {report.n_copied}",
        "",
        "Per-series manifest (kinds: CD ACD AuxInfo TIME):",
    ]
    for s in report.series:
        flags = []
        for kind in _FILE_KINDS:
            present = kind in s.kinds_present
            if kind == "TIME" and s.time_from_db:
                flags.append("TIME(db)")
            else:
                flags.append(kind if present else f"-{kind}")
        tag = " [reference]" if s.is_reference else ""
        status = "SKIPPED" if s.skipped else f"{len(s.copied)} files"
        lines.append(f"  {s.series_name}{tag}: {status}  [{' '.join(flags)}]")
        for w in s.warnings:
            lines.append(f"      ! {w}")
        if s.error:
            lines.append(f"      ERROR: {s.error}")
    lines.append("")
    fsutil.safe_write_text(report.report_path, "\n".join(lines))
    return report.report_path


def _yaml_str(value: str) -> str:
    """Double-quote a scalar so paths with odd characters stay valid YAML."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
