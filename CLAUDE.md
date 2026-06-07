# embryoDB — agent orientation

Python rewrite of the Murray Lab's Java `EmbryoDB.jar` (C. elegans embryo
lineage metadata DB) plus its `Stellaris_tif_pipeline*.pl` import pipeline.
This document is for AI assistants picking up the project. Live state is
the code; this is a map.

> **Commit cadence (agents, please read).** Commit at regular intervals —
> after each self-contained, test-green unit of work (a fix, a feature, a
> doc pass), not in one giant batch at the end of a session. Concretely:
> once the suite passes for a logical change, stage the related files and
> make a focused commit. If the working tree has accumulated several
> unrelated changes, split them into grouped commits rather than one
> mega-commit. If a session is wrapping up with uncommitted work, prompt
> the user to commit before ending. Do **not** push unless the user asks.
> Never stage editor autosave files (e.g. `#README.md#`).

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
| **v2.3 — Laptop-optimized GUI** | done | Detail dock fits a 1366×768 screen with ≥10 table rows visible. See "GUI layout — design goals" below. |
| **v2.4 — Partial.pl ported** | done | `embryodb partial` reads `partial_editing_code` from the DB instead of the stale legacy XML. First piece of the extract chain to be reimplemented in Python. |
| **v2.5 — Legacy XML auto-sync** | done | Every GUI save path (detail edit, bulk edit, mark-for-deletion) re-writes `source_dir/<series>.xml` so legacy tools (Tree1, Measure, RedExtractor1, …) see fresh `edited_timepts` / `partial_editing_code` / etc. immediately. CLI catch-up via `embryodb sync-legacy-xml [name…\|all]`. Bridge during the v3 rewrite — retires once the legacy tools are ported. |
| **v2.6 — TIME at import time** | done | New `compute_timestamps` pipeline step parses the Stellaris `<TimeStampList>` (hex FILETIME) at import, fills `volume_timestamps`, and writes `TIME<series>.csv` from the DB. `ProcessTime` removed from the default extract checklist (legacy SP5 series only). Vendor-pluggable registry in `parsers/timestamps.py`. CLI: `embryodb emit-time-csv [name…\|all]`. |
| **v2.7 — Acquisition settings + depth compensation** | done | Parser now extracts per-active-channel laser line + AOTF intensity + detector gain/dye/band, depth-compensation curves (projected per channel), and scalar scope settings (bit depth, pixel dwell, zoom, scan geometry, programmed timing, instrument serial). Stored as JSON on `MicroscopyMetadata` (`channels`, `depth_compensation`, `acquisition_settings`). Right-click → **Microscopy details…** opens a per-series dialog with the high-value table (channels + depth-comp curve). |
| **v2.7.1 — Multi-host worker claim** | done | `_claim_next` in `pipeline/worker.py` atomically transitions PENDING→RUNNING via a guarded `UPDATE … WHERE id=? AND status='pending'` + `rowcount` check (portable across SQLite/Postgres; no `FOR UPDATE SKIP LOCKED` needed). Race losers re-evaluate and pick the next candidate. New `claimed_by` column on `PipelineStepRun` (observability; additive migration). Safe for two machines running workers against one DB. |
| v2.8 — LineagePhenotyping bridge | **done** | Phase 1 (Python dataset freeze: CLI + GUI) + Phase 2 (GetACD stopgap, `external_tools.run_getacd`) + Phase 3 (R `build_inputs.R` port) all done; Phase 3 byte-validated against `die-1/` and `ceh-32_mutant/` Perl outputs. See "LineagePhenotyping bridge" section below. |
| **v3 — Reimplement remaining Java/Perl tools** | pending | The bigger remaining chunk. See "Legacy tools currently called" below + the v3 ordering note. |
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

## GUI layout — design goals (maintain across changes)

The dock layout was tuned in v2.3 so the default open dock fits a 1366×768
laptop with ≥10 rows of the browser table still visible. Re-arrangements
should preserve these properties; they're load-bearing for day-to-day use.

- **No vertical header rows on the dock.** The QDockWidget's title bar is
  hidden via `setTitleBarWidget(QWidget())`; the View menu's toggle action
  handles show/hide. Dataset and Details subsections are wrapped in
  `QFrame(StyledPanel)` for visual separation but with no titles.
- **Editable fields are split into two sub-columns** (`form_a` / `form_b`
  in `gui/detail_panel.py`). Pipeline lives in column A as a read-only
  summary + `Details…` popup; Comments (multi-line) lives in column B —
  this balances the two columns' heights.
- **Right column is short, fixed-content.** Member-of list is capped at
  ~3 rows (`fm.height() * 3 + 12`) with a scroll bar for the rare case of
  many memberships. Action buttons (Launch AceTree / Edit AceTree config /
  Edit AuxInfo / Mark for deletion) stack vertically below it, with
  Reload+Save in a 2-col HBox, the dirty indicator above. Provenance is a
  collapsible block at the bottom (`▸ Provenance`), hidden by default. No
  separate bottom-of-dock button row.
