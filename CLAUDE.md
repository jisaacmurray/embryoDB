# embryoDB — agent orientation

Python rewrite of the Murray Lab's Java `EmbryoDB.jar` (C. elegans embryo
lineage metadata DB) plus its `Stellaris_tif_pipeline*.pl` import pipeline.
This document is for AI assistants picking up the project. Live state is
the code; this is a map.

> **⚠️ NEVER recursively walk `/murrlab`, `/murrlab2`, `/murrlab3` (agents
> AND code).** These NFS mounts hold embryoDB's image tree: **every** embryo
> directory contains *tens of thousands* of TIFF files, and there are
> *thousands* of embryos. A recursive enumeration over a mount root — `find`,
> `ls -R`, `du`, `glob('**')`, `rglob`, `os.walk`, `pathlib` recursion, a
> Snakemake input-glob, or even `cd`-ing into such a tree so a tool indexes it —
> caches one NFS inode + dentry per entry. Over the full tree that is tens of
> millions of kernel slab objects which the NFS client will **not** release on
> demand, and it OOM-killed `penticton` on 2026-06-26 (zero swap → hard kill,
> required a reboot). Rules: (1) **discover files via the Postgres DB, never the
> filesystem.** (2) If you must walk, root it at the **narrowest** known subtree
> (a single series' `tif/`), never a `/murrlab*` mount point or a user's
> `images/` dir, and process/stat in bounded batches. (3) Launch `claude` /
> `embryodb` from a small **local** dir and pass NFS paths as arguments — never
> start a tool *inside* a million-file NFS tree. (4) When reviewing a diff,
> reject any new unbounded recursive traversal rooted on these mounts.
>
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
| `docs/portability.md` | What another lab would hit porting embryoDB to a different OS / directory layout / no-cluster environment (MCR v714, Java deps, hard-coded paths, POSIX permission model, …). Forward-looking, not a current work item. |
| `docs/data_access.md` | Self-contained how-to for a **consumer** program (or AI agent) that needs to read embryoDB data without the full dev context: connect via `EMBRYODB_DB_URL`, query via CLI exports or the `queries/` Python API, the data model, and where on-disk `dats/` files live. |
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
| **v2.9 — One-command remote GUI** | done | `scripts/embryodb-remote` opens (or reuses) a nested bastion→penticton SSH tunnel to Postgres via `ControlMaster`, sources the secret `EMBRYODB_DB_URL` from a chmod-600 file, and launches `embryodb-gui` flagged `EMBRYODB_REMOTE=1`. In remote mode `spawn_worker()` is a no-op so heavy jobs run on a penticton-resident worker (the GUI just enqueues PENDING rows). Assumes lab paths resolve locally via mount + root symlinks (`/murrlab3` etc.), so AceTree needs no path remap. Full recipe: `docs/remote_access.md`. (FastAPI tier still deferred — tunnel suffices for single-user Mac.) |
| **v3 — Reimplement remaining Java/Perl tools** | pending | The bigger remaining chunk. See "Legacy tools currently called" below + the v3 ordering note. **StarryNite track** is planned separately and **license-gated** — see `docs/starrynite_modernization.md` (adopt 2025 all-MATLAB upstream + retrain on the curated corpus; needs MATLAB + Image Processing + Statistics/ML, plus Compiler for the free-MCR cluster build). Reference checkout at `../StarryNite/`. |
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
  legacy AceTree only displays 2. The role `skip` drops a channel
  entirely — nothing is read or written for it (use it to decline a DIC
  channel; `--channel-role 2=skip`, or the `skip` entry in the GUI LIF
  dialog's role dropdown). The histone channel may not be skipped.
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
- **Appending extra movies onto a time course.** The scope sometimes saves a
  late extra volume as a SEPARATE LIF object (e.g. a 1-tp `..._t241` next to a
  240-tp main series) rather than as timepoint 241 of the main acquisition.
  `import_lif` appends such a movie's frames onto the END of each matching
  position's time course — matched by position NAME, numbered with a running
  offset so StarryNite/AceTree see one continuous movie. The combined
  `total_nt` drives `Series.timepts`, the AceTree `<end index>`, and
  matlabParams `end_time`.
  - **Appendability is structural, not name-based** (`series_compatible_for_append`
    in `lif/import_flow.py`): a candidate must share the main's per-volume
    geometry (channel count, plane count, X/Y pixels) AND at least one position
    name. Timepoint count is intentionally not compared. Don't trust the
    `_t<N>` name — users aren't reliable about it; it's only used as the
    *auto-check* heuristic (`extra_timepoint_siblings`).
  - **Auto vs. explicit.** `auto_append_extra` (default on) auto-appends only
    the confident `_t<N>` siblings. ANY other compatible movie
    (`appendable_candidates`) must be named explicitly via `--append-series`
    (CLI) or ticked in the GUI's compatible-movie checklist. Both paths are
    compatibility-gated — incompatible names are warned about and skipped.
    The GUI passes its full checklist selection as `append_series` with
    `auto_append_extra=False` (it owns the selection). CLI opt-out of the auto
    `_t<N>` behavior: `--no-auto-append`.
  - Caveat: timestamps still come from the main position's `Properties.xml`,
    so appended frames get no TIME row — fine, since only phenotyping reads
    TIME csvs.
  - **Re-import updates the count.** `_import_one_position` always sets
    `series.timepts = str(total_nt)` for an existing series (not just when
    blank), so re-importing a movie that gained a late `_t<N>` volume raises
    the stored count (240 → 241). The AceTree config and matlabParams are
    regenerated each import and carry the new count too. The legacy embryoDB
    XML (`step_write_embryodb_xml`) deliberately never *overwrites* a new-series
    file during staging, but every import/edit path now re-syncs the mirror
    after its session commits (see "legacy XML mirror" below), so `<timepts>`
    and other fields no longer go stale on a re-import. DB + pipeline configs
    remain authoritative; the XML is a parallel record for the old Java/Perl
    tools (Tree1/Measure).

- **user vs. person guards** (`identity.py`, GUI-free core shared by CLI + GUI).
  Two distinct identity fields, NOT interchangeable:
  - **user** = the OS/login account that OWNS staged files; it picks the
    `/murrlab3/<user>/images/<series>/` tree. Restricted to real accounts
    (`system_users()` = login accounts with uid ≥ 1000 ∪ existing image-tree
    owners), defaults to whoever runs embryoDB (`current_user()`). Picking a
    different user is allowed but warned: files are still WRITTEN by the runner,
    so they end up owned by the wrong uid → permission grief. GUI presents user
    as a non-editable dropdown; CLI `--user` is checked by `_user_person_warnings`
    and prompts unless `--yes`.
  - **person** = free-form scientific attribution (e.g. `jmurr` pipelining a
    movie `eli` collected records `person="eli"`). Not tied to the filesystem.
    Guarded only against typos: a brand-new name (absent from
    `known_persons()` = distinct `Acquisition.person` ∪ `Series.person`) gets an
    "are you sure?" confirm before it's created. GUI presents person as an
    editable combo of known names; CLI warns on a new person.
  - The import_wizard previously conflated the two (the Person field doubled as
    the directory owner) — they're now separate widgets. Don't reintroduce
    `user = person or settings.user`.

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
- CLI `import-acquisition` / `import-lif` — `_sync_legacy_xml_after_import`
  (`cli.py`) syncs every non-failed series after the import session block.
  The GUI LIF dialog spawns the CLI detached, so it inherits this.
- `ImportWizard.accept` — `sync_many(...)` after the inline-import session
- `pipeline/subprocess_steps.py` — after the worker updates `series.timepts`
  from the staged file count, it re-syncs that series so the re-import count
  lands in the XML too.

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

## CLI ↔ GUI parity

Design rule (stated by jmurr 2026-06-10): **every GUI menu/panel action
should have a CLI equivalent.** Both interfaces route through the same
`queries` / pipeline / `external_tools` modules, so a CLI command and its
GUI counterpart share one code path.

Parity launchers added in this pass (all reuse the GUI's underlying
functions):

| CLI | GUI counterpart | Shared core |
|---|---|---|
| `embryodb pipeline rerun <series…\|-d DATASET> [--step] [--set k=v] [--run/--no-run]` | Re-run pipeline… dialog | `pipeline/rerun.py::requeue_series` (also used by `gui/rerun_dialog.py`) |
| `embryodb extract <series…\|-d DATASET> --step/--all [--list-steps]` | Run extract steps… | `external_tools.run_extract` |
| `embryodb print-trees <series…\|-d DATASET> [--min/max-expr …]` | Print trees… | `external_tools.run_print_trees` |
| `embryodb jobs [--running]` | Background jobs… | `jobs.list_jobs` |
| `embryodb launch-acetree <series>` | Launch AceTree button | `external.launch_acetree` |
| `embryodb launch-acetree-py <series>` | Browser right-click → **Open in AceTree (Python)** | `external.launch_acetree_py` |
| `embryodb pipeline recover <series…\|-d DATASET> [--action auto\|truncate\|stub] [--min-prefix N] [--run/--no-run]` | _(automatic in worker; CLI is the manual escape hatch)_ | `pipeline/sn_recovery.py::recover_series` |
| `embryodb pipeline stub <series…\|-d DATASET>` | _(automatic in worker; CLI is the manual escape hatch)_ | `pipeline/stub_annotation.py::write_stub_for_series` |
| `embryodb audit-permissions <series…\|-d DATASET>` | _(read-only; no GUI twin yet)_ | `permissions.audit_series` |
| `embryodb fix-permissions <series…\|-d DATASET> [--dats-only]` | "Fix permissions" button | `permissions.normalize_series` |

`cli.py::_resolve_series_arg(series, dataset)` is the shared "names or
`--dataset`" resolver for rerun/extract/print-trees.

**Still GUI-only (deferred backlog, not yet ported):** single/bulk
**metadata edit** (detail-panel Save / Bulk edit metadata…), **Mark for
deletion** (sets `deleted_at`; only the *purge* side, `gc-deleted`, has a
CLI), and the two interactive file editors (**Edit AceTree config**, **Edit
AuxInfo**) — these open an in-GUI text editor, so a CLI equivalent would
just print the path. Add these when convenient to complete parity.

## File permission policy

**Goal: curation of a series passes from one lab member to another, so any
`users`-group member must be able to modify and re-save a series' files.** The
policy that guarantees this:

| What | Value | Why |
|---|---|---|
| Group | `users` (gid 100 — currently jmurr, jrumley, azach) | shared access for the whole lab |
| Files | `0664` — **group-writable, not world-writable** | any `users` member can re-save; outsiders can't |
| Dirs | `0775` | same, plus traversal |
| umask | `0002` during writes | keeps the group bits from being stripped |

This is implemented in `embryodb/fsutil.py` (`safe_write_bytes` /
`safe_write_text` / `safe_copy` / `ensure_dir`, all via `_scoped_umask` +
`chmod_if_possible` + `chgrp_if_possible`). **Every write the Python layer makes
routes through it** — staged TIFs, AceTree XML, matlabParams, legacy XML,
symlinks. Overridable per-deployment via `EMBRYODB_FILE_GROUP` /
`EMBRYODB_FILE_MODE` / `EMBRYODB_DIR_MODE`. Group-write (not world-write) is the
deliberate choice: it gives the handoff without making files writable by every
account on the host.

**The gap — writes that DON'T go through `safe_write`:**

- **Java AceTree curation edits** (e.g. the `<series>-edit.zip` AceTree saves),
  especially **over sshfs from a remote Mac**. These land as the *editor's
  personal* user:group (e.g. `jmurr:jmurr`) and, per the Mac's umask over
  sshfs, often `0666` (world-writable). They satisfy "another user can re-save"
  only via the world-write bit — which is fragile: if anything later tightens
  them to `0664` *without* also `chgrp users`, the personal group locks the
  next curator out. (Confirmed on `20260528_ceh-27_JIM593_L4/dats/*.zip`,
  2026-06-23.)
- **Legacy Java/Perl extract tools** (`acebatch3.jar`, `GetACD.pl`, …) — they
  write with whatever umask/group the spawning shell had.
- **Any manual edit** over the mount or on the lab host.

**Two structural reasons the group isn't auto-inherited:** the per-series
`dats/` dirs have **no setgid bit** (so a new file takes the creator's primary
group, not the dir's `users`), and sshfs writes carry the *remote* machine's
umask. A setgid on the tree would fix the group-inheritance half; it wouldn't
fix the world-writable mode from a permissive remote umask.

**To normalize a series after external editing** (bring AceTree/handoff files
back to policy so the next curator inherits clean perms):

```bash
chgrp -R users <annot_loc>             # e.g. /murrlab3/azach/images/<series>
find <annot_loc> -type f -exec chmod 0664 {} +
find <annot_loc> -type d -exec chmod 2775 {} +   # 2 = setgid, so future writes inherit `users`
```

Two CLI commands wrap this over `fsutil`/DB-resolved series (never a mount
walk):

- `embryodb audit-permissions <series…|-d DATASET>` — **read-only.** Stats each
  series' `dats/`, `matlab/`, `MLtemp/` and `matlabParams` (the modifiable
  outputs; `tif/`/`tifR/` are skipped so a whole-dataset audit stays bounded)
  and reports every entry that isn't group `users` + group-writable (dirs
  setgid). Exits non-zero if any issue is found. `permissions.audit_series`.
- `embryodb fix-permissions <series…|-d DATASET> [--dats-only]` — **mutating.**
  Applies the recipe via `fsutil.normalize_tree`. `permissions.normalize_series`.
  Ownership-gated: entries owned by another user are silently skipped (chgrp/chmod
  need ownership or root), so audit sees everything but a non-root fix may not
  correct everything. The **setgid on dirs** is the durable half — it stops the
  next sshfs write from re-introducing the wrong group.

ACL presence (the `+` in `ls -l`) is intentionally *not* audited: the GPFS
mounts carry a default ACL on every entry, so it would flag everything.

## Gotchas

- **PySide6 doesn't launch on the lab cluster.** Missing
  `libxcb-cursor0`. Use `QT_API=pyqt5` (already installed). qtpy makes
  the GUI binding-agnostic.
- **Legacy AceTree is hardcoded to 2 channels** (`tif/` + `tifR/`).
  Multichannel acquisitions stage extras to `tifC<n>/` for future viewers
  but legacy AceTree ignores them. The current jar (`AceTree_Santella.jar`)
  *does* take 3 channels via `<image numChannels="3" channel1/2/3=...>` (no
  `file=` attr), but channel→color is hard-coded GREEN/RED/BLUE — the 3rd
  (DIC) lands as blue. **AceTree-Py** (the napari rewrite at
  `../acetree_py`) reads the same config and is the path to grayscale/extra
  channels; launch it from the browser right-click → **Open in AceTree
  (Python)** (`external.launch_acetree_py`, CLI twin `launch-acetree-py`).
  It runs in its OWN venv (`acetree_py/.venv`, `settings.acetree_py_python`,
  env `EMBRYODB_ACETREE_PY_PYTHON`) because it pulls napari/numba; the
  launcher raises a helpful `LaunchError` if that venv isn't built yet.
- **Source-dir is the writer of record for the legacy Java GUI.**
  v2's `write_embryodb_xml` lands new acquisitions there so both systems
  see them. It explicitly refuses to overwrite existing files. Edits to
  imported series go to `export-dir` — a future `promote-to-source`
  operation will replace that staging once trust is built.
- **`/murrlab3/<user>/images/<series>` is canonical**, with an alias at
  `/murrlab/<user>/images/<series>`. Some legacy code uses the alias.
  The pipeline creates the symlink automatically.
- **StarryNite detection collapse → recovery.** When a movie is dim /
  photobleaches / drifts out of frame, MATLAB detection finds ~0–3 "nuclei"
  per timepoint where there should be hundreds; the C tracer can't lineage
  that and wedges (no "End time"), so `run_starrynite` FAILs. On failure
  `step_run_starrynite` runs `pipeline/sn_recovery.py::analyze_series_collapse`
  (counts `tif/<series>_matlabnuclei/matlabnuclei<N>` line counts; a timepoint
  is "low" only if it is below 25% of the running peak **and** ≤10 nuclei — more
  than 10 nuclei is always treated as traceable; collapse = detection peaked ≥20
  then ≥4 consecutive low timepoints) and, if recognized, **recovers
  automatically** in the worker via `sn_recovery.py::auto_recover` (no menu item
  / user step — the earlier "Recover StarryNite…" GUI action was removed). When
  a long healthy prefix exists it re-runs `run_starrynite` truncated to
  `end_time=K` (K ≥ 30 timepoints, the drift/late-bleach case): the failed run
  is reset to PENDING and the worker re-claims it, so `step_run_starrynite` must
  **not** finalize that run. Otherwise it writes a **stub annotation**
  (`pipeline/stub_annotation.py`) and the run stays FAILED with a note (so
  downstream steps don't run on a fake lineage) so the raw images still open in
  AceTree. **Loop guard:** truncation compares K against the *current*
  `matlabParams` `end_time` (`_current_end_time`), not the on-disk file count —
  a prior truncate leaves stale `matlabnuclei` files whose low tail re-reads as a
  collapse; guarding on `end_time` bounds recovery to a single truncate. Manual
  escape hatches remain on the CLI (`embryodb pipeline recover`/`stub`). The
  stub is built from the **real MATLAB detection** when it survives (one
  `t<NNN>-nuclei` per timepoint from `matlabnuclei`/`matlabdiams`, unlineaged —
  verified pass-through `x y z`→`x,y,z.0`, diam, `Nuc<i>`), so you can see where
  the detected nuclei are before retuning params; it falls back to a single
  placeholder nucleus (the legacy artifact format) only when no detection output
  exists. **High-priority follow-up:** the
  truncated retry currently re-runs MATLAB detection on 1..K (wasteful); a
  tracer-only re-run on the already-computed nuclei would be cheaper, and the
  real fix is a tracer that degrades gracefully (planned v3 rewrite).
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
`embryodb phenotyping freeze <dataset> [--output-base] [--minutes-per-timepoint]
[--expression-file PATH] [--expression-series SERIES]`
and a "Freeze for phenotyping…" button in the dataset panel. Tests:
`tests/test_phenotyping_freeze.py`.

**Expression (CA) file selection.** `run_pipeline.R`'s peak-expression
annotation needs a one-row-per-cell `blot` table (a CA file; read by
`functions.R::ReadPeakExpression` via `row.names=2`). The freeze resolves it
into `expression.csv` and sets `expression_file:` in the YAML:
`--expression-file PATH` (a per-timepoint CD/SCD/ACD — cells repeat — is
collapsed to a CA via **truncated-mean of `blot` per cell**; a one-row-per-cell
file is copied verbatim), or `--expression-series SERIES` (prefers that series'
`CA<series>.csv`, else generates from its SCD/CD/ACD). When neither is given the
YAML omits `expression_file` and `run_pipeline.R` falls back to a bundled
`LineagePhenotyping/data/CA*.csv` placeholder. **Caveat:** generated CA matches
the lab's real (EPIC/Java `SeriesSulstonizer`) CA files only ~half the cells
exactly — the rest differ from Java smoothing / valid-observation rules we don't
reproduce; for biological exactness pass a real CA via `--expression-file` /
`--expression-series`. The `CD_to_CA.pl` `×10` scaling is a stale convention not
present in current data, so it is **not** applied.

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
