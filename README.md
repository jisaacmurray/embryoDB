# embryoDB v1 — safe-mirror

A Python rewrite of the Murray Lab's Java `EmbryoDB.jar`. v1 is intentionally
narrow: it imports every legacy XML file, lets you browse / filter / edit them
in a Qt GUI, and writes back to a separate export directory. The original
`/murrlab/gpfs/fs0/l/murr/embryoDB/` is never modified.

The legacy Java GUI keeps running against `source-dir`, so the two systems
coexist while trust is built. See `/home/jmurr/.claude/plans/ok-as-you-see-staged-gizmo.md`
for the broader roadmap (v1.5 through v4).

## Requirements

```bash
pip install -e .             # from this directory
# or, if developing without installing:
pip install --user sqlalchemy psycopg pydantic pydantic-settings typer rich qtpy
# Plus one Qt binding (qtpy auto-selects whichever is present):
pip install --user PyQt5             # works everywhere, including X11 forwarding on Linux
# or:  pip install --user PySide6   # needs system libxcb-cursor0 on Linux ≥ 6.5
# or:  pip install --user PyQt6
```

The GUI works over SSH X11 forwarding. **On the lab cluster, use PyQt5** —
PySide6 6.5+ has a runtime dependency on `libxcb-cursor0` that isn't
installed there. To force a specific binding, set `QT_API=pyqt5` (or
`pyqt6` / `pyside6`).

## Configuration

All settings come from env vars (prefix `EMBRYODB_`) or a `.env` file. Defaults:

| Var | Default | Purpose |
|---|---|---|
| `EMBRYODB_SOURCE_DIR` | `/murrlab/gpfs/fs0/l/murr/embryoDB` | Read-only legacy XMLs |
| `EMBRYODB_EXPORT_DIR` | `/murrlab/gpfs/fs0/l/murr/new_tools/embryoDB_exports` | Where DB→XML writes land |
| `EMBRYODB_DB_URL` | `postgresql+psycopg://embryodb@localhost/embryodb` | SQLAlchemy URL |
| `EMBRYODB_USER` | `anonymous` | recorded as `imported_by` / `updated_by` |

For quick local dev use SQLite:

```bash
export EMBRYODB_DB_URL='sqlite:////tmp/embryodb-dev.db'
```

## First-time setup

```bash
# Create tables
embryodb init-db

# Pull every XML from source-dir into the DB (read-only on source)
embryodb import-xml

# Sanity check: every imported row must regenerate to byte-identical XML
embryodb audit-import         # exit 0 means safe-mirror holds
```

## CLI cheatsheet

| Command | What it does |
|---|---|
| `embryodb init-db` | Create the schema |
| `embryodb import-xml [dir]` | Bulk-import XMLs from source-dir (read-only) |
| `embryodb export-xml [series\|all]` | Write back to export-dir |
| `embryodb audit-import` | Round-trip the corpus, diff against source. Exit 1 on any diff |
| `embryodb compare-with-source <series>` | Single-series diff |
| `embryodb find-duplicates` | Name collisions, case-fold, file↔row symmetric diff |
| `embryodb validate-paths [--dataset NAME]` | Check image_loc / annot_loc / acetree_config on disk |
| `embryodb missing-images` / `missing-annots` | Coverage report variants |
| `embryodb list [--gene G] [--person P] [--status S] [--since YYYYMMDD] [--text T]` | Filter |
| `embryodb show <series>` | All fields + provenance |
| `embryodb stats` | Count by status |
| `embryodb dataset create <name> [--series A --series B]` | Create a named collection |
| `embryodb dataset add <name> <series...>` | Add to a collection |
| `embryodb dataset export-list <name> --output FILE` | Write `/murr/lists/`-style text file |

## GUI

```bash
embryodb-gui                # auto-selects whichever Qt binding is installed
QT_API=pyqt5 embryodb-gui   # explicit (recommended on the lab cluster)
# or
python -m embryodb.gui.app
```

Works over SSH X11 forwarding. Layout:

- **Filter bar** — person / strain / reporter / treatments / editor / status (multi-select dropdowns), date-before / date-after, text search with "comments-only" toggle. Filters compose live.
- **Dataset bar** — current dataset dropdown; create / add-selected / remove-selected / export-list buttons.
- **Browser table** — sortable, multi-select. Click a row → details on the right.
- **Detail panel** — 16 fields (line edits, multi-line for comments, auto-complete combos for controlled vocab) plus a Provenance section showing `version`, `updated_at`, source XML hash. Save uses optimistic locking — concurrent edits surface a conflict dialog with reload/cancel.

