# Portability — running embryoDB in another lab's environment

This is a forward-looking assessment, not a current work item. It captures
the obstacles a different lab (different Linux distro, or macOS / Windows,
different directory layout, no access to the Murray Lab cluster) would hit
trying to stand up embryoDB, and what a port would have to address.

The Python/ORM core is already fairly portable — the test suite runs on
in-memory SQLite and the schema is vendor-agnostic. The hard parts are the
**legacy compiled toolchain** the worker shells out to and a set of
**baked-in environment assumptions**. Roughly worst-to-mildest:

## 1. The compiled legacy toolchain (the dominant blocker)

The Python layer is orchestrator + UI; the actual analysis still runs in
legacy Matlab / Java / Perl binaries under `/gpfs/fs0/l/murr/tools3/`
(see CLAUDE.md "Legacy tools currently called").

- **Compiled-Matlab StarryNite needs MCR v7.14 (MATLAB R2010b).** Called via
  `pipeline.subprocess_steps.step_run_starrynite` → `matlab_SN_cluster.pl` →
  `run_commandLineDriver.sh` + the `starrynite` binary. The MCR is
  architecture- and glibc-locked: there is no realistic macOS/Windows MCR of
  that vintage, and even a *newer* Linux breaks it on glibc drift. The
  StarryNite C tracer is a compiled binary that must be rebuilt per platform.
  This is the single biggest obstacle.
- **Java extract tools** (`acebatch3.jar`: RedExtractor1, Measure1, RedExcel1/2,
  Align1; `acexpress_CL2.jar`: Tree1; `partialCSV.jar`; `AceTree_Santella.jar`).
  Java travels better than MCR, but the jars must come along and they assume
  Unix paths, a specific working-dir layout, and headless AWT for image I/O.
  Needs a compatible JRE.
- **Perl glue** (`matlab_SN_cluster.pl`, `GetACD.pl`, `ProcessTime.pl`,
  `UpdatePermissions.pl`, the LineagePhenotyping Perl). Perl is nominally
  cross-platform, but these shell out to `cp`, assume POSIX paths, and need
  CPAN modules (`Statistics::Descriptive`). `UpdatePermissions.pl` is
  inherently Unix (chmod/chgrp).

**Pragmatic fix: containerize, don't port.** A Singularity/Docker image that
freezes MCR + JRE + Perl + the compiled binaries sidesteps every
version/glibc/JRE issue at once, and Singularity is cluster-friendly. For
macOS/Windows the only sane routes are that container in a Linux VM, or the
modern reimplementations once they land (the in-progress R `GetACD` rewrite;
the planned v3 numpy/scipy ports of the Java workhorses).

## 2. Hard-coded paths and the Unix multi-user model

- Image storage (`/murrlab3/<user>/images/<series>/`, alias
  `/murrlab/<user>/images/<series>/`) and the phenotyping output base
  (`/murrlab3/<user>/phenotyping/<dataset>/`) are baked into conventions.
  `tools3_dir` is parameterized in some `external_tools` functions, but the
  storage roots are not — they'd need to become config.
- **`fsutil.safe_write` enforces a POSIX permission/group discipline**
  (umask 0002, chmod 0664 files / 0775 dirs, chgrp `users`). Fine on macOS;
  **meaningless on Windows** — those helpers would need a no-op branch. The
  whole design assumes a shared multi-user POSIX filesystem.

## 3. Execution model assumes a shared cluster

The per-host serial worker, pidfile, heartbeat/watchdog, multi-host atomic
claim (`worker._claim_next`), and `MCR_CACHE_ROOT` isolation all assume a
**shared filesystem (GPFS) visible to every host**, and `matlab_SN_cluster.pl`
implies cluster job submission. A single workstation or a cloud deployment
would need a different execution/queueing model — it does not "just run" on a
laptop.

## 4. Database

`EMBRYODB_DB_URL` defaults to a local PostgreSQL with a baked credential. The
ORM is portable (tests run on SQLite), but production assumes a running
Postgres; another lab must stand that up and externalize the connection
string + secret.

## 5. Microscope / format coupling

Import is built around **Leica LIF** (Stellaris) via the
`parsers/filename.py` + `parsers/leica_metadata.py` + `parsers/timestamps.py`
chain. A lab on Zeiss `.czi` or Nikon `.nd2` needs a different import path
*and* different metadata extraction (voxel xy/z, timestamps), and the
metadata→`matlabParams` resolution mapping is microscope/protocol specific.
The filename and timestamp parsers are already plugin registries (designed
for this), which is the right seam — but only Leica is implemented.

## 6. GUI / visualization stack

- **Qt** (`qtpy`) is already fragile — PySide6 fails to launch on the lab
  cluster (missing `libxcb-cursor0`); `QT_API=pyqt5` is the workaround. Native
  Qt libs and the X11/Wayland story differ per OS.
- The eventual `acetree_py` (napari) brings a heavy OpenGL/GPU stack with its
  own per-OS quirks.

## 7. Biology / reference assumptions (environment, not OS)

The Sulston reference embryo `20081128_sulston`, `SupplementalTable2_DivisionTimes.txt`,
`CellNames.csv`, and the hardcoded parent/sister lineage maps in the
LineagePhenotyping `functions.R` are **C. elegans-specific and baked in**.
Same-organism labs are fine; anyone else faces a deeper rewrite.

## 8. Cross-platform reproducibility gotchas

The byte-identical validation convention (audit-import; the `build_inputs.R`
golden masters) is sensitive to **locale, sort order, and float formatting**
(e.g. Perl's `int($seconds/6)/10`) and R's `check.names` column sanitization.
"Same outputs" across OSes is not guaranteed even when the code runs.

## Bottom line

- The dominant blocker is the compiled MCR/Java/Perl detection+extract stack;
  the cleanest answer is a Linux container, which also neutralizes most of #1.
- The remaining work is config-izing hard-coded paths/secrets, a non-POSIX
  branch in `fsutil` for Windows, decoupling the execution model from the
  shared-cluster assumption, and abstracting the microscope-import layer.
- **macOS:** feasible with effort, mostly via container. **Windows:** the most
  divergent, because of the permission model and path semantics.

## Note for future development

When adding features, prefer choices that don't deepen these couplings:
keep new external-tool paths parameterized (not hard-coded under
`/gpfs/.../tools3` or `/murrlab3`), route all filesystem writes through
`fsutil` so a future non-POSIX branch is the only change needed, add new
microscope/vendor support through the existing parser registries rather than
forking the pipeline, and read DB/connection details from config. None of
this needs doing now, but avoiding *new* hard-coded assumptions keeps an
eventual port tractable.
