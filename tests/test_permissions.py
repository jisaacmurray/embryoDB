"""Tests for the file-permission normalizer (fsutil.normalize_tree +
permissions.normalize_series).

Group ownership isn't asserted: chgrp to `users` may legitimately fail in a
test environment, and `chgrp_if_possible` swallows that by design. Mode bits
are deterministic (os.chmod ignores umask), so those are what we check.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from embryodb import fsutil, permissions
from embryodb.models import Series


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_normalize_tree_sets_modes(tmp_path):
    root = tmp_path / "series"
    sub = root / "dats"
    sub.mkdir(parents=True)
    f1 = root / "matlabParams"
    f2 = sub / "CDx.csv"
    f1.write_text("a")
    f2.write_text("b")
    # Mess up modes the way an external writer (legacy jar / sshfs) would.
    os.chmod(f1, 0o600)
    os.chmod(f2, 0o644)
    os.chmod(sub, 0o700)
    os.chmod(root, 0o755)

    n_dirs, n_files = fsutil.normalize_tree(root)

    assert n_dirs == 2  # root + dats
    assert n_files == 2
    assert _mode(f1) == 0o664
    assert _mode(f2) == 0o664
    # setgid + 0775 on dirs so future writes inherit the group.
    assert _mode(root) == 0o2775
    assert _mode(sub) == 0o2775


def test_normalize_tree_skips_symlinks(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    target = tmp_path / "outside.txt"  # lives OUTSIDE the tree
    target.write_text("x")
    os.chmod(target, 0o600)
    (root / "link.txt").symlink_to(target)

    fsutil.normalize_tree(root)

    # The link's target must be untouched (we never follow symlinks).
    assert _mode(target) == 0o600


def test_normalize_tree_missing_root(tmp_path):
    assert fsutil.normalize_tree(tmp_path / "nope") == (0, 0)


def test_normalize_series_walks_annot_and_image(db_session, tmp_path):
    annot = tmp_path / "annot"
    image = tmp_path / "image"
    (annot / "dats").mkdir(parents=True)
    image.mkdir()
    edit = annot / "dats" / "edit.zip"
    edit.write_text("z")
    os.chmod(edit, 0o600)
    tif = image / "t001.tif"
    tif.write_text("i")
    os.chmod(tif, 0o600)

    db_session.add(
        Series(
            series_name="20260528_test_L4",
            annot_loc=str(annot),
            image_loc=str(image),
        )
    )
    db_session.flush()

    rep = permissions.normalize_series(db_session, "20260528_test_L4")

    assert not rep.missing
    assert rep.n_files == 2
    assert _mode(edit) == 0o664
    assert _mode(tif) == 0o664


def test_normalize_series_dats_only(db_session, tmp_path):
    annot = tmp_path / "annot"
    (annot / "dats").mkdir(parents=True)
    edit = annot / "dats" / "edit.zip"
    edit.write_text("z")
    os.chmod(edit, 0o600)
    other = annot / "other.txt"  # outside dats/
    other.write_text("o")
    os.chmod(other, 0o600)

    db_session.add(
        Series(
            series_name="20260528_test_L5",
            annot_loc=str(annot),
            image_loc=str(annot),
        )
    )
    db_session.flush()

    rep = permissions.normalize_series(db_session, "20260528_test_L5", dats_only=True)

    assert _mode(edit) == 0o664
    assert _mode(other) == 0o600  # untouched — outside dats/
    assert rep.n_files == 1


def test_normalize_series_missing(db_session):
    rep = permissions.normalize_series(db_session, "does-not-exist")
    assert rep.missing
    assert rep.n_files == 0
