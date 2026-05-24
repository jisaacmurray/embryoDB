# embryoDB — agent orientation

Python rewrite of the Murray Lab's Java `EmbryoDB.jar` (C. elegans embryo
lineage metadata DB) plus its `Stellaris_tif_pipeline*.pl` import pipeline.
This document is for AI assistants picking up the project. Live state is
the code; this is a map.

## Where things live

| Path | Purpose |
|---|---|
| `embryodb/` | Python package |
| `tests/` | pytest suite (66 tests, in-memory SQLite, <2 s) |
| `README.md` | User-facing install + CLI cheatsheet + Postgres/Mac runbook |
| `pyproject.toml` | Package metadata + dependencies (requires Python ≥ 3.11) |
| **Sibling, not committed** | |
| `../embryoDB_test_data/20250527_JIM783_efl-3_test/` | ~4.6 GB raw-image fixture: 7 positions × 67 planes × 2 channels. Position 1 has 100 real timepoints (copied from production), L2-L7 stay at 10. Used for end-to-end pipeline smoke tests. |
| `../embryoDB_exports/` | Default `EMBRYODB_EXPORT_DIR` — where DB→XML writes land |
| **Outside this tree** | |
| `~/.claude/plans/ok-as-you-see-staged-gizmo.md` | Master plan: v1..v4 milestones, schema decisions, design rationale |
| `~/.claude/projects/-murrlab-gpfs-fs0-l-murr-new-tools/memory/project_embryodb_v1.md` | Memory file for cross-session context |
| `/murrlab/gpfs/fs0/l/murr/embryoDB/*.xml` | **Read-only legacy XMLs.** Source of truth for v1 audit-import; protected by the safe-mirror property |
| `/murrlab/gpfs/fs0/l/murr/new_tools/accessory/CLAUDE.md` | Catalog of legacy Perl/Java extraction tools v3 will subsume |
| `/murrlab/gpfs/fs0/l/murr/new_tools/LineagePhenotyping/CLAUDE.md` | R analysis pipeline that v2.5 will bridge |

## Status at a glance

| Milestone | State | Notes |
|---|---|---|
| **v1 — Safe mirror** | done | 0-byte-diff audit across 11,021 legacy XMLs; provenance + optimistic locking baked in; GUI binding-agnostic via qtpy |
| **v1.1 — UX iteration** | done | dock-widget layout, searchable filters, dataset filter checkbox, AceTree launcher, Notes editor |
| **v2 — Pipeline import** | done | 9 step orchestrator, background worker (handles `stage_images` + 3 subprocess steps), per-host pidfile, heartbeat-based crash recovery, GUI import wizard, live status polling, off-hours `delay_hours` scheduling, soft-delete lifecycle + `gc-deleted` CLI |
| **v2.1 — Multi-user + Postgres** | done (docs + schema) | Schema vendor-agnostic, additive migrations on connect (`deleted_at`, `deleted_by`, `not_before`, VARCHAR→TEXT widening); README has full Postgres runbook + Mac SSH-tunnel recipe. Lab Postgres on `penticton`; `/opt/embryodb/venv` recommended install model. |
| **v2.2 — Legacy tool launchers** | done | GUI dialogs for the 8 `extract.sh` steps and `PrintTrees.pl` (Tree1); selection-based + dataset-based |
| v2.5 — LineagePhenotyping bridge + FastAPI | pending | |
| **v3 — Reimplement Java/Perl tools in Python** | pending | The bigger remaining chunk. See "Legacy tools currently called" below. |
| v4 — acetree_py / archive lifecycle / image tiles | pending | |

## Quickstart

```bash
# From a fresh shell on the lab host (penticton). All env in /etc/profile.d/embryodb.sh
# and /opt/embryodb/venv per the README install steps.
embryodb-open                                # bootstraps + opens GUI; idempotent
# OR
embryodb-gui                                 # day-to-day, faster startup

# Local dev with SQLite (no Postgres needed):
export EMBRYODB_DB_URL='sqlite:////tmp/embryodb-dev.db'
embryodb-open

# Smoke tests:
pytest tests/                                # 66 tests, in-memory SQLite, <2 s
embryodb audit-import                        # must report 0 byte diffs across 11k XMLs
```