- **Splitter is 3:1 left:right**; the user can still drag-rebalance on
  larger monitors. The form fields stretch horizontally so wider monitors
  give more text-input space without restructuring.
- **Dataset bar is two rows**: row 1 = `Search` + `Dataset` combo + `New…`
  + `Show only members`; row 2 = membership / inspection / export /
  analysis buttons. Each row's stretch absorbs slack on wider monitors.

If you're changing the detail panel or dataset bar, eyeball the result at
1366×768 (or use `QT_QPA_PLATFORM=offscreen` + screenshot helper) and keep
at least 10 browser rows visible with the dock at its default position.

## Legacy XML auto-sync (v2.5)

The legacy `source_dir/<series>.xml` files are still the source of truth
for every Java/Perl tool (Tree1 reads `iRecord[11]` for end time;
RedExtractor1, Measure1, etc. all open `EmbryoXML(<series>)`). The new
GUI is the source of truth for the DB. To keep the two in lock-step
during the v3 rewrite, every save path in the new GUI re-writes the
legacy XML through `embryodb.legacy_sync.sync_legacy_xml(name)`:

- `DetailPanel._on_save` — single-series edits
- `DetailPanel._on_toggle_delete` — soft-delete flag flips
- `BulkEditMetadataDialog._on_apply` — bulk metadata edits

When porting a new save path, route it through `sync_legacy_xml(name)` /
`sync_many(names)` after `session.commit()` so legacy tools see the same
state. Failures are logged to stderr but do not abort the GUI save —
losing the legacy mirror is recoverable via `embryodb sync-legacy-xml`.

**Audit-import is preserved.** The exporter rewrites with the canonical
form whenever `version > 1`, and the auto-sync bumps both `version` and
`raw_xml` in the same transaction. Round-tripping a synced row through
`audit-import` still produces a 0-byte diff because source-dir is now
the canonical form, and that's what the exporter produces too.

**Retirement.** This sync goes away once every legacy tool either reads
from the DB directly (the Tree1/Measure ports) or has been replaced by a
Python implementation. Search the codebase for `sync_legacy_xml` to find
the call sites that need to be removed when the time comes.

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
| `partialCSV.jar` | (no class — `-jar`) | `embryodb.partial` (Python wrapper that replaced Partial.pl in v2.4) | Trim per-cell tables to the curated extent from the DB's `partial_editing_code` |
| `AceTree_Santella.jar` | (no class — `-jar`) | `external.launch_acetree` (detail-panel button) | Legacy curation GUI; fire-and-forget |

### Perl scripts (`/gpfs/fs0/l/murr/tools3/`)

| Script | Called from | What it does |
|---|---|---|
| `matlab_SN_cluster.pl` | `pipeline.subprocess_steps.step_run_starrynite` | Wraps the compiled-Matlab StarryNite pipeline; uses MCR v714 |
| ~~`Partial.pl`~~ | ported in v2.4 → `embryodb.partial` | Replaced — reads `partial_editing_code` from the DB instead of `<checkedby>` in the legacy XML. Still drives `partialCSV.jar` for the lineage-walk trim. |
| `ProcessTime.pl` | `external_tools.run_extract` — opt-in for legacy SP5 series only. The Stellaris branch was ported to `parsers/timestamps.py` in v2.6; the SP5 `info/_t<N>_*` branch still needs a Python port. | Per-timepoint timestamps for SP5-era data (writes `TIME<series>.csv`). |
| `UpdatePermissions.pl` | `external_tools.run_extract` | `chgrp users` + `chmod` across each series' `dats/` |
| ~~`Process_Time_Stellaris.pl`~~ | ported in v2.6 → `parsers/timestamps.py::LeicaStellarisTimestampParser` | The old regex looked for `<TimeStamp RelativeTime="..."/>`; the modern Stellaris format packs timestamps as hex Windows FILETIME values in `<TimeStampList>`. The Python parser handles the new format. |
| `GetACD.pl` | wrapped → `external_tools.run_getacd` (v2.8, **temporary stopgap** — R rewrite is the planned replacement) | ACD coordinate normalization vs. Richards 2013 reference |
| `GetFiles.pl` | ported → `embryodb.phenotyping.freeze` (v2.8) | Per-series `dats/*.csv` freeze into a per-user directory; the embryoDB-native dataset-aware replacement. |
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

### v3 ordering (negotiated 2026-05-25)

- **Tree1 wrapper next.** Read `edited_timepts` from the DB and pass it as
  an explicit CLI arg so the in-jar XML lookup stops mattering. Should be
  a one-day port; the v2.5 auto-sync already protects against drift, but
  the wrapper retires the XML dependency for this tool entirely.
- **`UpdatePermissions.pl` next.** Already largely subsumed by
  `fsutil.safe_write`; trivial to finish (just `os.chmod` + `os.chown`).