## Safety guarantees

- `source-dir` is opened read-only. No code path writes to it. The exporter, audits, and round-trip tests all confirm this.
- `audit-import` round-trips all 11,023 corpus XMLs to byte-identical output today (0 diffs).
- Optimistic locking: every Series row carries `version`. A stale-version save surfaces a conflict dialog rather than silently overwriting another user's edits.
- Raw XML preserved on every row (`raw_xml`, `xml_hash`, `xml_mtime`, `xml_source_path`, `imported_at`, `imported_by`) so we can detect source drift after import.

## Tests

```bash
pytest tests/                # ~30 tests, all in-memory SQLite, <1s
```

## Source layout

```
embryodb/
  models.py         SQLAlchemy ORM (Series, Dataset, dataset_series, Status)
  database.py       Engine + session_scope context manager
  config.py         Pydantic-settings (source_dir, export_dir, db_url)
  xml_format.py     The 16-field schema (single source of truth)
  parsers/xml.py    Lenient parser + canonical serializer
  importers/        xml_importer.py
  exporters/        xml_exporter.py
  queries/          series.py, datasets.py
  audits.py         audit-import, find-duplicates, validate-paths, …
  cli.py            Typer entry
  gui/              app.py, main_window.py, filter_bar.py, detail_panel.py,
                    dataset_panel.py, models.py (Qt table model)
```

## v2 Pipeline import (in progress)

Replaces the Perl `Stellaris_tif_pipeline*.pl` + `standard_pipeline.sh`
orchestration with Python. The Java analysis JARs themselves (RedBkgComp7,
Measure, SeriesSulstonizer) stay as subprocess invocations — those are
rewritten in v3.

### CLI

```bash
# One-time: seed Protocols from /gpfs/fs0/l/murr/parameters/Stellaris_*
embryodb pipeline seed-protocols

# Inspect available protocols
embryodb pipeline list-protocols

# Import one acquisition end-to-end (channel routing per the protocol)
embryodb pipeline import-acquisition \
    /murrlab3/Images/20250527_JIM783_efl-3 \
    --protocol Stellaris_JIM113 \
    --person azach \
    --strain JIM783 \
    --perturbation 'efl-3 RNAi'

# Register pre-existing on-disk acquisitions (no re-processing)
embryodb pipeline backfill /murrlab3/azach/images/
```

### What `import-acquisition` does

For each TileScan position discovered in the source dir:

1. **stage_images** — LZW-compress + rename raw TIFs into `tif/` / `tifR/` /
   optional `DIC/` (channel routing per `Protocol.channel_map`); `tifC<n>/`
   for any additional channels.
2. **stage_metadata** — copy the per-position Leica `Properties.xml` to
   `dats/` and parse it into a `MicroscopyMetadata` row (voxel sizes,
   objective, NA, channels, pinhole, stage position, scan settings).
3. **write_acetree_config** — `<image_loc>/dats/<series>.xml` with
   resolution sourced from the Leica metadata.
4. **write_embryodb_xml** — legacy 16-field XML written to source-dir so
   the unmodified Java GUI sees the new series immediately. **Refuses to
   overwrite** an existing file (the safe-mirror property holds).
5. **create_alias_symlink** — `/murrlab/<user>/images/<series>` →
   `/murrlab3/<user>/images/<series>` for tools that use the alias path.
6. **write_matlab_params** — copies the protocol's parameter file, applies
   `--set-param k=v` overrides, sets `xyres` / `zres` / `slices` /
   `end_time` from the Leica metadata so AceTree XML and matlabParams
   can't drift.

Steps 7-9 (`run_starrynite`, `run_red_extract`, `run_measure`) are
recorded as PENDING rows on each Series for the future background worker.

### Every file write goes through `fsutil.safe_write`

Honors `umask 0002`, `chmod 0664` on files / `0775` on dirs, `chgrp users`.
Fixes the legacy permissions issue at the extract step where files written
by one user blocked others from accessing them.

### Browser GUI

- New **Pipeline** column shows per-series step state, e.g.
  `6/9  stage✓ meta✓ cfg✓ xml✓ lnk✓ prm✓ SN. red. meas.`
- **Edit AceTree config…** button on the detail panel for tweaking
  `<annot_loc>/dats/<series>.xml` (start/end/axis/resolution).

## Beyond v2

- **v2.5** — LineagePhenotyping YAML bridge, FastAPI tier for off-network access
- **v3** — Python port of RedBkgComp7, Measure, SeriesSulstonizer, GetACD
- **v4** — acetree_py launch, archive/delete lifecycle, image tile streaming
