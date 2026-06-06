"""Typer CLI for embryoDB v1.

All commands route through the same `queries` / `audits` / `importers` /
`exporters` modules the GUI uses, so behavior is consistent between
interfaces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import audits, database
from .config import settings
from .exporters.xml_exporter import export_all, export_series
from .importers.list_importer import import_lists
from .importers.xml_importer import import_dir
from .pipeline.backfill import backfill_directory
from .pipeline.orchestrate import ImportOptions, STEPS, import_acquisition
from .pipeline.protocol_seed import seed_protocols
from .models import Status
from .queries import datasets as q_datasets
from .queries import series as q_series

app = typer.Typer(
    add_completion=False,
    help="embryoDB v1 — safe-mirror CLI",
    no_args_is_help=True,
)
console = Console()


# --- lifecycle ---------------------------------------------------------------


@app.command("init-db")
def init_db() -> None:
    """Create all tables. Idempotent."""
    database.create_all()
    console.print("[green]ok[/green] tables created")


# --- import / export --------------------------------------------------------


@app.command("import-xml")
def import_xml_cmd(
    source: Annotated[
        Path | None,
        typer.Argument(help="Source directory (defaults to EMBRYODB_SOURCE_DIR)"),
    ] = None,
    user: Annotated[str | None, typer.Option("--user", "-u")] = None,
) -> None:
    """Import every XML in `source` into the database. Source is read-only."""
    src = source or settings.source_dir
    console.print(f"importing from [cyan]{src}[/cyan]")
    with database.session_scope() as s:
        report = import_dir(s, source_dir=src, user=user)
    console.print(report.summary())
    if report.drifted:
        console.print("[yellow]drifted (not auto-refreshed):[/yellow]")
        for name, reason in report.drifted[:20]:
            console.print(f"  {name}: {reason}")
    if report.parse_errors:
        console.print("[yellow]parse errors:[/yellow]")
        for path, err in report.parse_errors[:20]:
            console.print(f"  {path}: {err}")


@app.command("export-xml")
def export_xml_cmd(
    target: Annotated[
        str | None,
        typer.Argument(help="Series name or 'all'. Default: all"),
    ] = None,
    out_dir: Annotated[
        Path | None,
        typer.Option("--dir", "-d", help="Export directory (default: EMBRYODB_EXPORT_DIR)"),
    ] = None,
) -> None:
    """Write series back to XML in the export directory. Source dir is never touched."""
    out = out_dir or settings.export_dir
    with database.session_scope() as s:
        if target in (None, "all"):
            report = export_all(s, export_dir=out)
            console.print(f"exported -> [cyan]{out}[/cyan]: {report.summary()}")
        else:
            path = export_series(s, target, export_dir=out)
            console.print(f"wrote [cyan]{path}[/cyan]")


# --- audits ------------------------------------------------------------------


@app.command("audit-import")
def audit_import_cmd(
    source: Annotated[Path | None, typer.Option("--source")] = None,
) -> None:
    """Round-trip every imported series through the exporter and diff against
    source-dir. Source files are not modified."""
    src = source or settings.source_dir
    with database.session_scope() as s:
        report = audits.audit_import(s, source_dir=src)
    console.print(report.summary())
    if report.byte_diffs:
        console.print("[red]byte differences:[/red]")
        for name in report.byte_diffs[:20]:
            console.print(f"  {name}")
    if report.missing_source:
        console.print("[yellow]missing source XML:[/yellow]")
        for name in report.missing_source[:20]:
            console.print(f"  {name}")
    raise typer.Exit(0 if not report.byte_diffs and not report.errors else 1)


@app.command("compare-with-source")
def compare_with_source_cmd(
    series_name: str,
    source: Annotated[Path | None, typer.Option("--source")] = None,
) -> None:
    """Diff one series's current DB representation against its source-dir XML."""
    with database.session_scope() as s:
        matches, generated, source_path = audits.compare_with_source(
            s, series_name, source_dir=source
        )
    if matches:
        console.print(f"[green]match[/green]: {generated} == {source_path}")
        generated.unlink(missing_ok=True)
    else:
        console.print(f"[red]differ[/red]: {generated}  vs  {source_path}")
        console.print(f"  inspect with: diff {generated} {source_path}")
        raise typer.Exit(1)


@app.command("find-duplicates")
def find_duplicates_cmd(
    source: Annotated[Path | None, typer.Option("--source")] = None,
) -> None:
    """Report series_name collisions, case-fold matches, whitespace artifacts,
    and file<->row symmetric differences against source-dir."""
    with database.session_scope() as s:
        report = audits.find_duplicates(s, source_dir=source)
    console.print(report.summary())
    for label, items in [
        ("case-fold", report.case_fold),
        ("whitespace", report.whitespace),
    ]:
        if items:
            console.print(f"[yellow]{label}:[/yellow]")
            for a, b in items[:20]:
                console.print(f"  {a!r} ~ {b!r}")
    if report.file_without_row:
        console.print("[yellow]source files with no DB row:[/yellow]")
        for n in report.file_without_row[:20]:
            console.print(f"  {n}")
    if report.row_without_file:
        console.print("[yellow]DB rows with no source file:[/yellow]")
        for n in report.row_without_file[:20]:
            console.print(f"  {n}")


@app.command("validate-paths")
def validate_paths_cmd(
    dataset: Annotated[str | None, typer.Option("--dataset")] = None,
) -> None:
    """Check that image_loc, annot_loc, and acetree_config point to extant paths."""
    with database.session_scope() as s:
        report = audits.validate_paths(s, dataset_name=dataset)
    console.print(report.summary())
    for label, items in [
        ("image_loc missing", report.image_missing),
        ("annot_loc missing", report.annot_missing),
        ("acetree_config missing", report.config_missing),
    ]:
        if items:
            console.print(f"[yellow]{label}:[/yellow]")
            for name, path in items[:10]:
                console.print(f"  {name}: {path}")


@app.command("find-name-mismatches")
def find_name_mismatches_cmd() -> None:
    """Flag series whose name disagrees with one of its recorded paths.

    Likely cleanup candidates — usually caused by a legacy Java GUI rename
    that didn't propagate to all path fields.
    """
    with database.session_scope() as s:
        report = audits.find_name_mismatches(s)
    console.print(report.summary())
    for label, items in [
        ("image_loc", report.image_loc_mismatches),
        ("annot_loc", report.annot_loc_mismatches),
        ("xml_source", report.xml_source_mismatches),
        ("acetree_config", report.acetree_config_mismatches),
    ]:
        if items:
            console.print(f"[yellow]{label} mismatches:[/yellow]")
            for name, other in items[:20]:
                console.print(f"  {name!r}  vs  {other!r}")
            if len(items) > 20:
                console.print(f"  …and {len(items) - 20} more")


@app.command("migrate-checkedby-anomalies")
def migrate_checkedby_anomalies_cmd(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Actually write changes. Without --apply this is a dry run.",
        ),
    ] = False,
) -> None:
    """Move suspect partial-editing-code values into comments.

    For each row flagged by `find-checkedby-anomalies`, prepends the
    `partial_editing_code` to `comments` with a `[migrated from legacy
    checkedBy]` tag and clears the field. Idempotent. Dry-run by default
    — pass `--apply` to commit the changes.
    """
    with database.session_scope() as s:
        report = audits.migrate_checkedby_anomalies_to_comments(s, dry_run=not apply)
    verb = "would migrate" if not apply else "migrated"
    console.print(f"{verb}: {len(report.migrated)}; skipped: {len(report.skipped)}")
    for name in report.migrated[:25]:
        console.print(f"  {name}")
    if len(report.migrated) > 25:
        console.print(f"  …and {len(report.migrated) - 25} more")
    if not apply and report.migrated:
        console.print(
            "[yellow]dry-run only.[/yellow] Re-run with --apply to commit."
        )


