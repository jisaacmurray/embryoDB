"""Tests for the GUI-free identity helpers (user + person guards)."""

from __future__ import annotations

from embryodb import identity
from embryodb.models import Acquisition, Series


# --- system users / image-dir owners --------------------------------------


def test_image_dir_owners_requires_images_subdir(tmp_path):
    """Only top-level dirs that contain an ``images/`` subdir count as users.

    The staged layout is ``<root>/<user>/images/<series>/`` so dotfiles and
    legacy series dirs that also live directly under the root must be ignored.
    """
    (tmp_path / "alice" / "images").mkdir(parents=True)
    (tmp_path / "bob" / "images").mkdir(parents=True)
    (tmp_path / ".AppleDB").mkdir()  # hidden — skip
    (tmp_path / "20210317_legacy_series").mkdir()  # no images/ — skip

    owners = identity._image_dir_owners(tmp_path)
    assert owners == {"alice", "bob"}


def test_system_users_unions_owners_and_current_user(tmp_path):
    (tmp_path / "fileserver_only" / "images").mkdir(parents=True)
    users = identity.system_users(tmp_path)
    assert "fileserver_only" in users  # owner with no passwd entry still listed
    assert identity.current_user() in users  # default always selectable
    assert users == sorted(users)


def test_is_system_user(tmp_path):
    (tmp_path / "carol" / "images").mkdir(parents=True)
    assert identity.is_system_user("carol", tmp_path)
    assert not identity.is_system_user("nope_not_a_user", tmp_path)


def test_user_has_image_dir(tmp_path):
    (tmp_path / "dave").mkdir()
    assert identity.user_has_image_dir("dave", tmp_path)
    assert not identity.user_has_image_dir("erin", tmp_path)


# --- known persons --------------------------------------------------------


def test_known_persons_unions_acquisition_and_series(db_session):
    db_session.add(Acquisition(name="acq1", source_dir="/tmp/acq1", person="eli"))
    db_session.add(Series(series_name="s1", person="azach"))
    db_session.add(Series(series_name="s2", person="azach"))  # dup → collapses
    db_session.add(Series(series_name="s3", person="  "))  # blank → ignored
    db_session.add(Series(series_name="s4", person=None))  # null → ignored
    db_session.flush()

    persons = identity.known_persons(db_session)
    assert persons == ["azach", "eli"]


def test_is_known_person(db_session):
    db_session.add(Series(series_name="s1", person="jmurr"))
    db_session.flush()
    assert identity.is_known_person(db_session, "jmurr")
    assert identity.is_known_person(db_session, "  jmurr  ")  # trimmed
    assert not identity.is_known_person(db_session, "typo")
    assert not identity.is_known_person(db_session, "")
