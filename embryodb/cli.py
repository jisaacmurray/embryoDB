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


# --- entrypoint -------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
