# embryoDB — agent orientation

Python rewrite of the Murray Lab's Java `EmbryoDB.jar` (C. elegans embryo
lineage metadata DB) plus its `Stellaris_tif_pipeline*.pl` import pipeline.
This document is for AI assistants picking up the project. Live state is
the code; this is a map.

## Where things live

| Path | Purpose |
|---|---|
| `embryodb/` | Python package |
| `tests/` | pytest suite (~49 tests, in-memory SQLite, <1s) |
| `README.md` | User-facing install + CLI cheatsheet |
| `pyproject.toml` | Package metadata + dependencies |
| **Sibling, not committed** | |
| `../embryoDB_test_data/20250527_JIM783_efl-3_test/` | ~4.6 GB raw-image fixture: 10 timepoints × 7 positions × 67 planes × 2 channels + Leica Properties.xml per position. Used for end-to-end pipeline smoke tests. |
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
| **v1 — Safe mirror** | done | 0-byte-diff audit across 11,023 legacy XMLs; provenance + optimistic locking baked in; GUI in PySide6 via qtpy |
| **v1.1 — UX iteration** | done | dock-widget layout, searchable filters, dataset filter checkbox, AceTree launcher, Notes editor |
| **v2 — Pipeline import orchestration** | done | new schema, parsers, orchestrator with 6/9 steps inline + 3 PENDING; backfill command; AceTree config editor; status column |
| **v2 — Subprocess wrappers + worker + GUI wizard** | **pending** | run_starrynite / run_red_extract / run_measure stubs; background worker process; multi-page import dialog |
| v2.5 — LineagePhenotyping bridge + FastAPI | pending | |
| v3 — Reimplement Java analysis algorithms in Python | pending | |
| v4 — acetree_py / archive lifecycle / image tiles | pending | |

## Quickstart

```bash
# from this directory
pip install -e .                            # or pip install --user the deps in pyproject.toml
export EMBRYODB_DB_URL='sqlite:////tmp/embryodb-dev.db'

embryodb init-db
embryodb import-xml                          # bulk-load legacy XMLs (read-only against source-dir)
embryodb audit-import                        # must report 0 byte diffs
embryodb dataset import-lists                # load /gpfs/fs0/l/murr/lists/

embryodb pipeline seed-protocols             # one-time: seed Stellaris_* protocols
embryodb pipeline import-acquisition \       # v2 import on the test fixture
    ../embryoDB_test_data/20250527_JIM783_efl-3_test \
    --protocol Stellaris_JIM113 \
    --image-loc-root /tmp/embryodb-pipeline-test/images \
    --alias-root    /tmp/embryodb-pipeline-test/alias \
    --legacy-xml-dir /tmp/embryodb-pipeline-test/legacy_xml \
    --user jmurr --person jmurr --strain JIM783

QT_API=pyqt5 embryodb-gui                    # launch GUI (PyQt5 on the lab cluster; libxcb-cursor0 missing for PySide6)

pytest tests/                                # full suite
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

## Pending v2 work (next session)

In rough order:

1. **`run_starrynite` step**: wrap the legacy Matlab pipeline as a
   subprocess. The current Perl `matlab_SN_cluster.pl` is the reference
   for the command-line shape (line 114 of that file). Capture
   stdout/stderr to a log file at `PipelineStepRun.log_path`.
2. **`run_red_extract` + `run_measure` steps**: wrap `acebatch3.jar
   RedExtractor1` and `Measure1` invocations. The legacy CLI form is
   `nice java -mx500m -cp /gpfs/fs0/l/murr/tools3/acebatch3.jar
   <SubClass> <series_list_file>`.
3. **Background worker process** that picks up PENDING runs in series
   order. Per-machine queue, GUI-detachable (`subprocess.Popen` with
   `start_new_session=True`), heartbeat into `PipelineStepRun.heartbeat_at`
   so the GUI can show staleness if the worker died.
4. **GUI import wizard**: multi-page `QDialog` reachable from File menu.
   - Page 1: pick acquisition dir + protocol + parser; preview discovered
     positions.
   - Page 2: per-acquisition metadata + tunable parameter overrides
     (`rangethreshold`, `intensitythreshold`, `staging`,
     `firsttimestepdiam`, `firsttimestepnumcells`).
   - Page 3: confirm targets + permissions; submit enqueues work for the
     worker.
5. **GUI status badge live updates**: QTimer polls
   `PipelineStepRun.heartbeat_at` for series whose runs are RUNNING and
   refreshes the Pipeline column.

After v2: see plan for v2.5 (LineagePhenotyping bridge + FastAPI),
v3 (Java algorithm reimplementation), v4 (acetree_py + lifecycle).
