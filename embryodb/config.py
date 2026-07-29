import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_user() -> str:
    """Try $USER first, then $LOGNAME, then 'anonymous'."""
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "anonymous"


def _default_command_log_dir() -> Path:
    """Per-user dir under the canonical /murrlab3 root for queued command-job logs.

    Must resolve to the same absolute path on the lab host AND a remote client
    (the Mac maps /murrlab3 via a root symlink), so a client that enqueues a job
    can later read the log a penticton worker wrote at this exact path.
    """
    return Path("/murrlab3") / _default_user() / "embryodb-jobs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EMBRYODB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    source_dir: Path = Field(
        default=Path("/murrlab/gpfs/fs0/l/murr/embryoDB"),
        description="Read-only source XML directory. Never written to.",
    )
    export_dir: Path = Field(
        default=Path("/murrlab/gpfs/fs0/l/murr/new_tools/embryoDB_exports"),
        description="Where DB->XML writes land. Retires when promote-to-source is implemented.",
    )
    db_url: str = Field(
        default="postgresql+psycopg://embryodb@localhost/embryodb",
        description="SQLAlchemy URL. SQLite path acceptable for local dev/tests.",
    )
    user: str = Field(
        default_factory=_default_user,
        description=(
            "Identifier recorded in updated_by / imported_by columns. "
            "Defaults to $USER / $LOGNAME, falls back to 'anonymous'."
        ),
    )

    # AceTree external launcher (legacy Java GUI). Used by the detail panel's
    # "Launch AceTree" button. Mirrors the behaviour of the old EmbryoDB.jar
    # "acetreex" action: `java -mx500m -jar <jar> <annot_loc>/dats/<config>`.
    acetree_jar: Path = Field(
        default=Path("/gpfs/fs0/l/murr/tools3/AceTree_Santella.jar"),
        description="Path to the AceTree jar to spawn for legacy curation.",
    )
    java_command: str = Field(
        default="java",
        description="Java launcher binary (overridable if multiple JREs are present).",
    )
    java_mx: str = Field(
        default="500m",
        description="JVM max-heap argument forwarded to AceTree (-mx<value>).",
    )

    # AceTree-Py (napari-based Python rewrite) — alternative viewer launched
    # from the browser right-click menu. Runs in its OWN venv because it pulls
    # heavy deps (napari, numba, tifffile) the embryoDB GUI environment lacks.
    # Launched as `<acetree_py_python> -m acetree_py gui <config>`; the same
    # AceTree XML config the legacy jar consumes.
    acetree_py_python: Path = Field(
        default=Path("/murrlab/gpfs/fs0/l/murr/new_tools/acetree_py/.venv/bin/python"),
        description="Python interpreter of the acetree_py venv (created via pip install -e '.[gui]').",
    )

    # Pipeline subprocess tools
    tools3_dir: Path = Field(
        default=Path("/gpfs/fs0/l/murr/tools3"),
        description="Directory containing matlab_SN_cluster.pl, acebatch3.jar, etc.",
    )
    tools4_dir: Path = Field(
        default=Path("/gpfs/fs0/l/murr/tools4"),
        description="Directory containing partialCSV.jar (legacy tools4 layout).",
    )
    worker_pidfile_dir: Path = Field(
        default=Path("/tmp"),
        description="Directory for the per-machine worker pidfile.",
    )
    worker_max_slots: int = Field(
        default=3,
        description=(
            "Max concurrent worker processes per host. The cron relauncher and "
            "spawn_worker() each claim the first free slot (0..N-1) via a "
            "per-slot pidfile; the DB atomic claim keeps them from running the "
            "same job. >1 lets independent jobs run in parallel and stops one "
            "wedged job from freezing the whole queue (EMBRYODB_WORKER_MAX_SLOTS)."
        ),
    )
    worker_slab_guard_gib: float = Field(
        default=30.0,
        description=(
            "Refuse to start (or continue) a worker when /proc/meminfo "
            "SReclaimable exceeds this many GiB. Tens of GiB of reclaimable "
            "slab is the stranded-NFS-inode-cache signature that OOM-killed "
            "penticton on 2026-06-26; the worker bails rather than become the "
            "next OOM victim. 0 disables the guard (EMBRYODB_WORKER_SLAB_GUARD_GIB)."
        ),
    )
    worker_memfree_floor_mib: float = Field(
        default=512.0,
        description=(
            "Refuse to start (or continue) a worker when /proc/meminfo MemFree "
            "drops below this many MiB. Truly-free memory near zero means the "
            "next allocation may OOM. Pairs with worker_slab_guard_gib; either "
            "tripping aborts the worker. 0 disables (EMBRYODB_WORKER_MEMFREE_FLOOR_MIB)."
        ),
    )
    starrynite_max_seconds: int = Field(
        default=21_600,  # 6 hours
        description=(
            "Hard wall-clock cap for a single StarryNite run. A run exceeding "
            "this is killed and marked FAILED — a deterministic backstop to the "
            "heuristic CPU/log-idle watchdog, which periodic MCR/java child "
            "processes can defeat (EMBRYODB_STARRYNITE_MAX_SECONDS)."
        ),
    )
    # --- New all-MATLAB StarryNite (release_v1) ---------------------------
    # Optional alternative to the legacy compiled-MATLAB+C StarryNite. Selected
    # per pipeline run via the run_starrynite step's `params.sn_engine == "new"`
    # (default remains "old"). Needs a FULL MATLAB (not the MCR) with Image
    # Processing + Statistics/ML toolboxes; the retrained tracking model is
    # resolved from starrynite_v1_dir/../distribution_lineaging on the path.
    matlab_command: Path = Field(
        default=Path("/gpfs/fs0/l/murr/local/MATLAB/R2026a/bin/matlab"),
        description="Full MATLAB launcher for the new all-MATLAB StarryNite (EMBRYODB_MATLAB_COMMAND).",
    )
    starrynite_v1_dir: Path = Field(
        # FROZEN release copy, committed + isolated from the active dev tree at
        # new_tools/StarryNite/ (whose uncommitted edits break runs -- e.g. an
        # 11-dim perp feature vs this release's 10-dim model). Point production
        # runs here; never at the dev tree. The tracking model + .m code resolve
        # from starrynite_v1_dir/../distribution_lineaging, i.e. the frozen tree.
        default=Path("/murrlab/gpfs/fs0/l/murr/new_tools/StarryNite_release_v1/release_v1"),
        description="Frozen release_v1 dir holding run_starrynite.m, the param template, and effort/.",
    )
    starrynite_tracking_model: str = Field(
        default="MurrayTrackingModel_20260704.mat",
        description="Retrained tracking model .mat name loaded by the new-SN param file.",
    )
    effort_python: Path = Field(
        default=Path(
            "/home/jmurr/anaconda3/envs/"
            "EmbNucEnhancementPYEnvironment20240329Ver/bin/python"
        ),
        description="Python interpreter (numpy/pandas/sklearn/joblib) for the GT-free effort predictor.",
    )
    # --- Production StarryNite (sn_production_driver) ----------------------
    # Selected via `params.sn_engine == "prod"`. A THIRD engine, not a revision
    # of "new": it runs a different entry point (permissive detect -> GT-free
    # nucleus filter -> time-model track), a different tracking model, and takes
    # its tracking parameters from a frozen template rather than the param file.
    #
    # Deliberately NOT frozen (decided by jmurr 2026-07-29): the driver resolves
    # its .m code from the ACTIVE dev tree, so tracker improvements reach
    # production without a release step. The cost is that a lineage's behaviour
    # depends on the tree's state at run time, so every run records the dev-tree
    # git HEAD + dirty files + driver/asset digests in its output_summary. Read
    # that, not the clock, when comparing two prod lineages.
    starrynite_prod_dir: Path = Field(
        default=Path("/murrlab3/jmurr/starrynite_test/release_prod"),
        description=(
            "Dir holding sn_production_driver.m, applyParamDefaults.m, "
            "apply_nucleus_filter.py and assets/ (EMBRYODB_STARRYNITE_PROD_DIR)."
        ),
    )
    starrynite_prod_assets: Path | None = Field(
        default=None,
        description=(
            "Asset dir with nucleus_filter.pkl, tracking_model.mat and "
            "tracking_template.mat. None = <starrynite_prod_dir>/assets "
            "(EMBRYODB_STARRYNITE_PROD_ASSETS)."
        ),
    )
    starrynite_prod_iscale: float = Field(
        default=1.0,
        description=(
            "Permissive intensity scale the driver applies to staging bands "
            "stagelo..stagehi. 1.0 makes that scaling a no-op, which is what the "
            "uniform-threshold variant wants (EMBRYODB_STARRYNITE_PROD_ISCALE)."
        ),
    )
    starrynite_prod_stagelo: int = Field(
        default=3,
        description=(
            "First staging band the permissive scale applies to. Irrelevant at "
            "iscale=1.0 but recorded for provenance "
            "(EMBRYODB_STARRYNITE_PROD_STAGELO)."
        ),
    )
    starrynite_prod_uniform_threshold: float | None = Field(
        default=0.004,
        description=(
            "Flatten parameters.intensitythreshold to this value across ALL "
            "staging bands in a scratch copy of the series' matlabParams. The "
            "shipped band-2 value (0.0075) is a ~1.9x notch above both "
            "neighbours that costs mid-movie recall; flattening it measurably "
            "improved band-2 capture across the 20260716 JIM801 set. None keeps "
            "the file's own values (EMBRYODB_STARRYNITE_PROD_UNIFORM_THRESHOLD)."
        ),
    )
    starrynite_scratch_root: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("EMBRYODB_STARRYNITE_SCRATCH_ROOT")
            or ("/scratch" if Path("/scratch").is_dir() else "/tmp")
        ),
        description=(
            "Scratch root for new-SN runs. New SN NEVER writes into image_loc; it "
            "runs in <root>/embryodb-sn-<series>-<pid>/ and only the final lineage "
            "zip + effort QC are copied back (EMBRYODB_STARRYNITE_SCRATCH_ROOT)."
        ),
    )
    # --- Lineage-zip archiving ---------------------------------------------
    archive_annotations: bool = Field(
        default=True,
        description=(
            "Before a pipeline step overwrites dats/<series>-edit.zip (AceTree's "
            "curation target) or the pristine dats/<series>.zip, copy the existing "
            "ones into dats/archived/<UTC timestamp>/. The zips are ~1 MB, so this "
            "is cheap insurance against a re-pipeline destroying manual curation "
            "(EMBRYODB_ARCHIVE_ANNOTATIONS)."
        ),
    )
    archive_annotations_keep: int = Field(
        default=0,
        description=(
            "How many archive generations to retain per series; older ones are "
            "pruned after each archive. 0 (default) keeps every generation "
            "(EMBRYODB_ARCHIVE_ANNOTATIONS_KEEP)."
        ),
    )
    # --- R-based GetACD (get_acd.R port of GetACD.pl) -----------------------
    get_acd_dir: Path = Field(
        default=Path("/murrlab/gpfs/fs0/l/murr/new_tools/accessory/get_ACD"),
        description=(
            "Directory containing get_acd.R and SupplementalTable2_DivisionTimes.txt "
            "(EMBRYODB_GET_ACD_DIR)."
        ),
    )
    rscript_command: Path = Field(
        default=Path("Rscript"),
        description="Rscript executable for running get_acd.R (EMBRYODB_RSCRIPT_COMMAND).",
    )

    command_log_dir: Path = Field(
        default_factory=_default_command_log_dir,
        description=(
            "Shared, host-agnostic directory for queued command-job logs. Must "
            "resolve to the same absolute path on the lab host and any remote "
            "client so a Mac that enqueued a job can read the log a penticton "
            "worker writes (EMBRYODB_COMMAND_LOG_DIR)."
        ),
    )

    # Remote client mode. Set by the off-network launcher (scripts/embryodb-remote)
    # when the GUI runs on a Mac/laptop talking to the lab DB over an SSH tunnel.
    # In this mode the GUI must NOT spawn a local worker: heavy pipeline steps
    # (StarryNite/extract/measure/staging) need the lab's compiled MATLAB/Java
    # stack and shared storage, so they run on a worker resident on penticton,
    # which claims the same PENDING rows this client enqueues.
    remote: bool = Field(
        default=False,
        description="Off-network client mode; suppresses local worker spawn (EMBRYODB_REMOTE=1).",
    )


settings = Settings()