@app.command("backfill-microscopy")
def backfill_microscopy_cmd(
    series_names: Annotated[
        list[str] | None,
        typer.Argument(help="One or more series names. Omit when using --all."),
    ] = None,
    all_eligible: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Process every series whose name is on or after --since. "
                 "Mutually exclusive with explicit series_names.",
        ),
    ] = False,
    since: Annotated[
        str,
        typer.Option(
            "--since",
            help="With --all, only process series with name prefix >= this "
                 "date (YYYY-MM-DD or YYYYMMDD). Default 2021-01-01 "
                 "(Stellaris era; earlier SP5 data uses a different "
                 "metadata format the parser doesn't claim).",
        ),
    ] = "2021-01-01",
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Cap the number of series processed."),
    ] = None,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Commit every N series so a mid-run abort keeps prior "
                 "successes. Default 50.",
        ),
    ] = 50,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="One line per series (default: batched progress + summary).",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would change without writing to the DB.",
        ),
    ] = False,
) -> None:
    """Re-parse each series's ``Position N_Properties.xml`` and update its
    ``MicroscopyMetadata`` row in place.

    Two modes:

    - **Named** — explicit series_names argument(s). Verbose by default;
      good for one-off catch-up after editing the metadata file on disk.
    - **Bulk** — ``--all --since YYYY-MM-DD`` walks every eligible series
      in the corpus. Use ``--dry-run`` first to preview, then commit. The
      command processes in batches (``--batch-size``) and commits each
      batch, so a mid-run failure preserves the work done so far.

    Per-series outcome buckets reported at the end:

    - **updated** — Properties.xml found, parsed, row upserted.
    - **no metadata file** — annot_loc/dats/ has no Position N XML
      (legacy SP5 data, deleted directories, in-progress curation, …).
    - **no annot_loc** — series row has an empty annot_loc.
    - **parse error** — file was found but the parse raised
      (damaged file / unexpected vendor format).
    - **missing row** — explicit series_name doesn't exist in the DB.

    Idempotent. Re-running over the same set is a no-op apart from
    updated_at, thanks to skip-None upsert semantics.
    """
    import re
    from sqlalchemy import select
    from .models import MicroscopyMetadata, Series
    from .parsers.microscopy import find_metadata_file, parse_microscopy

    _LSUFFIX_RE = re.compile(r"_L(\d+)(?:_(?:xx|yy))?$")

    def _position_for(name: str) -> int:
        m = _LSUFFIX_RE.search(name)
        return int(m.group(1)) if m else 1

    def _vendor_for_file(p: Path) -> str:
        """Map metadata-file name → vendor tag stored on MicroscopyMetadata."""
        if p.name in ("omxml.xml", "omxml.xml.gz"):
            return "leica_sp5"
        return "leica_stellaris"

    def _apply_to_row(row: Series, md, raw_path: Path) -> None:
        attrs = (
            ("objective", md.objective),
            ("objective_NA", md.objective_NA),
            ("magnification", md.magnification),
            ("immersion", md.immersion),
            ("refractive_index", md.refractive_index),
            ("voxel_xy_um", md.voxel_xy_um),
            ("voxel_z_um", md.voxel_z_um),
            ("planes_per_volume", md.planes_per_volume),
            ("channels_per_plane", len(md.channels) or None),
            ("x_pixels", md.x_pixels),
            ("y_pixels", md.y_pixels),
            ("n_timepoints", md.n_timepoints),
            ("cycle_time_s", md.cycle_time_s),
            ("pinhole_um", md.pinhole_um),
            ("pinhole_airy", md.pinhole_airy),
            ("scan_speed_hz", md.scan_speed_hz),
            ("line_averaging", md.line_averaging),
            ("frame_averaging", md.frame_averaging),
            ("channels", md.channels),
            ("acquisition_settings", md.acquisition_settings or {}),
            ("depth_compensation", md.depth_compensation or {}),
            ("stage_x_um", md.stage_x_um),
            ("stage_y_um", md.stage_y_um),
            ("stage_z_um", md.stage_z_um),
            ("raw_metadata_path", str(raw_path)),
        )
        if row.microscopy is None:
            row.microscopy = MicroscopyMetadata(vendor=_vendor_for_file(raw_path))
        target = row.microscopy
        for col, val in attrs:
            if val is not None:
                setattr(target, col, val)

    # --- argument validation + schema check ----------------------------

    if (series_names and all_eligible) or (not series_names and not all_eligible):
        console.print(
            "[red]error[/red] specify either explicit series_names OR --all "
            "(not both, not neither)."
        )
        raise typer.Exit(2)

    # Ensure the v2.7 JSON columns exist before we try to write to them.
    # Idempotent — `embryodb-open` does this on every launch.
    database.create_all()

    # --- candidate selection ------------------------------------------

    if all_eligible:
        since_yyyymmdd = since.replace("-", "")
        if not (len(since_yyyymmdd) == 8 and since_yyyymmdd.isdigit()):
            console.print(
                f"[red]error[/red] --since must be YYYY-MM-DD or YYYYMMDD "
                f"(got {since!r})."
            )
            raise typer.Exit(2)
        with database.session_scope() as s:
            # Series names are conventionally `<YYYYMMDD>_<…>` so plain
            # string >= on series_name works. Also require non-empty
            # annot_loc and not soft-deleted.
            stmt = (
                select(Series.series_name)
                .where(Series.series_name >= since_yyyymmdd)
                .where(Series.annot_loc.isnot(None))
                .where(Series.annot_loc != "")
                .where(Series.deleted_at.is_(None))
                .order_by(Series.series_name)
            )
            if limit:
                stmt = stmt.limit(limit)
            names = [r[0] for r in s.execute(stmt).all()]
        console.print(
            f"Selected [bold]{len(names)}[/bold] series with name >= "
            f"{since_yyyymmdd} and annot_loc set."
        )
    else:
        names = list(series_names or [])
        if limit:
            names = names[:limit]

    if not names:
        console.print("[yellow]nothing to do.[/yellow]")
        raise typer.Exit(0)

    # --- counters + per-bucket samples (cap to keep output sane) ------

    counts = {
        "updated": 0,
        "no_metadata_file": 0,
        "no_annot_loc": 0,
        "parse_error": 0,
        "missing_row": 0,
    }
    samples: dict[str, list[str]] = {k: [] for k in counts}

    def _record(bucket: str, name: str, detail: str = "") -> None:
        counts[bucket] += 1
        if len(samples[bucket]) < 25:
            samples[bucket].append(f"{name}" + (f" — {detail}" if detail else ""))

    # --- process in batches with per-batch commits --------------------

    total = len(names)
    processed = 0
    for batch_start in range(0, total, batch_size):
        batch = names[batch_start : batch_start + batch_size]
        with database.session_scope() as session:
            for name in batch:
                row = q_series.get_by_name(session, name)
                if row is None:
                    _record("missing_row", name)
                    if verbose:
                        console.print(f"[red]missing row[/red] {name}")
                    continue
                if not row.annot_loc:
                    _record("no_annot_loc", name)
                    if verbose:
                        console.print(f"[yellow]no annot_loc[/yellow] {name}")
                    continue
                pos = _position_for(name)
                metadata_file = find_metadata_file(Path(row.annot_loc), pos)
                if metadata_file is None:
                    _record(
                        "no_metadata_file",
                        name,
                        f"no Properties.xml or omxml.xml(.gz) under "
                        f"{row.annot_loc}/dats/",
                    )
                    if verbose:
                        console.print(
                            f"[yellow]no file[/yellow] {name} (pos {pos})"
                        )
                    continue
                try:
                    md = parse_microscopy(
                        metadata_file, dats_dir=Path(row.annot_loc) / "dats"
                    )
                except Exception as exc:
                    _record("parse_error", name, f"{type(exc).__name__}: {exc}")
                    if verbose:
                        console.print(f"[red]parse error[/red] {name}: {exc}")
                    continue
                if md is None:
                    # Unknown format — file name passed our directory probe
                    # but no parser claimed it. Treat as "no metadata file".
                    _record(
                        "no_metadata_file",
                        name,
                        f"file {metadata_file.name} found but no parser claimed it",
                    )
                    if verbose:
                        console.print(
                            f"[yellow]no parser[/yellow] {name} for {metadata_file}"
                        )
                    continue

                n_ch = sum(1 for c in (md.channels or []) if c.get("is_active"))
                n_dc = len((md.depth_compensation or {}).get("channels", []))
                n_settings = len(md.acquisition_settings or {})
                if verbose:
                    console.print(
                        f"[cyan]{name}[/cyan]: {n_ch} ch, {n_dc} dc-curve(s), "
                        f"{n_settings} settings key(s) ← {metadata_file}"
                    )
                if not dry_run:
                    _apply_to_row(row, md, metadata_file)
                counts["updated"] += 1
                if len(samples["updated"]) < 25:
                    samples["updated"].append(
                        f"{name} ({n_ch}ch, {n_dc}dc, {n_settings}set)"
                    )
            # End of batch: session_scope commits on exit.
        processed += len(batch)
        if not verbose:
            console.print(
                f"  [dim]progress[/dim] {processed}/{total} "
                f"([green]{counts['updated']} updated[/green], "
                f"[yellow]{counts['no_metadata_file']} no-file[/yellow], "
                f"[red]{counts['parse_error']} errors[/red])"
            )

    # --- summary -------------------------------------------------------

    console.print("")
    if dry_run:
        console.print("[yellow]dry-run only.[/yellow] No DB writes occurred.")
    console.print(
        f"[bold]Summary:[/bold] {processed} processed | "
        f"[green]{counts['updated']} updated[/green] | "
        f"[yellow]{counts['no_metadata_file']} no metadata file[/yellow] | "
        f"[yellow]{counts['no_annot_loc']} no annot_loc[/yellow] | "
        f"[red]{counts['parse_error']} parse errors[/red] | "
        f"[red]{counts['missing_row']} missing rows[/red]"
    )
    for bucket, label in (
        ("parse_error", "Parse errors"),
        ("no_metadata_file", "No metadata file (first 25)"),
        ("no_annot_loc", "No annot_loc (first 25)"),
        ("missing_row", "Missing rows (first 25)"),
    ):
        if samples[bucket]:
            console.print(f"\n[bold]{label}:[/bold]")
            for sname in samples[bucket]:
                console.print(f"  {sname}")
            if counts[bucket] > len(samples[bucket]):
                console.print(
                    f"  …and {counts[bucket] - len(samples[bucket])} more"
                )
    raise typer.Exit(0 if counts["parse_error"] == 0 else 1)