- ~~`ProcessTime.pl` / `Process_Time_Stellaris.pl`~~ — **done in v2.6.**
  Per-timepoint timestamps are now parsed at import time by the new
  `compute_timestamps` pipeline step. Vendor-pluggable parser registry
  lives in `embryodb/parsers/timestamps.py` (Leica Stellaris implemented;
  SP5-era format + non-Leica vendors plug in by adding a `TimestampParser`
  to `TIMESTAMP_PARSERS`). Output: `volume_timestamps` table + per-series
  `TIME<series>.csv` for downstream consumers that haven't been ported
  (`LineagePhenotyping/CompareDivTime.pl`). The legacy `ProcessTime`
  extract step is unchecked by default in the GUI dialog — opt-in only
  for series that pre-date the v2 pipeline.
- **Then port the Java workhorses** (RedBkgComp7, Measure,
  SeriesSulstonizer) in numpy/scipy as originally planned.
- **Last: consolidate tree rendering** (Tree1 + LIVEtools +
  TreeExprViewer2 → one Python renderer).

## Future-feature placeholders (designed but not yet implemented)

These are deliberate design choices recorded here so they don't get lost.

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

## LineagePhenotyping bridge (v2.8, in progress)

Hybrid design: **embryoDB (Python)** owns dataset resolution + the file
freeze; **LineagePhenotyping (R)** owns the numeric extraction
(`build_inputs.R`, a port of `CompareDivTime.pl` + `ComparePositions.pl`)
and the existing 8-step `run_pipeline.R`. The R half runs standalone from a
freeze so a collaborator with only `dats/*.csv` and no embryoDB install can
still run everything.

**Phase 1 — DONE.** `embryodb/phenotyping/freeze.py` ::
`freeze_dataset(session, dataset_name, output_base=None,
minutes_per_timepoint=None) -> FreezeReport`. Copies each series'
`<annot_loc>/dats/*.csv` (via `fsutil.safe_copy`) into a **per-user**
directory `/murrlab3/<user>/phenotyping/<dataset>/` (overridable), always
includes the Sulston reference series `20081128_sulston` (the regression
baseline), and emits `configs/<dataset>.yaml` (ready-to-run; `data_dir` /
`output_dir` point into the same tree), a legacy `.list` file, and
`freeze_report.txt`. Missing-piece handling: missing **CD** = hard error
(series skipped); missing **ACD** = warn (positions can't be built); missing
**TIME** = regenerate from `volume_timestamps` if present, else fall back to
a `minutes_per_timepoint` value (manual flag, or inferred from DB
`delta_seconds`) written into the YAML for `build_inputs.R`. Exposed as CLI
`embryodb phenotyping freeze <dataset> [--output-base] [--minutes-per-timepoint]`
and a "Freeze for phenotyping…" button in the dataset panel. Tests:
`tests/test_phenotyping_freeze.py`.

**Phase 2 — DONE (GetACD stopgap).** `external_tools.run_getacd(series_names,
tools3_dir=...)` wraps the *existing* Perl `GetACD.pl` as a detached
subprocess step (same launcher pattern as `run_extract` / `run_print_trees`):
writes a series-list file, pre-creates the `CDs`/`AuxInfos` scratch dirs the
script copies into, and shells `perl <tools3>/GetACD.pl <list>`. Runs on a
dataset/list (not per-embryo — that's a script limitation). Tests in
`tests/test_external_tools.py::test_run_getacd_*`. **HIGH PRIORITY next
step:** replace this stopgap with the in-progress R `GetACD` rewrite (owned
by someone else — leave their effort alone) and integrate ACD generation
cleanly into the freeze/extract flow.

**Phase 3 — done, byte-validated.** `build_inputs.R` in the
LineagePhenotyping repo (the numeric port of `CompareDivTime.pl` +
`ComparePositions.pl`) produces all four output tables
(`DivTimeNorm.tsv`, `CCLengthNorm.tsv`, `CCLengthMinTerminal.tsv`,
`positions.txt`) **byte-identical** to the committed Perl outputs for
both `die-1` (dataset 412) and `ceh-32_mutant` (dataset 388), freezing
each via `embryodb phenotyping freeze` then running `build_inputs.R`.
`GetAngles_revRotate.pl` is retired (not consumed by the modern pipeline).

**List-order caveat (integration gotcha):** the division tables
(`DivTime*`, `CCLength*`) order their per-series columns by the
freeze list-file order, exactly as the Perl did; `positions.txt` uses
sorted (byte-order) series, so it is order-independent. The freeze emits
the `.list` in DB/query order, which need not match a *historical* Perl
run's list order — so to reproduce an old analysis byte-for-byte you must
feed the same list order (column *values* are identical regardless; only
column order shifts). For new analyses the order is simply whatever the
freeze emits.

## Pending v2 work (next session)

All v2 / v2.1 / v2.2 deliverables are done. Next session likely starts
on one of:
- **v2.5 LineagePhenotyping bridge** — biggest next chunk
- **v2.5 FastAPI tier** — unlocks Mac access without SSH tunnel
- **v3 RedBkgComp7 port** — the first major Java workhorse to port
- One of the future-feature placeholders above

After v2: see plan for v2.5 (LineagePhenotyping bridge + FastAPI),
v3 (Java/Perl reimplementation), v4 (acetree_py + lifecycle).
