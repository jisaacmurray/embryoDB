"""Launchers for external tools (AceTree, future StarryNite).

Wrappers that mirror the legacy EmbryoDB.jar action-button behaviour so the
new GUI can defer to the existing Java tooling for curation while the
Python rewrite catches up.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import settings
from .models import Series


class LaunchError(RuntimeError):
    pass


def acetree_config_path(row: Series) -> Path:
    """Resolve the config file the legacy Java GUI passes to AceTree.

    Matches `runAceTree` in EmbryoDB.java: `<annot_loc>/dats/<acetree_config>`.
    """
    if not row.annot_loc:
        raise LaunchError("series has no annot_loc; cannot resolve AceTree config")
    if not row.acetree_config:
        raise LaunchError("series has no acetree_config; nothing to launch")
    return Path(row.annot_loc) / "dats" / row.acetree_config


def launch_acetree(row: Series, jar: Path | None = None) -> subprocess.Popen:
    """Spawn the legacy AceTree jar against this series' config file.

    Detached process: the GUI does not wait for it. Returns the Popen handle
    so callers can monitor PID / exit if they want, but the typical use is
    fire-and-forget.

    Raises LaunchError if the config file or jar are missing.
    """
    config_path = acetree_config_path(row)
    if not config_path.exists():
        raise LaunchError(f"AceTree config not found: {config_path}")
    jar_path = Path(jar or settings.acetree_jar)
    if not jar_path.exists():
        raise LaunchError(f"AceTree jar not found: {jar_path}")
    cmd = [
        settings.java_command,
        f"-mx{settings.java_mx}",
        "-jar",
        str(jar_path),
        str(config_path),
    ]
    # Popen with stdin/stdout/stderr to DEVNULL so the GUI session and the
    # child are fully decoupled.
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # don't kill child if GUI exits
    )