@app.command("emit-time-csv")
def emit_time_csv_cmd(
    series_names: Annotated[
        list[str],
        typer.Argument(help="One or more series names. Use 'all' for every row with timestamps."),
    ],
) -> None:
    """Write ``<annot_loc>/dats/TIME<series>.csv`` from the DB.

    The CSV is what ``LineagePhenotyping/CompareDivTime.pl`` and other
    legacy downstream tools expect. The v2 pipeline writes it
    automatically at import time (via the ``compute_timestamps`` step)
    so this command is for one-off catch-up: re-emit after editing a
    series's metadata path, or back-fill for legacy-imported series
    once their timestamps land in the DB.
    """
    from sqlalchemy import select
    from .models import Series, VolumeTimestamp
    from .pipeline.orchestrate import step_write_time_csv

    with database.session_scope() as s:
        if series_names == ["all"]:
            names = [
                r.series_name
                for r in s.execute(
                    select(Series)
                    .join(VolumeTimestamp, VolumeTimestamp.series_id == Series.id)
                    .distinct()
                ).scalars()
            ]
        else:
            names = list(series_names)

        ok = 0
        skipped: list[str] = []
        for name in names:
            row = q_series.get_by_name(s, name)
            if row is None:
                skipped.append(f"{name} (not found)")
                continue
            if not row.volume_timestamps:
                skipped.append(f"{name} (no timestamps in DB)")
                continue
            if not row.annot_loc:
                skipped.append(f"{name} (no annot_loc)")
                continue
            target = step_write_time_csv(row)
            console.print(f"[green]wrote[/green] {target}")
            ok += 1

    console.print(f"emitted {ok}, skipped {len(skipped)}")
    for line in skipped:
        console.print(f"  {line}")
    raise typer.Exit(0 if ok else 1)


@app.command("sync-legacy-xml")
def sync_legacy_xml_cmd(
    series_names: Annotated[
        list[str] | None,
        typer.Argument(
            help="Series to sync. Omit (or pass 'all') to sync every row."
        ),
    ] = None,
) -> None:
    """Re-write source-dir/<series>.xml from the current DB state.

    Useful for one-off catch-up after manual DB changes or for rebuilding
    the legacy mirror from scratch. Save paths in the GUI auto-sync, so
    this is rarely needed during normal use.
    """
    from sqlalchemy import select
    from .legacy_sync import sync_many
    from .models import Series

    if not series_names or series_names == ["all"]:
        with database.session_scope() as s:
            names = [
                r.series_name
                for r in s.execute(select(Series)).scalars()
            ]
    else:
        names = list(series_names)
    written, failed = sync_many(names)
    console.print(f"[green]synced[/green] {written} / failed {failed}")
    raise typer.Exit(1 if failed else 0)


@app.command("partial")
def partial_cmd(
    series_names: Annotated[
        list[str],
        typer.Argument(help="One or more series names to trim."),
    ],
) -> None:
    """Apply partialCSV trimming to the given series.

    Replaces the legacy ``Partial.pl``: the partial_editing_code is read
    from the database (not the legacy XML), validated, then handed to
    ``partialCSV.jar`` which writes trimmed CD/CA/SCD/SCA CSVs into
    ``<annot_loc>/dats/``. Series whose code is missing/invalid are
    skipped with a message and exit 0 (so the surrounding extract chain
    keeps running).
    """
    from .partial import run_partial_for_series
    worst = 0
    for name in series_names:
        rc = run_partial_for_series(name)
        if rc != 0:
            worst = rc
    raise typer.Exit(worst)


@app.command("find-checkedby-anomalies")
def find_checkedby_anomalies_cmd() -> None:
    """Flag series whose checkedBy (partial editing code) looks malformed.

    The legacy Java EmbryoDB GUI had a bug that occasionally let other
    field content land in this field. Suspect rows are listed for manual
    review; nothing is modified.
    """
    with database.session_scope() as s:
        report = audits.find_checkedby_anomalies(s)
    console.print(report.summary())
    for name, code in report.suspect[:50]:
        # Truncate long values defensively
        shown = code if len(code) <= 80 else code[:77] + "…"
        console.print(f"  {name}  ->  {shown!r}")
    if len(report.suspect) > 50:
        console.print(f"  …and {len(report.suspect) - 50} more")