Pipeline import via CLI (the GUI wizard is the preferred path):

```bash
embryodb pipeline seed-protocols             # one-time: seed Stellaris_* protocols
embryodb pipeline import-acquisition \
    ../embryoDB_test_data/20250527_JIM783_efl-3_test \
    --protocol Stellaris_JIM113 \
    --image-loc-root /tmp/embryodb-pipeline-test/images \
    --alias-root    /tmp/embryodb-pipeline-test/alias \
    --legacy-xml-dir /tmp/embryodb-pipeline-test/legacy_xml \
    --user jmurr --person jmurr --strain JIM783
```

## Architecture: trust-anchored layers

```
            +----------------------+
   GUI ---> | queries/   (filters, |
            |   validated, datasets|
   CLI ---> |   list, distinct...) |
            +----------+-----------+
                       |
            +----------+-----------+         +-------------------+
            | models.py            |<--------+ database.py       |
            | (SQLAlchemy ORM)     |  engine | (session_scope    |
            +----------+-----------+         |  context manager) |
                       |                     +-------------------+
                       |
  +--------------------+----------------------+
  |                                            |
  v                                            v
 importers/ + exporters/             pipeline/   (v2)
 + parsers/xml.py                    + parsers/{filename, leica_metadata, matlab_params}
                                     + fsutil.safe_write (project invariant)
 (v1 safe mirror)                    + orchestrate.import_acquisition
                                     + backfill.backfill_directory
```

Key trust property: **`audit-import` round-trips 11,023 legacy XMLs to
byte-identical output**, and `source-dir` files are never modified (only
new files added). This is the v1 guarantee; v2 honours it via
`step_write_embryodb_xml` refusing to overwrite existing legacy XMLs.

## v2 pipeline import — design highlights

- **Acquisition vs Series.** One microscope run → one `Acquisition` →
  many `Series` (one per TileScan position, e.g. `<stem>_L1`..`_LN`).
- **Channel mapping is data, not script choice.** The three legacy
  Stellaris variants (Sequential / Simultaneous / Simultaneous+DIC) are
  now rows in the `Protocol` table with `channel_map = {raw_ch: role}`.
  Extra channels route to `tifC<n>/` so the data survives even though
  legacy AceTree only displays 2.
- **Filename parser is a plugin registry**
  (`embryodb/parsers/filename.py`). Ships Leica TileScan; designed to add
  Zeiss / Nikon without forking the pipeline.
- **`fsutil.safe_write` is the permission invariant.** Every write
  (TIFs, AceTree XML, matlabParams, legacy XML, symlinks) routes through
  it: umask 0002, chmod 0664 files / 0775 dirs, chgrp `users`. Fixes the
  legacy permissions issue at the extract step.
- **Resolution comes from Leica `Properties.xml`** (parsed into
  `MicroscopyMetadata`), with hardcoded fallback only when missing. Same
  values populate the AceTree XML and matlabParams so they can't drift.
- **9 pipeline steps tracked in `PipelineStepRun`**: stage_images,
  stage_metadata, write_acetree_config, write_embryodb_xml,
  create_alias_symlink, write_matlab_params, **run_starrynite,
  run_red_extract, run_measure**. Last three are PENDING stubs.

## Gotchas

- **PySide6 doesn't launch on the lab cluster.** Missing
  `libxcb-cursor0`. Use `QT_API=pyqt5` (already installed). qtpy makes
  the GUI binding-agnostic.
- **Legacy AceTree is hardcoded to 2 channels** (`tif/` + `tifR/`).
  Multichannel acquisitions stage extras to `tifC<n>/` for future viewers
  but legacy AceTree ignores them.
