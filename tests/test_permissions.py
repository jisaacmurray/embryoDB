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


# --- read-only audit --------------------------------------------------------


def _issue_for(rep, path: Path):
    return next((i for i in rep.issues if i.path == str(path)), None)


def _ignore_group(monkeypatch):
    """Disable the group check so mode-bit assertions are deterministic (test
    files carry the runner's group, not `users`)."""
    monkeypatch.setattr(permissions, "_target_gid", lambda *a, **k: None)


def test_audit_series_flags_bad_modes(db_session, tmp_path, monkeypatch):
    _ignore_group(monkeypatch)
    root = tmp_path / "series"
    dats = root / "dats"
    dats.mkdir(parents=True)
    bad_file = dats / "edit.zip"
    bad_file.write_text("z")
    os.chmod(bad_file, 0o644)  # not group-writable
    good_file = dats / "CDx.csv"
    good_file.write_text("c")
    os.chmod(good_file, 0o664)
    os.chmod(dats, 0o775)  # dir missing setgid

    db_session.add(
        Series(series_name="20260528_aud_L1", annot_loc=str(root), image_loc=str(root))
    )
    db_session.flush()

    rep = permissions.audit_series(db_session, "20260528_aud_L1")

    assert not rep.missing and not rep.no_paths
    # The clean 0664 file is not flagged; the 0644 one is.
    assert _issue_for(rep, good_file) is None
    bad = _issue_for(rep, bad_file)
    assert bad and "not group-writable" in bad.problems
    dats_issue = _issue_for(rep, dats)
    assert dats_issue and "no setgid" in dats_issue.problems


def test_audit_series_skips_tif_tree(db_session, tmp_path, monkeypatch):
    _ignore_group(monkeypatch)
    root = tmp_path / "series"
    (root / "dats").mkdir(parents=True)
    tif = root / "tif"
    tif.mkdir()
    stray = tif / "t001.tif"
    stray.write_text("i")
    os.chmod(stray, 0o600)  # would be an issue IF tif/ were scanned

    db_session.add(
        Series(series_name="20260528_aud_L2", annot_loc=str(root), image_loc=str(root))
    )
    db_session.flush()

    rep = permissions.audit_series(db_session, "20260528_aud_L2")

    # tif/ is never scanned, so its bad file is invisible to the audit.
    assert all("/tif/" not in i.path for i in rep.issues)
    assert not any(i.path == str(stray) for i in rep.issues)


def test_audit_series_no_paths(db_session, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()  # exists but has no dats/matlab/MLtemp/matlabParams
    db_session.add(
        Series(series_name="20260528_aud_L3", annot_loc=str(root), image_loc=str(root))
    )
    db_session.flush()

    rep = permissions.audit_series(db_session, "20260528_aud_L3")
    assert rep.no_paths
    assert rep.n_issues == 0


def test_audit_series_missing(db_session):
    rep = permissions.audit_series(db_session, "does-not-exist")
    assert rep.missing
    assert rep.n_issues == 0


def test_audit_series_scans_matlabparams_file(db_session, tmp_path, monkeypatch):
    _ignore_group(monkeypatch)
    root = tmp_path / "series"
    root.mkdir()
    params = root / "matlabParams"
    params.write_text("p")
    os.chmod(params, 0o600)  # not group-writable

    db_session.add(
        Series(series_name="20260528_aud_L4", annot_loc=str(root), image_loc=str(root))
    )
    db_session.flush()

    rep = permissions.audit_series(db_session, "20260528_aud_L4")
    issue = _issue_for(rep, params)
    assert issue and not issue.is_dir
    assert "not group-writable" in issue.problems


def test_audit_series_flags_wrong_group(db_session, tmp_path, monkeypatch):
    # Force a target gid the test file cannot possibly carry.
    file_gid = (tmp_path).stat().st_gid
    monkeypatch.setattr(permissions, "_target_gid", lambda *a, **k: file_gid + 12345)
    root = tmp_path / "series"
    dats = root / "dats"
    dats.mkdir(parents=True)
    f = dats / "CDx.csv"
    f.write_text("c")
    os.chmod(f, 0o2775 & 0o777 | 0o664)  # group-writable, clean mode

    db_session.add(
        Series(series_name="20260528_aud_L5", annot_loc=str(root), image_loc=str(root))
    )
    db_session.flush()

    rep = permissions.audit_series(db_session, "20260528_aud_L5")
    issue = _issue_for(rep, f)
    assert issue and any(p.startswith("group=") for p in issue.problems)