@app.command("missing-images")
def missing_images_cmd(
    dataset: Annotated[str | None, typer.Option("--dataset")] = None,
) -> None:
    """Alias of validate-paths filtered to image_loc coverage for a dataset."""
    with database.session_scope() as s:
        report = audits.validate_paths(s, dataset_name=dataset)
    console.print(f"image_loc missing: {len(report.image_missing)}")
    for name, path in report.image_missing:
        console.print(f"  {name}: {path}")


@app.command("missing-annots")
def missing_annots_cmd(
    dataset: Annotated[str | None, typer.Option("--dataset")] = None,
) -> None:
    """Alias of validate-paths filtered to annot_loc coverage."""
    with database.session_scope() as s:
        report = audits.validate_paths(s, dataset_name=dataset)
    console.print(f"annot_loc missing: {len(report.annot_missing)}")
    for name, path in report.annot_missing:
        console.print(f"  {name}: {path}")


# --- day-to-day queries -----------------------------------------------------


@app.command("list")
def list_cmd(
    gene: Annotated[list[str] | None, typer.Option("--gene", "-g")] = None,
    person: Annotated[list[str] | None, typer.Option("--person", "-p")] = None,
    status: Annotated[list[Status] | None, typer.Option("--status", "-s")] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="YYYYMMDD lower bound (date_acquired)")
    ] = None,
    text: Annotated[str | None, typer.Option("--text", "-t")] = None,
    limit: int = 50,
) -> None:
    """List series with optional filters."""
    with database.session_scope() as s:
        rows = q_series.list_series(
            s,
            reporter_gene=gene,
            person=person,
            status=status,
            date_after=since,
            text=text,
            text_in_comments_only=False,
            limit=limit,
        )
    table = Table(title=f"series ({len(rows)} rows)")
    for col in ("series_name", "date_acquired", "person", "reporter_gene", "status", "edited_cells"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r.series_name,
            r.date_acquired,
            r.person,
            r.reporter_gene,
            str(r.status.value if hasattr(r.status, "value") else r.status),
            r.edited_cells,
        )
    console.print(table)


@app.command("show")
def show_cmd(series_name: str) -> None:
    """Show all fields for one series."""
    with database.session_scope() as s:
        row = q_series.get_by_name(s, series_name)
    if row is None:
        console.print(f"[red]no series named[/red] {series_name!r}")
        raise typer.Exit(1)
    table = Table(title=row.series_name)
    table.add_column("field"); table.add_column("value")
    fields = [
        "date_acquired", "person", "strain_name", "treatments",
        "reporter_gene", "image_loc", "timepts", "annot_loc",
        "acetree_config", "edited_by", "edited_timepts", "edited_cells",
        "partial_editing_code", "comments", "status",
        "version", "updated_at", "updated_by",
        "xml_source_path", "xml_hash", "imported_at",
    ]
    for f in fields:
        v = getattr(row, f)
        table.add_row(f, str(v.value if hasattr(v, "value") else v))
    console.print(table)


@app.command("stats")
def stats_cmd() -> None:
    """Summary counts by status."""
    with database.session_scope() as s:
        total = q_series.count(s)
        by_status = q_series.count_by_status(s)
    console.print(f"total series: {total}")
    for status, n in sorted(by_status.items(), key=lambda kv: kv[1], reverse=True):
        console.print(f"  {status.value:>14}: {n}")


# --- datasets ---------------------------------------------------------------


datasets_app = typer.Typer(help="dataset (named series collection) operations")
app.add_typer(datasets_app, name="dataset")


@datasets_app.command("create")
def ds_create(
    name: str,
    series: Annotated[list[str] | None, typer.Option("--series", "-s")] = None,
    description: str = "",
) -> None:
    with database.session_scope() as session:
        ds = q_datasets.create(session, name, description=description, series_names=series or [])
        console.print(f"[green]created[/green] dataset {ds.name!r} with {len(ds.series)} series")


@datasets_app.command("list")
def ds_list() -> None:
    with database.session_scope() as session:
        rows = q_datasets.list_datasets(session)
    table = Table(title=f"datasets ({len(rows)})")
    for col in ("name", "size", "description"):
        table.add_column(col)
    for r in rows:
        table.add_row(r.name, str(len(r.series)), r.description)
    console.print(table)


@datasets_app.command("show")
def ds_show(name: str) -> None:
    with database.session_scope() as session:
        ds = q_datasets.get_by_name(session, name)
        if ds is None:
            console.print(f"[red]no dataset named[/red] {name!r}")
            raise typer.Exit(1)
        members = [s.series_name for s in ds.series]
    console.print(f"[bold]{name}[/bold]  ({len(members)} series)")
    for m in members:
        console.print(f"  {m}")


@datasets_app.command("add")
def ds_add(name: str, series: list[str]) -> None:
    with database.session_scope() as session:
        ds = q_datasets.add_series(session, name, series)
        console.print(f"{ds.name}: {len(ds.series)} series")


@datasets_app.command("remove")
def ds_remove(name: str, series: list[str]) -> None:
    with database.session_scope() as session:
        ds = q_datasets.remove_series(session, name, series)
        console.print(f"{ds.name}: {len(ds.series)} series")


@datasets_app.command("import-lists")
def ds_import_lists(
    source: Annotated[
        Path,
        typer.Argument(help="Directory containing flat list files (one series per line)"),
    ] = Path("/gpfs/fs0/l/murr/lists"),
    pattern: Annotated[str, typer.Option("--glob", help="filename glob")] = "*",
) -> None:
    """Bulk-import every flat list file in `source` as a Dataset."""
    with database.session_scope() as session:
        report = import_lists(session, source_dir=source, glob=pattern)
    console.print(report.summary())
    if report.missing_series:
        console.print(
            f"[yellow]{len(report.missing_series)} datasets reference unknown series.[/yellow] "
            "Pass --verbose with `embryodb dataset show <name>` to see details."
        )


@datasets_app.command("export-list")
def ds_export_list(name: str, output: Annotated[Path, typer.Option("--output", "-o")]) -> None:
    """Write a flat list file compatible with /murr/lists/ consumers."""
    with database.session_scope() as session:
        path = q_datasets.export_list_file(session, name, output)
    console.print(f"wrote [cyan]{path}[/cyan]")


@datasets_app.command("export-all")
def ds_export_all(
    target_dir: Annotated[Path, typer.Argument(help="Output directory")],
    suffix: Annotated[str, typer.Option(help="Append to each filename, e.g. '.list'")] = "",
) -> None:
    """Bulk-export every dataset to flat list files in `target_dir`.

    The safe-mirror story for legacy lists: writes go to a staging
    directory you choose (defaults to none beyond what you pass).
    `/gpfs/fs0/l/murr/lists/` stays untouched until you explicitly point
    your downstream scripts at the new location.
    """
    with database.session_scope() as session:
        written = q_datasets.export_all_to_dir(session, target_dir, suffix=suffix)
    console.print(f"wrote {len(written)} list files to [cyan]{target_dir}[/cyan]")


# --- pipeline import (v2) ---------------------------------------------------


pipeline_app = typer.Typer(help="pipeline import (v2) — stage acquisitions and run analysis")
app.add_typer(pipeline_app, name="pipeline")


@pipeline_app.command("seed-protocols")
def pipeline_seed(
    parameters_dir: Annotated[
        Path, typer.Option("--params-dir")
    ] = Path("/gpfs/fs0/l/murr/parameters"),
    prefix: str = "Stellaris_",
) -> None:
    """Seed Protocol rows from /gpfs/fs0/l/murr/parameters/<prefix>*."""
    with database.session_scope() as session:
        report = seed_protocols(session, parameters_dir=parameters_dir, name_prefix=prefix)
    console.print(report.summary())
    for n in report.inserted[:20]:
        console.print(f"  [green]+[/green] {n}")
    for n in report.updated[:5]:
        console.print(f"  [yellow]~[/yellow] {n}")
    for n, why in report.skipped[:5]:
        console.print(f"  [red]-[/red] {n}  ({why})")


