# Next session — pick up v2 completion

Paste (or adapt) the following as the opening prompt for the next session.
It points the agent at the orientation doc, the master plan, and the
specific deliverables that finish v2.

---

I'm picking up the v2 pipeline-import work on the embryoDB rewrite. Before
suggesting anything please read in this order:

1. `/murrlab/gpfs/fs0/l/murr/new_tools/embryoDB/CLAUDE.md` — orientation
   doc covering layout, status, architecture, gotchas
2. `~/.claude/plans/ok-as-you-see-staged-gizmo.md` — master plan with
   the v1/v1.1/v2/v2.5/v3/v4 milestones and design rationale
3. `~/.claude/projects/-murrlab-gpfs-fs0-l-murr-new-tools/memory/` —
   cross-session memory, especially `project_embryodb_v1.md`

v1 + v1.1 + the v2 orchestration layer are done. 49 tests pass. The 6
inline steps (`stage_images`, `stage_metadata`, `write_acetree_config`,
`write_embryodb_xml`, `create_alias_symlink`, `write_matlab_params`) run
end-to-end on the test fixture at
`../embryoDB_test_data/20250527_JIM783_efl-3_test/`.

**Deliverables for this session (rough order):**

1. **`run_starrynite` step.** Wrap the legacy Matlab pipeline as a
   subprocess. Reference: `/murrlab/gpfs/fs0/l/murr/tools3/matlab_SN_cluster.pl`
   (especially line 114 for command-line shape) and
   `/murrlab/gpfs/fs0/l/murr/tools3/matlabRunner.pl`. Capture stdout +
   stderr to a log file under `PipelineStepRun.log_path`. Set status to
   RUNNING with a heartbeat, COMPLETE on exit 0, FAILED otherwise with a
   tail of stderr in `error_excerpt`.

2. **`run_red_extract` + `run_measure` steps.** Wrap
   `acebatch3.jar RedExtractor1/Measure1` invocations:
   ```
   nice java -mx500m -cp /gpfs/fs0/l/murr/tools3/acebatch3.jar \
        <SubClass> <series_list_file>
   ```
   Same audit-row pattern as run_starrynite. Skip with status SKIPPED if
   `Protocol.channel_map` has no `reporter` role.

3. **Background worker process.** Per-machine queue that picks up PENDING
   PipelineStepRun rows in series order (`series.id` ascending) and runs
   them sequentially. Properties:
   - GUI is non-blocking — enqueue, then carry on
   - Worker survives GUI close (`subprocess.Popen` with
     `start_new_session=True`); pidfile + heartbeat so the GUI knows
     whether to spawn a new one or attach to a running one
   - Crash-safe — if a step dies mid-run, the row stays RUNNING with a
     stale heartbeat; re-running the worker picks it up and reruns from
     scratch (steps are idempotent enough — overwrite outputs)
   - One worker per machine for now; multi-host is later

4. **GUI import wizard.** Multi-page `QDialog` reachable from File menu.
   - Page 1 (Source): pick acquisition dir + Protocol + parser; preview
     discovered positions (Leica filename parser already discovers them,
     just surface in a `QTableWidget`).
   - Page 2 (Metadata): per-acquisition fields (person, strain,
     perturbation, reporter, comments) + parameter override table
     pre-filled from Leica metadata where possible. Tunable knobs are
     listed in `embryodb/parsers/matlab_params.py::TUNABLE_KEYS`.
   - Page 3 (Targets): confirm `image_loc_root`, `alias_root`,
     `legacy_xml_dir`; show estimated disk usage.
   - Page 4 (Confirm): list of series to be created; Submit enqueues
     work via the worker.

5. **Live status updates in browser.** QTimer polls
   `PipelineStepRun.heartbeat_at` for series with RUNNING runs and
   refreshes the Pipeline column without a full table reload.

## Constraints / cautions

- **Don't break the safe-mirror property.** After any change touching
  import or export paths, run `embryodb audit-import` — it must still
  report 0 byte diffs. The trust anchor.
- **Don't change the schema without considering migration.** SQLite dev
  DBs are throwaway, but the deployment target is PostgreSQL on a shared
  lab server. Additive changes (new columns / tables) are safe; renames
  / drops need a migration story.
- **Every file write goes through `embryodb.fsutil.safe_write*`.** Don't
  use `Path.write_text` or `shutil.copy` directly — those bypass the
  permission discipline that fixes the legacy "other lab members can't
  access this" bug at the extract step.
- **PySide6 doesn't launch on the lab cluster.** Use `QT_API=pyqt5`.
- **`/gpfs/fs0/l/murr/embryoDB/` is read-only for existing files.** New
  acquisitions may add new files there; existing files must never be
  overwritten.

## Verification before committing

1. `pytest tests/ -q` — must still pass (49+)
2. `embryodb audit-import` — must report 0 byte diffs
3. End-to-end smoke against the test fixture:
   `embryodb pipeline import-acquisition ../embryoDB_test_data/20250527_JIM783_efl-3_test --protocol Stellaris_JIM113 --image-loc-root /tmp/embryodb-pipeline-test/images --alias-root /tmp/embryodb-pipeline-test/alias --legacy-xml-dir /tmp/embryodb-pipeline-test/legacy_xml --user jmurr --person jmurr`
   produces 7 Series with all inline steps COMPLETE.
4. With Matlab + StarryNite installed (lab cluster only), the new
   `run_starrynite` step actually produces `dats/<series>-edit.zip`.

## Stop conditions / when to ask

- If the worker design starts to need real IPC (Redis, RabbitMQ, etc),
  stop and discuss — the per-machine SQLite-polling queue is the
  intended ceiling for v2.
- If StarryNite wrapping requires non-trivial Matlab parameter munging,
  surface it as a question rather than guessing.
- If the import wizard layout starts pulling toward >4 pages, stop and
  redesign — that's a smell.

Don't push to GitHub. Make local commits as you go. The human will
decide when to push.