- **Source-dir is the writer of record for the legacy Java GUI.**
  v2's `write_embryodb_xml` lands new acquisitions there so both systems
  see them. It explicitly refuses to overwrite existing files. Edits to
  imported series go to `export-dir` — a future `promote-to-source`
  operation will replace that staging once trust is built.
- **`/murrlab3/<user>/images/<series>` is canonical**, with an alias at
  `/murrlab/<user>/images/<series>`. Some legacy code uses the alias.
  The pipeline creates the symlink automatically.
- **Test fixture filenames are prefixed `_test`** (e.g.
  `20250527_JIM783_efl-3_test_L1`) so smoke tests can't accidentally
  clobber real `20250527_JIM783_efl-3` series.
- **49-test suite includes one slow test** (~1s) that writes synthetic
  TIFFs via `tifffile`. Everything else is sub-millisecond.

## Where to look first when…

- **The Pipeline column shows wrong status** → `embryodb/gui/models.py::_summarize_runs` + `PipelineStepRun` rows in DB
- **An import fails partway** → `PipelineStepRun.error_excerpt` for that series; orchestrator step rows show `failed_step` per series
- **Resolution looks wrong in AceTree XML** → `embryodb/pipeline/orchestrate.py::step_write_acetree_config` pulls from `series.microscopy.voxel_xy_um` / `voxel_z_um`; fallback constants `FALLBACK_VOXEL_XY_UM` / `FALLBACK_VOXEL_Z_UM`
- **Channel routing looks wrong** → check `Protocol.channel_map` for the protocol used; `embryodb/pipeline/stage.py::role_subdir` maps roles to subdirs
- **audit-import starts failing** → something modified source-dir; check `embryodb/audits.py::audit_import` output, compare exported XML against the originals via `compare-with-source`

## Legacy tools currently called (v3 replacement targets)

The Python layer is the orchestrator + UI; the actual analysis work still
happens in legacy Java / Perl / Matlab binaries that the worker (or, for
some, the GUI directly) spawns. Each of these is a v3 candidate for native
reimplementation. Listed roughly in order of how much code depends on them.

### Java JARs (`/gpfs/fs0/l/murr/tools3/`)

| Jar | Subcommand(s) | Called from | What it does |
|---|---|---|---|
| `acebatch3.jar` | `RedExtractor1` | `pipeline.subprocess_steps.step_run_red_extract` + `external_tools.run_extract` | Quantitate red-channel signal per nucleus |
| `acebatch3.jar` | `Measure1` | `pipeline.subprocess_steps.step_run_measure` + `external_tools.run_extract` | Nuclear morphometry; writes `<series>AuxInfo.csv` |
| `acebatch3.jar` | `RedExcel1` | `external_tools.run_extract` | Format RedExtractor output → `S<series>.csv` |
| `acebatch3.jar` | `RedExcel2` | `external_tools.run_extract` | Position + expression → `CD<series>.csv` |
| `acebatch3.jar` | `Align1` | `external_tools.run_extract` | Sulston-lineage alignment |
| `acexpress_CL2.jar` | `Tree1` | `external_tools.run_print_trees` (via the GUI "Print trees…" launcher) | Lineage-tree PNG with expression overlay; output at `/gpfs/fs0/l/murr/trees/` |
| `partialCSV.jar` | (no class — `-jar`) | `Partial.pl` (invoked via `external_tools.run_extract`) | Trim per-cell tables to the curated extent from `checkedby` |
| `AceTree_Santella.jar` | (no class — `-jar`) | `external.launch_acetree` (detail-panel button) | Legacy curation GUI; fire-and-forget |

### Perl scripts (`/gpfs/fs0/l/murr/tools3/`)