@pipeline_app.command("list-protocols")
def pipeline_list_protocols() -> None:
    from sqlalchemy import select
    from .models import Protocol

    with database.session_scope() as session:
        rows = list(session.execute(select(Protocol).order_by(Protocol.name)).scalars())
    table = Table(title=f"protocols ({len(rows)})")
    for col in ("name", "channel_map", "default_timepts", "parameters_file"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r.name,
            str(r.channel_map),
            str(r.default_timepts),
            r.parameters_file_path or "",
        )
    console.print(table)


@pipeline_app.command("import-acquisition")
def pipeline_import_acquisition(
    source: Annotated[Path, typer.Argument(help="Raw acquisition directory")],
    protocol: Annotated[str, typer.Option("--protocol", "-p")],
    image_loc_root: Annotated[
        Path,
        typer.Option(
            "--image-loc-root",
            help="Root directory for staged images (per-user subdir below).",
        ),
    ] = Path("/murrlab3"),
    alias_root: Annotated[
        Path | None,
        typer.Option("--alias-root", help="Symlink root (None to disable)"),
    ] = Path("/murrlab"),
    legacy_xml_dir: Annotated[
        Path | None,
        typer.Option(
            "--legacy-xml-dir",
            help="Where to write the new legacy embryoDB XML (default: source-dir from config)",
        ),
    ] = None,
    user: Annotated[str | None, typer.Option("--user")] = None,
    parser_name: Annotated[str, typer.Option("--parser")] = "leica_tilescan",
    run_through: Annotated[
        str,
        typer.Option(
            "--run-through",
            help=f"Last step to execute. One of: {', '.join(STEPS)}.",
        ),
    ] = "write_matlab_params",
    no_compress: Annotated[bool, typer.Option("--no-compress")] = False,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    person: str = "",
    strain: str = "",
    perturbation: str = "",
    reporter: str = "",
    comments: str = "",
    set_param: Annotated[
        list[str] | None,
        typer.Option(
            "--set-param",
            "-s",
            help="Override a Matlab parameter, e.g. --set-param parameters.intensitythreshold=0.01",
        ),
    ] = None,
) -> None:
    """Import one microscope acquisition end-to-end.

    Each TileScan position becomes a Series. Images are staged with the
    chosen Protocol's channel routing. Per-series Leica metadata, AceTree
    config, embryoDB legacy XML, alias symlink, and matlabParams file are
    all written. Steps beyond --run-through are left as PENDING rows for
    the worker to pick up later.
    """
    from sqlalchemy import select
    from .models import Protocol

    if run_through not in STEPS:
        raise typer.BadParameter(f"--run-through must be one of {STEPS}")

    overrides: dict[str, str] = {}
    for pair in set_param or []:
        if "=" not in pair:
            raise typer.BadParameter(f"--set-param expects k=v, got {pair!r}")
        k, v = pair.split("=", 1)
        overrides[k] = v

    with database.session_scope() as session:
        proto = session.execute(
            select(Protocol).where(Protocol.name == protocol)
        ).scalar_one_or_none()
        if proto is None:
            console.print(f"[red]no protocol named[/red] {protocol!r}")
            raise typer.Exit(1)
        opts = ImportOptions(
            image_loc_root=image_loc_root,
            alias_root=alias_root,
            user=user,
            parameter_overrides=overrides,
            compress_with_lzw=not no_compress,
            overwrite_existing_images=overwrite,
            run_through_step=run_through,
        )
        result = import_acquisition(
            session,
            source_dir=source,
            protocol=proto,
            options=opts,
            person=person,
            strain_name=strain,
            treatments=perturbation,
            reporter_gene=reporter,
            comments=comments,
            legacy_xml_dir=legacy_xml_dir,
        )
    console.print(result.summary())
    for outc in result.series_outcomes:
        st = outc.stage_outcome
        line = (
            f"  [bold]{outc.series_name}[/bold]  -> {outc.image_loc} "
            f"(written={st.written if st else 0} skipped={st.skipped if st else 0})"
        )
        if outc.failed_step:
            line += f"  [red]FAILED at {outc.failed_step}[/red]: {outc.error}"
        console.print(line)


@app.command("lif-inspect")
def lif_inspect_cmd(
    lif_path: Annotated[Path, typer.Argument(help="Path to a .lif file")],
) -> None:
    """List the series + channels inside a Leica .lif file.

    Read-only metadata walk; no pixel data loaded. Use this before
    ``lif-import`` to confirm which series (TileScan) you want to import
    and to verify the channel mapping the protocol will apply.
    """
    from .lif import inspect_lif

    size = lif_path.stat().st_size
    console.print(f"[bold]LIF:[/bold] {lif_path} ({size / 1e9:.1f} GB)")
    series_list = inspect_lif(lif_path)
    for idx, s in enumerate(series_list, start=1):
        p0 = s.positions[0] if s.positions else None
        n_pos = len(s.positions)
        n_tps = p0.n_timepoints if p0 else 0
        n_pl = p0.n_planes if p0 else 0
        size_est = s.estimated_uncompressed_bytes() / 1e9
        cycle = f"{s.cycle_time_s:.0f}s" if s.cycle_time_s else "n/a"
        console.print(
            f"\n[cyan]Series {idx}[/cyan] [bold]{s.name!r}[/bold]: "
            f"{n_pos} pos × {n_tps} tp × {n_pl} planes × {len(s.channels)} ch "
            f"· ~{size_est:.1f} GB uncompressed · cycle {cycle}"
        )
        if s.objective:
            console.print(
                f"  objective {s.objective!r}  scope {s.microscope_model!r}"
            )
        if s.voxel_xy_um:
            console.print(
                f"  voxel  xy={s.voxel_xy_um:.4f} µm  z={s.voxel_z_um:.4f} µm"
            )
        for c in s.channels:
            console.print(
                f"  raw_ch {c.raw_index}: [bold]{c.dye_name or '(no dye)'}[/bold] "
                f"· {c.laser_line_nm} nm · intensity={c.laser_intensity_dev}% "
                f"· detector {c.detector_name!r} · gain={c.detector_gain}"
            )
        if n_pos:
            console.print(
                f"  positions: {', '.join(p.name for p in s.positions)}"
            )


