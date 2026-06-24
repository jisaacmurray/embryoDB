"""Filesystem helpers with the project's permission discipline baked in.

Every file/directory the pipeline writes goes through this module. Default
policy: mode 0664 for files, 02775 (setgid) for directories, group `users`
(gid 100). The legacy Perl/Java pipeline failed at the extract step on
permissions, which blocked other lab members from accessing series; this is the
fix. Group-write (not world-write) is deliberate: any `users` member can re-save
a series during a curation handoff, without exposing files to every account.

The **setgid bit** on directories is load-bearing: it forces new files into the
dir's group (`users`) regardless of which tool or user writes them — covering
writers that bypass this module (legacy jars, AceTree edits over sshfs, manual
edits). The *group* is then correct passively; only the *mode* still rides on
the writer's umask. `normalize_tree()` is the post-hoc mop for those stragglers
(see `embryodb.permissions` for the series-level / CLI entry point).

Override `EMBRYODB_FILE_GROUP` / `EMBRYODB_FILE_MODE` / `EMBRYODB_DIR_MODE`
in env if a deployment needs different defaults.
"""

from __future__ import annotations

import grp
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

DEFAULT_GROUP = os.environ.get("EMBRYODB_FILE_GROUP", "users")
DEFAULT_FILE_MODE = int(os.environ.get("EMBRYODB_FILE_MODE", "0o664"), 8)
DEFAULT_DIR_MODE = int(os.environ.get("EMBRYODB_DIR_MODE", "0o2775"), 8)


def resolve_gid(group: str) -> int | None:
    """Look up the gid for `group`; return None if the group doesn't exist
    or the current user isn't a member (chgrp would fail)."""
    try:
        gr = grp.getgrnam(group)
    except KeyError:
        return None
    user = os.environ.get("USER") or ""
    if user and user not in gr.gr_mem:
        # Primary-group case: user's effective gid would match without being
        # listed in gr_mem. Accept it if we're already that group.
        if os.getegid() != gr.gr_gid:
            return None
    return gr.gr_gid


_GROUP_GID = resolve_gid(DEFAULT_GROUP)


def chgrp_if_possible(path: Path | str, gid: int | None = None) -> None:
    """`chgrp` to the project group; silent no-op if it would fail
    (cross-filesystem mounts, missing group, etc)."""
    target_gid = gid if gid is not None else _GROUP_GID
    if target_gid is None:
        return
    try:
        os.chown(path, -1, target_gid)
    except (OSError, PermissionError):
        pass


def chmod_if_possible(path: Path | str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def ensure_dir(path: Path | str) -> Path:
    """`mkdir -p` with the project's directory mode + group."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    chmod_if_possible(p, DEFAULT_DIR_MODE)
    chgrp_if_possible(p)
    return p


def safe_write_bytes(path: Path | str, data: bytes) -> Path:
    """Write `data` to `path`, ensuring parent dirs + permissions."""
    p = Path(path)
    ensure_dir(p.parent)
    with _scoped_umask():
        p.write_bytes(data)
    chmod_if_possible(p, DEFAULT_FILE_MODE)
    chgrp_if_possible(p)
    return p


def safe_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    with _scoped_umask():
        p.write_text(text, encoding=encoding)
    chmod_if_possible(p, DEFAULT_FILE_MODE)
    chgrp_if_possible(p)
    return p


def safe_copy(src: Path | str, dst: Path | str) -> Path:
    """Like shutil.copy2 but with the project's mode/group on the destination."""
    s, d = Path(src), Path(dst)
    ensure_dir(d.parent)
    with _scoped_umask():
        shutil.copy2(s, d)
    chmod_if_possible(d, DEFAULT_FILE_MODE)
    chgrp_if_possible(d)
    return d


def safe_symlink(target: Path | str, link: Path | str, overwrite: bool = False) -> Path:
    """Create `link -> target`. By default, refuses if `link` already exists
    unless `overwrite=True`."""
    link_p = Path(link)
    if link_p.exists() or link_p.is_symlink():
        if not overwrite:
            return link_p
        link_p.unlink()
    ensure_dir(link_p.parent)
    link_p.symlink_to(target)
    return link_p


def normalize_tree(root: Path | str) -> tuple[int, int]:
    """Recursively re-apply the project's group + mode to an existing tree.

    The mop for writers that bypass `safe_write*` (legacy Java/Perl jars,
    AceTree curation edits over sshfs, manual edits): brings every file to
    `DEFAULT_FILE_MODE` and every directory to `DEFAULT_DIR_MODE` (setgid), all
    group `DEFAULT_GROUP`. Idempotent. Symlinks are skipped (never followed, so
    link targets outside the tree are untouched). Returns
    `(dirs_touched, files_touched)`.
    """
    root = Path(root)
    n_dirs = n_files = 0
    if not root.exists():
        return (0, 0)
    if root.is_dir() and not root.is_symlink():
        chmod_if_possible(root, DEFAULT_DIR_MODE)
        chgrp_if_possible(root)
        n_dirs += 1
    elif root.is_file() and not root.is_symlink():
        chmod_if_possible(root, DEFAULT_FILE_MODE)
        chgrp_if_possible(root)
        return (0, 1)
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            p = Path(dirpath) / d
            if p.is_symlink():
                continue
            chmod_if_possible(p, DEFAULT_DIR_MODE)
            chgrp_if_possible(p)
            n_dirs += 1
        for f in filenames:
            p = Path(dirpath) / f
            if p.is_symlink():
                continue
            chmod_if_possible(p, DEFAULT_FILE_MODE)
            chgrp_if_possible(p)
            n_files += 1
    return (n_dirs, n_files)


@contextmanager
def _scoped_umask(value: int = 0o002):
    """Force umask 0o002 for the duration of a write so group bits stick.

    Restores the prior umask on exit even if the write raises.
    """
    old = os.umask(value)
    try:
        yield
    finally:
        os.umask(old)


__all__ = [
    "DEFAULT_GROUP",
    "DEFAULT_FILE_MODE",
    "DEFAULT_DIR_MODE",
    "resolve_gid",
    "chgrp_if_possible",
    "chmod_if_possible",
    "ensure_dir",
    "safe_write_bytes",
    "safe_write_text",
    "safe_copy",
    "safe_symlink",
    "normalize_tree",
]