| Script | Called from | What it does |
|---|---|---|
| `matlab_SN_cluster.pl` | `pipeline.subprocess_steps.step_run_starrynite` | Wraps the compiled-Matlab StarryNite pipeline; uses MCR v714 |
| `Partial.pl` | `external_tools.run_extract` | Drives `partialCSV.jar` to trim CD/CA/SCD/SCA tables to curated extent |
| `ProcessTime.pl` | `external_tools.run_extract` | Per-timepoint timestamps; writes `TIME<series>.csv` |
| `UpdatePermissions.pl` | `external_tools.run_extract` | `chgrp users` + `chmod` across each series' `dats/` |
| `Process_Time_Stellaris.pl` | (not yet wired) | Stellaris variant of `ProcessTime.pl`; not in `extract.sh` flow today |
| `GetACD.pl` | (not yet wired) | ACD coordinate normalization vs. Richards 2013 reference |
| `PrintTrees.pl` | bypassed | Tiny wrapper around `Tree1`; we call the Java class directly |

### Matlab (compiled)

| Binary | Called from | What it does |
|---|---|---|
| `starrynite_traceonly/starrynite` | `matlab_SN_cluster.pl` (transitively) | StarryNite cell-detection + tracking core |
| `run_commandLineDriver.sh` | `matlab_SN_cluster.pl` | MCR launcher for the Matlab compiled binary |

Replacement strategy is in the master plan under v3. The two-phase
sequencing is roughly: (1) port the easy stuff (Perl glue + Excel-format
emitters) to Python alongside the Java calls (already underway —
`fsutil.safe_write` subsumes `UpdatePermissions.pl`); (2) port the big
Java workhorses (`RedBkgComp7`, `Measure`, `SeriesSulstonizer`) using
numpy + scipy; (3) consolidate tree rendering (Tree1 + LIVEtools +
TreeExprViewer2 → one Python renderer).

## Future-feature placeholders (designed but not yet implemented)

These are deliberate design choices recorded here so they don't get lost.

### Worker DB-level claim (multi-host safety)

`_next_work_item` in `embryodb/pipeline/worker.py` doesn't atomically
transition PENDING → RUNNING. Two workers on different hosts could see the
same row PENDING and both start the subprocess. Today's mitigation is the
per-host pidfile, which prevents two workers per machine but doesn't help
across machines. Fix would either add `claimed_by` / `claimed_at` columns
with a single-row UPDATE-WHERE-status='pending', or `SELECT … FOR UPDATE
SKIP LOCKED` on Postgres. Low priority right now (single worker per lab),
but mandatory before two machines run workers against the same DB.

### Retry on transient `OperationalError`

Postgres occasionally drops connections (network blips, server restarts).
A tiny retry decorator around `session_scope()` would make the GUI more
resilient. Not blocking — current behaviour surfaces the error and the
user retries.

### Off-hours scheduling — richer policies

Current `delay_hours` on the wizard's Targets page sets `not_before` on
each `PipelineStepRun`. A future scheduler could add per-step time-of-day
windows, day-of-week restrictions, max-N-concurrent across the lab, and a
"scheduled jobs" view. The `not_before` column is the API; richer policy
reads from it without further schema changes.

### Deletion lifecycle — pre-import gc

`Series.deleted_at` + `embryodb gc-deleted` covers post-import. The
mirror case — auto-purging `Acquisition.source_dir` after every position
reaches a terminal pipeline state — isn't built yet.

### Promote-to-source (XML retirement path)

Phase A of "migrating away from XML dependence" (see prior session
notes). Once `audit-import` has run clean for some weeks, flip a flag
so detail-panel Saves write `source-dir/<series>.xml` directly instead
of `export-dir/`. The serializer is already byte-identical.

## Pending v2 work (next session)

All v2 / v2.1 / v2.2 deliverables are done. Next session likely starts
on one of:
- **v2.5 LineagePhenotyping bridge** — biggest next chunk
- **v2.5 FastAPI tier** — unlocks Mac access without SSH tunnel
- **v3 RedBkgComp7 port** — the first major Java workhorse to port
- One of the future-feature placeholders above

After v2: see plan for v2.5 (LineagePhenotyping bridge + FastAPI),
v3 (Java/Perl reimplementation), v4 (acetree_py + lifecycle).