@app.command("extract-lif")
def extract_lif_cmd(
    lif_path: Annotated[Path, typer.Argument(help="Path to a .lif file")],
    out_dir: Annotated[
        Path,
        typer.Argument(help="Output directory (source-dir layout)"),
    ],
    series: Annotated[
        str | None,
        typer.Option(
            "--series",
            help="Series name (e.g. 'TileScan 2'). Required when LIF has >1 series.",
        ),
    ] = None,
    position: Annotated[
        str | None,
        typer.Option(
            "--position",
            help="Position name (e.g. 'Position 1'). Default: all positions.",
        ),
    ] = None,
    stem: Annotated[
        str | None,
        typer.Option(
            "--stem",
            help="Acquisition stem in the output filenames. Default: LIF filename stem.",
        ),
    ] = None,
    bit_depth_policy: Annotated[
        str,
        typer.Option(
            "--bit-depth-policy",
            help="pass | downcast | fail. Default downcast (warns + shifts).",
        ),
    ] = "downcast",
    compress: Annotated[
        bool,
        typer.Option("--compress/--no-compress", help="LZW compression on write"),
    ] = True,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
) -> None:
    """Extract a LIF's per-plane TIFs + Properties.xml to a directory.

    Writes the *source-dir layout* — i.e. ``<stem>_Position N_tNNN_zNN_chNN.tif``
    and ``<stem>_Position N_Properties.xml`` — so the output can be fed
    to ``pipeline import-acquisition`` as if it were a LAS X TIF export.
    No DB rows are created.

    For the common case of "import this LIF straight into embryoDB", use
    ``pipeline import-lif`` instead — it writes to the staged image_loc
    directly, halving the I/O.
    """
    from .lif import LifExtractor

    ex = LifExtractor(lif_path)
    series_list = ex.list_series()
    if series is None:
        if len(series_list) == 1:
            series = series_list[0].name
        else:
            console.print(
                f"[red]LIF has {len(series_list)} series; pass --series to choose:[/red]"
            )
            for s in series_list:
                console.print(f"  {s.name!r}")
            raise typer.Exit(2)
    series_info = next((s for s in series_list if s.name == series), None)
    if series_info is None:
        console.print(f"[red]series {series!r} not found in LIF[/red]")
        raise typer.Exit(2)

    out_stem = stem or lif_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    pos_list = (
        [p for p in series_info.positions if p.name == position]
        if position is not None
        else series_info.positions
    )
    if not pos_list:
        console.print("[yellow]no positions selected[/yellow]")
        raise typer.Exit(0)

    total_planes = 0
    total_skipped = 0
    total_bytes = 0
    from .lif.import_flow import _position_number

    for pos_idx, pos in enumerate(pos_list, start=1):
        # Output position number = the real Leica position number (readlif
        # yields positions out of numeric order), so the source-dir layout
        # this writes matches a LAS X export and feeds import-acquisition
        # with identical position numbering.
        out_pos = _position_number(pos.name, pos_idx)
        nz = pos.n_planes

        def _path_fn(t: int, z: int, raw_ch: int, _out_pos=out_pos) -> Path:
            return out_dir / (
                f"{out_stem}_Position {_out_pos}_t{t:03d}_z{z:02d}_ch{raw_ch:02d}.tif"
            )

        console.print(
            f"[cyan]Position {pos_idx}/{len(pos_list)}[/cyan] "
            f"({pos.name}): {pos.n_timepoints} tp × {nz} z × "
            f"{len(series_info.channels)} ch = "
            f"{pos.n_timepoints * nz * len(series_info.channels)} planes…"
        )
        report = ex.extract_position(
            series, pos.name,
            path_fn=_path_fn,
            bit_depth_policy=bit_depth_policy,
            compress=compress,
            overwrite=overwrite,
        )
        if report.error:
            console.print(f"  [red]ERROR:[/red] {report.error}")
            raise typer.Exit(1)
        console.print(
            f"  wrote {report.planes_written} / skipped {report.planes_skipped}"
            f" · {report.bytes_written / 1e6:.1f} MB"
        )
        if report.bit_depth_warning:
            console.print(f"  [yellow]{report.bit_depth_warning}[/yellow]")
        total_planes += report.planes_written
        total_skipped += report.planes_skipped
        total_bytes += report.bytes_written

        # Write the per-position Properties.xml alongside.
        xml_dst = out_dir / f"{out_stem}_Position {out_pos}_Properties.xml"
        ex.write_position_xml(series, pos.name, xml_dst)

    console.print(
        f"\n[green]done[/green]: wrote {total_planes} planes "
        f"(skipped {total_skipped}) · {total_bytes / 1e9:.2f} GB total"
    )


