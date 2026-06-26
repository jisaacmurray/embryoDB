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
