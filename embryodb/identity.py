"""User + person identity helpers (GUI-free core).

Two distinct identity fields flow through the pipeline, and they are NOT
interchangeable:

- **user** — the OS/login account that OWNS the staged files. It determines
  the image directory (``/murrlab3/<user>/images/<series>/``), so it must be a
  real account on this host: writing into another user's tree leaves files
  owned by the wrong uid and causes permission grief. Defaults to the account
  running embryoDB.
- **person** — free-form attribution for WHO the data belongs to scientifically
  (e.g. ``jmurr`` pipelining a movie ``eli`` collected records ``person="eli"``).
  Not tied to the filesystem; guarded only against typos via a known-person
  list so a fat-fingered new name is visibly new.

These helpers back both the CLI guards and the GUI dropdowns so the two share
one source of truth (the CLI<->GUI parity rule).
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Acquisition, Series
from .pipeline.orchestrate import DEFAULT_IMAGE_LOC_ROOT

# Accounts below this uid are daemons/services, not lab members. 65534 is the
# conventional `nobody` sentinel and is excluded even though it clears the bar.
MIN_LOGIN_UID = 1000
_NOBODY_UID = 65534


def current_user() -> str:
    """The account embryoDB is running as (settings.user → $USER/$LOGNAME)."""
    return settings.user or os.environ.get("USER") or os.environ.get("LOGNAME") or "anonymous"


def _login_accounts() -> set[str]:
    """Real login accounts on this host (uid >= MIN_LOGIN_UID, excl. nobody)."""
    out: set[str] = set()
    try:
        for pw in pwd.getpwall():
            if pw.pw_uid >= MIN_LOGIN_UID and pw.pw_uid != _NOBODY_UID:
                out.add(pw.pw_name)
    except Exception:  # noqa: BLE001 — pwd unavailable on some platforms
        pass
    return out


def _image_dir_owners(image_loc_root: Path | None = None) -> set[str]:
    """Usernames that already own an image tree under the root.

    The staged layout is ``<root>/<user>/images/<series>/``, so we count a
    top-level dir as a user only when it has an ``images/`` subdir. That filters
    out the dotfiles and legacy series dirs that also live directly under
    ``/murrlab3``.
    """
    root = Path(image_loc_root or DEFAULT_IMAGE_LOC_ROOT)
    out: set[str] = set()
    try:
        for child in root.iterdir():
            if (
                child.is_dir()
                and not child.name.startswith(".")
                and (child / "images").is_dir()
            ):
                out.add(child.name)
    except OSError:
        pass
    return out


def system_users(image_loc_root: Path | None = None) -> list[str]:
    """Selectable pipeline users, sorted.

    Union of real login accounts (``uid >= MIN_LOGIN_UID``) and usernames that
    already own an image tree under ``image_loc_root`` — the latter covers
    accounts that exist on a fileserver but not in this host's passwd db. The
    current user is always present so the default is always selectable.
    """
    users = _login_accounts() | _image_dir_owners(image_loc_root)
    users.add(current_user())
    return sorted(users)


def is_system_user(name: str, image_loc_root: Path | None = None) -> bool:
    return name.strip() in set(system_users(image_loc_root))


def user_has_image_dir(name: str, image_loc_root: Path | None = None) -> bool:
    """Whether ``<root>/<name>`` already exists.

    A missing dir for a chosen user is worth a confirm: it'll be created and
    owned by whoever runs the import, so picking a user who isn't set up yet is
    a common sign of a typo or a cross-user mistake.
    """
    root = Path(image_loc_root or DEFAULT_IMAGE_LOC_ROOT)
    return (root / name).is_dir()


def known_persons(session: Session) -> list[str]:
    """Distinct non-empty person attributions already recorded, sorted.

    Backs the person dropdown so a brand-new (possibly mistyped) name is
    visibly absent from the list and can be confirmed before it's created.
    """
    persons: set[str] = set()
    for model in (Acquisition, Series):
        for (val,) in session.execute(select(model.person).distinct()):
            if val and val.strip():
                persons.add(val.strip())
    return sorted(persons)


def is_known_person(session: Session, name: str) -> bool:
    return bool(name.strip()) and name.strip() in set(known_persons(session))


__all__ = [
    "MIN_LOGIN_UID",
    "current_user",
    "is_known_person",
    "is_system_user",
    "known_persons",
    "system_users",
    "user_has_image_dir",
]