@pipeline_app.command("import-lif")
def pipeline_import_lif_cmd(
    lif_path: Annotated[Path, typer.Argument(help="Path to a .lif file")],
    protocol: Annotated[str, typer.Option("--protocol", "-p")],
    series: Annotated[
        str | None,
        typer.Option(
            "--series",
            help="Which top-level series (TileScan) to import. "
                 "Required when the LIF has >1 series.",
        ),
    ] = None,
    image_loc_root: Annotated[
        Path,
        typer.Option("--image-loc-root"),
    ] = Path("/murrlab3"),
    alias_root: Annotated[
        Path | None,
        typer.Option("--alias-root"),
    ] = Path("/murrlab"),
    legacy_xml_dir: Annotated[
        Path | None,
        typer.Option("--legacy-xml-dir"),
    ] = None,
    user: Annotated[str | None, typer.Option("--user")] = None,
    run_through: Annotated[
        str,
        typer.Option(
            "--run-through",
            help=f"Last step to execute. One of: {', '.join(STEPS)}.",
        ),
    ] = "write_matlab_params",
    position: Annotated[
        list[str] | None,
        typer.Option(
            "--position",
            help="Position name(s) to import. Default: all in the series.",
        ),
    ] = None,
    channel_role: Annotated[
        list[str] | None,
        typer.Option(
            "--channel-role",
            help="Override channel role: e.g. '0=histone' (raw_ch=role). "
                 "Repeatable. Defaults to the Protocol's channel_map.",
        ),
    ] = None,
    bit_depth_policy: Annotated[
        str, typer.Option("--bit-depth-policy")
    ] = "downcast",
    no_compress: Annotated[bool, typer.Option("--no-compress")] = False,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y",
            help="Skip the channel-mapping confirmation prompt.",
        ),
    ] = False,
    person: str = "",
    strain: str = "",
    perturbation: str = "",
    reporter: str = "",
    comments: str = "",
    set_param: Annotated[
        list[str] | None, typer.Option("--set-param", "-s")
    ] = None,
) -> None:
    """Import a LIF file end-to-end (extract directly to staged layout + run pipeline).

    Each position in the chosen series becomes a Series row. Per-plane
    TIFs land in ``<image_loc_root>/<user>/images/<series>_L<N>/<role>/``
    with LZW compression and Z-inversion (deepest plane = p01). The
    sliced Properties.xml goes to ``<…>/dats/``. The orchestrator's
    inline steps (stage_metadata, compute_timestamps, write_acetree_config,
    write_embryodb_xml, create_alias_symlink, write_matlab_params) run
    against the already-staged files.

    The TIF write loop is the slow part (~30 min for a 240-tp embryo on
    NFS). Run via the GUI dialog or behind ``nohup`` if you need it to
    survive an SSH disconnect.
    """
    from sqlalchemy import select
    from .lif import LifExtractor
    from .lif.import_flow import import_lif
    from .models import Protocol

    if run_through not in STEPS:
        raise typer.BadParameter(f"--run-through must be one of {STEPS}")

    overrides: dict[str, str] = {}
    for pair in set_param or []:
        if "=" not in pair:
            raise typer.BadParameter(f"--set-param expects k=v, got {pair!r}")
        k, v = pair.split("=", 1)
        overrides[k] = v

    channel_role_override: dict[int, str] = {}
    for spec in channel_role or []:
        if "=" not in spec:
            raise typer.BadParameter(
                f"--channel-role expects raw_ch=role, got {spec!r}"
            )
        raw, role = spec.split("=", 1)
        try:
            channel_role_override[int(raw)] = role.strip()
        except ValueError:
            raise typer.BadParameter(f"raw_ch must be an integer, got {raw!r}")

    # Inspect first so the user sees what they're about to import.
    ex = LifExtractor(lif_path)
    series_list = ex.list_series()
    if series is None:
        if len(series_list) == 1:
            series = series_list[0].name
        else:
            console.print(
                f"[red]LIF has {len(series_list)} series; pass --series:[/red]"
            )
            for s in series_list:
                p0 = s.positions[0] if s.positions else None
                console.print(
                    f"  {s.name!r}  ({len(s.positions)} pos × "
                    f"{p0.n_timepoints if p0 else 0} tp)"
                )
            raise typer.Exit(2)
    series_info = next((s for s in series_list if s.name == series), None)
    if series_info is None:
        console.print(f"[red]series {series!r} not found[/red]")
        raise typer.Exit(2)

    # Confirmation: display channel mapping + disk estimate so a
    # misconfigured Protocol or channel_role override is visible BEFORE
    # the multi-hour extraction.
    with database.session_scope() as session:
        proto = session.execute(
            select(Protocol).where(Protocol.name == protocol)
        ).scalar_one_or_none()
        if proto is None:
            console.print(f"[red]no protocol named {protocol!r}[/red]")
            raise typer.Exit(1)
        # Build effective channel map for display.
        eff_map: dict[int, str] = {}
        for raw_idx_str, role in (proto.channel_map or {}).items():
            try:
                eff_map[int(raw_idx_str)] = role
            except ValueError:
                continue
        eff_map.update(channel_role_override)

    console.print(
        f"\n[bold]Importing[/bold] {lif_path}  series={series!r}\n"
        f"  positions:  {len(series_info.positions)}"
        + (f" (filtered to {len(position)})" if position else "")
    )
    console.print(
        f"  per-pos:    {series_info.positions[0].n_timepoints} tp × "
        f"{series_info.positions[0].n_planes} planes × "
        f"{len(series_info.channels)} ch  "
        f"= {series_info.positions[0].n_timepoints * series_info.positions[0].n_planes * len(series_info.channels):,} planes"
    )
    console.print(
        f"  est size:   ~{series_info.estimated_uncompressed_bytes() / 1e9:.1f} GB "
        f"uncompressed (LZW typically 30-50% of that)\n"
    )
    console.print(f"[bold]Channel mapping[/bold] (from Protocol {protocol!r}):")
    for c in series_info.channels:
        role = eff_map.get(c.raw_index, "(unmapped → tifC?)")
        # Heuristic warning: histone is usually the longer wavelength.
        warn = ""
        if (
            role == "histone" and c.laser_line_nm is not None
            and c.laser_line_nm < 500
        ):
            warn = "  [yellow]· note: histone on a short-wavelength channel[/yellow]"
        elif (
            role == "reporter" and c.laser_line_nm is not None
            and c.laser_line_nm >= 560
        ):
            warn = "  [yellow]· note: reporter on a long-wavelength channel[/yellow]"
        console.print(
            f"  raw_ch {c.raw_index}: {c.dye_name or '(no dye)'} "
            f"· {c.laser_line_nm} nm  →  [bold]{role}[/bold]{warn}"
        )
    console.print(
        f"\n[bold]Acquisition:[/bold]  person={person!r}  strain={strain!r}  "
        f"perturbation={perturbation!r}  reporter={reporter!r}"
    )

    if not yes:
        console.print(
            "\n[bold]Proceed?[/bold] (extraction can take hours; the staged "
            "files are idempotent so it's safe to rerun.)  [y/N]: ",
            end="",
        )
        ans = input().strip().lower()
        if ans not in ("y", "yes"):
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(0)

    # Run the import.
    with database.session_scope() as session:
        proto = session.execute(
            select(Protocol).where(Protocol.name == protocol)
        ).scalar_one()
        opts = ImportOptions(
            image_loc_root=image_loc_root,
            alias_root=alias_root,
            user=user,
            parameter_overrides=overrides,
            compress_with_lzw=not no_compress,
            overwrite_existing_images=overwrite,
            run_through_step=run_through,
        )
        result = import_lif(
            session,
            lif_path=lif_path,
            series_name=series,
            protocol=proto,
            options=opts,
            person=person,
            strain_name=strain,
            treatments=perturbation,
            reporter_gene=reporter,
            comments=comments,
            legacy_xml_dir=legacy_xml_dir,
            positions=position,
            channel_role_override=channel_role_override,
            bit_depth_policy=bit_depth_policy,
            compress=not no_compress,
            on_progress=lambda sname, done, total: print(
                f"  {sname}: {done}/{total} planes", flush=True
            ) if done and done % 100 == 0 else None,
        )
    console.print(f"\n{result.summary()}")
    for outc in result.series_outcomes:
        st = outc.stage_outcome
        line = (
            f"  [bold]{outc.series_name}[/bold] -> {outc.image_loc} "
            f"(written={st.written if st else 0} skipped={st.skipped if st else 0})"
        )
        if outc.failed_step:
            line += f"  [red]FAILED at {outc.failed_step}[/red]: {outc.error}"
        console.print(line)


@pipeline_app.command("worker")
def pipeline_worker_cmd() -> None:
    """Start the per-machine background worker (exits when queue is empty).

    The worker picks up PENDING run_starrynite / run_red_extract / run_measure
    rows in series order and executes them sequentially. It is crash-safe: rows
    left RUNNING with a stale heartbeat are requeued on startup.
    """
    from .pipeline.worker import run_worker, worker_is_running
    if worker_is_running():
        console.print("[yellow]worker already running for this host — nothing to do[/yellow]")
        return
    console.print("[green]worker starting…[/green]")
    run_worker()
    console.print("[green]worker exited (queue empty)[/green]")


@pipeline_app.command("backfill")
def pipeline_backfill_cmd(
    root: Annotated[
        Path,
        typer.Argument(
            help="Image-loc parent directory, e.g. /murrlab3/<user>/images/"
        ),
    ],
    no_create_acquisitions: Annotated[
        bool,
        typer.Option(
            "--no-create-acquisitions",
            help="Skip series whose Acquisition row doesn't already exist.",
        ),
    ] = False,
    no_microscopy: Annotated[bool, typer.Option("--no-microscopy")] = False,
) -> None:
    """Backfill Acquisition/Series/PipelineStepRun rows from on-disk state.

    Walks `root` for series directories (`<name>_L<N>`) that already exist
    on disk, links them to inferred Acquisition rows, and records per-step
    status based on what files are present. Read-only against image data.
    """
    with database.session_scope() as session:
        report = backfill_directory(
            session,
            root,
            create_unknown_acquisitions=not no_create_acquisitions,
            attach_microscopy=not no_microscopy,
        )
    console.print(report.summary())
    for n in report.acquisitions_created[:10]:
        console.print(f"  [green]+acq[/green] {n}")
    for n in report.series_unmatched[:10]:
        console.print(f"  [yellow]?[/yellow] {n} (no matching Series row)")


# --- deletion garbage collection --------------------------------------------


