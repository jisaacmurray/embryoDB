# embryoDB data access — how-to for consumer programs

Audience: an AI assistant (or developer) building a **separate** program that
needs to read data from embryoDB, without taking on the full embryoDB dev
context. This is a focused map of the read surface: how to connect, how to
query, what the data means, and where the on-disk analysis files live.

**Read-only discipline:** consumers should only read. Writes (imports, edits,
pipeline runs, soft-deletes) go through the embryoDB GUI/worker, which enforce
provenance, optimistic locking, and the legacy-XML mirror. Don't `UPDATE` the
tables directly or write into a series' image/annotation dirs.

## Connecting

One environment variable selects the database; everything else is derived.

```bash
export EMBRYODB_DB_URL='postgresql+psycopg://<user>:<password>@<host>/embryodb'
# or, for a local/dev copy:
export EMBRYODB_DB_URL='sqlite:////absolute/path/to/embryodb.db'
```

The lab production DB is PostgreSQL (host `penticton`); ask for credentials
rather than hard-coding them. SQLite works for a local snapshot and is what
the test suite uses, so any consumer that works against SQLite will work
against Postgres unchanged (the schema is vendor-agnostic).

There are two ways to consume the data.

## Option A — shell out to the CLI

Best when your program is in another language, or you just need exports.
Install the package (`pip install -e .` from the repo, Python ≥ 3.11), which
puts an `embryodb` entry point on PATH. All commands honor `EMBRYODB_DB_URL`.

**Important:** the human-facing query commands (`list`, `show`, `stats`,
`dataset show`) print **Rich tables, not JSON** — fine for eyeballing, not for
parsing. For programmatic consumption prefer the **export** commands below
(which write parseable files) or Option B (the Python API).

### Query / inspect

```bash
embryodb list --gene pha-4 --status edited --limit 100   # filter series
embryodb list --person jmurr --since 20250101            # YYYYMMDD lower bound
embryodb show <series_name>                              # all fields for one series
embryodb stats                                           # counts by status
embryodb dataset list                                    # all named datasets
embryodb dataset show <dataset_name>                     # member series names
```

`list` filters: `--gene/-g`, `--person/-p`, `--status/-s`, `--since YYYYMMDD`
(date_acquired), `--text/-t` (free-text), `--limit` (default 50). Flags repeat
for OR (`-g a -g b`).

### Export parseable data

```bash
# Per-series metadata as legacy XML (the format every legacy tool reads).
# Writes to EMBRYODB_EXPORT_DIR unless --dir is given. Source dir is untouched.
embryodb export-xml <series_name> --dir ./out
embryodb export-xml all --dir ./out

# Flat newline-delimited series list for a dataset (the "/murr/lists/" format).
embryodb dataset export-list <dataset_name> --output ./mydataset.list
embryodb dataset export-all ./lists_dir --suffix .list     # one file per dataset

# Per-timepoint volume timestamps -> TIME<series>.csv (frame -> seconds).
embryodb emit-time-csv <series_name…|all>
```

### Phenotyping freeze (bundled data export for downstream R)

If your program is in the LineagePhenotyping space, the freeze gathers a
dataset's `dats/*.csv` + a ready-to-run config into one per-user directory:

```bash
embryodb phenotyping freeze <dataset> [--output-base DIR] [--minutes-per-timepoint N]
```

See `LineagePhenotyping bridge` in CLAUDE.md for the contract.

## Option B — import the Python query API (read-only)

Best for a Python consumer. Use the same session + query helpers the CLI uses;
do not build your own SQL.

```python
from embryodb import database
from embryodb.queries import series as q_series
from embryodb.queries import datasets as q_datasets

with database.session_scope() as s:          # reads EMBRYODB_DB_URL
    rows = q_series.list_series(
        s,
        reporter_gene=["pha-4"],             # all filters optional, lists = OR
        person=None,
        status=None,                         # list[Status]
        date_after="20250101",               # YYYYMMDD
        text=None,
        dataset_id=None,                     # restrict to a dataset's members
        limit=200,
    )
    one = q_series.get_by_name(s, "20250527_JIM783_efl-3_test_L1")  # Series | None
    total = q_series.count(s)
    by_status = q_series.count_by_status(s)   # {Status: int}

    ds = q_datasets.get_by_name(s, "ceh-32_mutant")   # Dataset | None
    members = [x.series_name for x in ds.series]
    all_ds = q_datasets.list_datasets(s)
```

**Session lifetime:** read what you need *inside* the `with` block (or eager-load
relationships) — ORM objects detach when the session closes. `session_scope()`
commits on exit; for pure reads that's a harmless no-op, but don't mutate.

## Data model — what's available

- **`Series`** — the core record (one TileScan position of one acquisition).
  Useful fields: `series_name`, `date_acquired`, `person`, `strain_name`,
  `treatments`, `reporter_gene`, `status` (a `Status` enum), `timepts`,
  `image_loc` (raw TIFs), `annot_loc` (annotations / `dats/`),
  `acetree_config`, `edited_by`, `edited_timepts`, `edited_cells`,
  `partial_editing_code` (curated lineage extent), `comments`. Provenance:
  `version`, `updated_at`, `updated_by`, `imported_at`, `xml_source_path`,
  `xml_hash`. (`embryodb show <series>` prints exactly this field set.)
- **`Dataset`** — a named collection of series (many-to-many via
  `dataset_series_table`). `dataset.series` → list of `Series`.
- **`MicroscopyMetadata`** (`series.microscopy`) — `voxel_xy_um`, `voxel_z_um`,
  and JSON blobs `channels`, `depth_compensation`, `acquisition_settings`.
- **`VolumeTimestamp`** (`series.volume_timestamps`) — per-timepoint
  `timepoint`, `absolute_seconds`, `delta_seconds` (frame → time conversion;
  this is what `emit-time-csv` renders).
- **`PipelineStepRun`** (`series.runs`) — per-step status
  (PENDING/RUNNING/COMPLETE/FAILED/SKIPPED) for the import/processing pipeline;
  read this to know whether a series' derived files exist yet.

The authoritative shapes are in `embryodb/models.py`.

## On-disk analysis files (the actual numeric data)

The DB holds metadata + provenance; the heavy per-cell data lives on the shared
filesystem, keyed off each series' paths:

- `image_loc` → raw image planes (`tif/`, `tifR/`, extra channels `tifC<n>/`)
  and `matlabParams`.
- `annot_loc` → annotations, including `dats/` with the per-cell CSVs
  (`CD<series>.csv`, `ACD<series>.csv`, `<series>AuxInfo.csv`,
  `TIME<series>.csv`, etc.) that downstream analysis consumes.

So a typical consumer pattern is: query the DB for the series/dataset you want,
read `annot_loc` (or `image_loc`) off each `Series`, then open the on-disk
files directly. Note some legacy code uses the alias path
`/murrlab/<user>/images/<series>` for the canonical `/murrlab3/<user>/...`.

## Gotchas

- CLI `list`/`show`/`stats` output is **Rich tables, not JSON** — don't parse
  them; use exports or Option B.
- `Status` is an enum; compare with `Status.<NAME>` or `.value`, not bare
  strings.
- Soft-deleted series carry `deleted_at`; depending on the query helper they
  may still appear — filter them out if your program shouldn't see them.
- Don't write into `image_loc` / `annot_loc`; those trees are managed by the
  pipeline and shared by legacy tools.