@app.command("gc-deleted")
def gc_deleted_cmd(
    older_than: Annotated[
        int,
        typer.Option(
            "--older-than",
            help="Days since deleted_at after which image data is purged.",
        ),
    ] = 30,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Actually delete. Without --apply this is a dry run.",
        ),
    ] = False,
) -> None:
    """Permanently delete image data for series marked for deletion long ago.

    Removes only the bulky image directories (tif/, tifR/, DIC/, tifC*) under
    each affected series' image_loc. The dats/ directory, matlabParams, the
    legacy embryoDB XML, and the DB row itself are all preserved — so
    curation work (edit.zip, AuxInfo, expression CSVs) survives the purge.
    """
    from datetime import datetime, timedelta, timezone
    import shutil
    from sqlalchemy import select
    from .models import Series

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=older_than)
    BULK_SUBDIRS = ("tif", "tifR", "DIC")

    with database.session_scope() as session:
        rows = list(
            session.execute(
                select(Series).where(
                    Series.deleted_at.is_not(None),
                    Series.deleted_at < cutoff,
                )
            ).scalars()
        )
        if not rows:
            console.print(f"[green]No series marked for deletion older than {older_than}d.[/green]")
            return
        console.print(
            f"{'Would delete' if not apply else 'Deleting'} image data for {len(rows)} series "
            f"(deleted_at older than {older_than}d):"
        )
        total_bytes = 0
        for row in rows:
            if not row.image_loc:
                console.print(f"  [yellow]skip[/yellow] {row.series_name}: no image_loc")
                continue
            base = Path(row.image_loc)
            subdirs = []
            for name in BULK_SUBDIRS:
                p = base / name
                if p.exists():
                    subdirs.append(p)
            # Also collect tifC<n> directories (extra channels).
            if base.exists():
                for p in base.iterdir():
                    if p.is_dir() and p.name.startswith("tifC"):
                        subdirs.append(p)
            if not subdirs:
                console.print(f"  {row.series_name}: nothing to delete (already purged)")
                continue
            for sub in subdirs:
                try:
                    sz = sum(f.stat().st_size for f in sub.rglob("*") if f.is_file())
                except OSError:
                    sz = 0
                total_bytes += sz
                age_days = (datetime.now(tz=timezone.utc) - row.deleted_at).days
                line = f"  {row.series_name}  {sub.name}/  {sz / 1e9:.2f} GB  (deleted {age_days}d ago)"
                console.print(line)
                if apply:
                    shutil.rmtree(sub, ignore_errors=False)

        console.print(
            f"\n[bold]{'Deleted' if apply else 'Would delete'} "
            f"~{total_bytes / 1e9:.2f} GB total[/bold]"
        )
        if not apply:
            console.print("[yellow]Dry-run only.[/yellow] Re-run with --apply to commit.")


# --- open (convenience launcher) --------------------------------------------


@app.command("open")
def open_cmd(
    source: Annotated[
        Path | None,
        typer.Option("--source", help="XML source dir (default: EMBRYODB_SOURCE_DIR)"),
    ] = None,
    lists_dir: Annotated[
        Path | None,
        typer.Option("--lists-dir", help="Flat list files dir (default: /gpfs/fs0/l/murr/lists)"),
    ] = None,
    parameters_dir: Annotated[
        Path | None,
        typer.Option("--params-dir", help="Protocol parameters dir (default: /gpfs/fs0/l/murr/parameters)"),
    ] = None,
    no_worker: Annotated[bool, typer.Option("--no-worker")] = False,
) -> None:
    """One-shot launcher: init DB, sync XMLs and lists, seed protocols, then open the GUI.

    Idempotent — safe to run on every startup. Re-running is fast because
    import-xml short-circuits unchanged rows, and seed-protocols is a no-op
    when protocols already exist.
    """
    src = source or settings.source_dir
    lists = lists_dir or Path("/gpfs/fs0/l/murr/lists")
    params = parameters_dir or Path("/gpfs/fs0/l/murr/parameters")

    # 1. Tables
    console.print("[bold]1/5[/bold] initialising database…")
    database.create_all()
    console.print("    [green]ok[/green]")

    # 2. Import legacy XMLs
    console.print(f"[bold]2/5[/bold] syncing XMLs from [cyan]{src}[/cyan]…")
    with database.session_scope() as s:
        report = import_dir(s, source_dir=src)
    console.print(f"    [green]ok[/green]  {report.summary()}")
    if report.parse_errors:
        for path, err in report.parse_errors[:5]:
            console.print(f"    [yellow]parse error[/yellow] {path}: {err}")

    # 3. Import dataset lists
    if lists.is_dir():
        console.print(f"[bold]3/5[/bold] syncing dataset lists from [cyan]{lists}[/cyan]…")
        with database.session_scope() as s:
            lr = import_lists(s, source_dir=lists)
        console.print(f"    [green]ok[/green]  {lr.summary()}")
    else:
        console.print(f"[bold]3/5[/bold] lists dir [yellow]{lists}[/yellow] not found — skipping")

    # 4. Seed protocols
    console.print(f"[bold]4/5[/bold] seeding protocols from [cyan]{params}[/cyan]…")
    with database.session_scope() as s:
        pr = seed_protocols(s, parameters_dir=params, name_prefix="Stellaris_")
    console.print(
        f"    [green]ok[/green]  inserted={len(pr.inserted)}  updated={len(pr.updated)}  skipped={len(pr.skipped)}"
    )

    # 5. Spawn worker if there are PENDING pipeline runs
    if not no_worker:
        from sqlalchemy import func, select
        from .models import PipelineStepRun, RunStatus
        from .pipeline.worker import spawn_worker, worker_is_running, WORKER_STEPS

        with database.session_scope() as s:
            n_pending = s.scalar(
                select(func.count())
                .select_from(PipelineStepRun)
                .where(
                    PipelineStepRun.step.in_(WORKER_STEPS),
                    PipelineStepRun.status == RunStatus.PENDING,
                )
            )
        console.print(f"[bold]5/5[/bold] worker: {n_pending} pending pipeline step(s)")
        if n_pending:
            if worker_is_running():
                console.print("    worker already running")
            else:
                spawn_worker()
                console.print("    [green]worker spawned[/green]")
        else:
            console.print("    nothing pending — skipping")
    else:
        console.print("[bold]5/5[/bold] worker: skipped (--no-worker)")

    # Open GUI
    console.print("\nopening GUI…")
    import os
    os.execvp("embryodb-gui", ["embryodb-gui"])


# --- phenotyping (LineagePhenotyping bridge) --------------------------------


phenotyping_app = typer.Typer(
    help="LineagePhenotyping bridge — freeze a dataset's CSVs for the R pipeline"
)
app.add_typer(phenotyping_app, name="phenotyping")


@phenotyping_app.command("freeze")
def phenotyping_freeze(
    dataset: Annotated[str, typer.Argument(help="Dataset name to freeze")],
    output_base: Annotated[
        Path | None,
        typer.Option(
            "--output-base",
            help="Parent dir for the freeze (default: /murrlab3/<user>/phenotyping)",
        ),
    ] = None,
    minutes_per_timepoint: Annotated[
        float | None,
        typer.Option(
            "--minutes-per-timepoint",
            help="Manual frame->minutes value for series lacking TIME info",
        ),
    ] = None,
) -> None:
    """Freeze every series of DATASET (plus the Sulston reference) into a
    self-contained directory with a ready-to-run R config.

    The freeze lands in ``<output-base>/<dataset>/``; downstream
    ``build_inputs.R`` then ``run_pipeline.R`` read and write there.
    """
    from .phenotyping import freeze_dataset

    with database.session_scope() as session:
        report = freeze_dataset(
            session,
            dataset,
            output_base=output_base,
            minutes_per_timepoint=minutes_per_timepoint,
        )
    console.print(f"[green]froze[/green] {report.dataset_name} -> [cyan]{report.target_dir}[/cyan]")
    console.print(f"  files copied: {report.n_copied}")
    console.print(f"  config: [cyan]{report.config_path}[/cyan]")
    console.print(f"  list:   [cyan]{report.list_path}[/cyan]")
    console.print(f"  report: [cyan]{report.report_path}[/cyan]")
    if not report.reference_included:
        console.print(
            f"  [yellow]warning[/yellow] reference series not found in DB; "
            "build_inputs.R regression will fail without it"
        )
    if report.minutes_per_timepoint is not None:
        console.print(f"  minutes_per_timepoint (fallback): {report.minutes_per_timepoint}")
    for w in report.warnings:
        console.print(f"  [yellow]![/yellow] {w}")


def open_gui() -> None:
    """Entry point for `embryodb-open`: runs `embryodb open` with no args."""
    import sys
    # Reuse the Typer app so --help, --source, etc. all work.
    sys.argv = ["embryodb-open"] + sys.argv[1:]
    # Inject the subcommand name so Typer routes correctly.
    sys.argv.insert(1, "open")
    app()


# --- entrypoint -------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
