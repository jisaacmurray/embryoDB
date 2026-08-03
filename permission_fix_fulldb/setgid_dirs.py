#!/usr/bin/env python3
"""PASS 1 of the two-pass Cat2 fix: setgid the `dats` dirs, grouped by DIR OWNER.

Why this is separate from the launder: setgid-inheritance is the only lever we
have to give a fresh file gid 100 (the NFS manage-gids server blocks `chgrp`),
but setting the setgid bit on a directory needs OWNERSHIP of that directory. The
per-series `dats` dirs are owned by whoever first ran the pipeline for that
series (often a DIFFERENT lab member than the file owner), so the file owner
usually can't setgid the dir the file lives in.

So: each dir-owner runs THEIR script here first (`chmod g+rwxs` = setgid +
group-writable, owner-gated), then each file-owner re-runs launder_fix (PASS 2)
— a fresh temp in the now-setgid+group-writable dir inherits gid 100, and the
atomic replace lands. `chmod g+rwxs` also matches the policy dir mode (2775).

Scripts are keyed by NUMERIC dir-owner uid because the same number is a
different human per box. Run each AS that uid on a box where it maps, e.g.:
    dir_owner 1006 -> su - yongjun  (on alcatraz)
    dir_owner 1005 -> su - azach    (penticton)  OR  su - jrumley (alcatraz)
Orphan dir-owners (no login anywhere) can't be su'd to; their dirs need the
appliance admin (or root on the hosting NFS server).

DEPLOYMENT-SPECIFIC: gid 100 and the LOGINS uid->human table below are this
lab's; the same numeric uid is a different person per box. Read README.md in
this directory before running.
"""
from __future__ import annotations
import os
import shlex
import stat
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATHS = HERE / "regen" / "chgrp_admin_paths.tsv"
OUT = HERE / "regen"
TARGET_GID = 100

# numeric uid -> where it has a login (informational, printed in the header)
LOGINS = {
    1000: "penticton:jmurr2 / alcatraz:testuser",
    1001: "penticton:wlkrsn / alcatraz:eliciap",
    1002: "alcatraz:azach",
    1003: "penticton:jmurr / alcatraz:jmurr",
    1004: "alcatraz:jburdick",
    1005: "penticton:azach / alcatraz:jrumley",
    1006: "alcatraz:yongjun",
    1008: "alcatraz:priya",
    1013: "penticton:jrumley",
}

# unique parent dirs (gid 100, not yet setgid) grouped by dir owner uid
by_owner: dict[int, list[str]] = defaultdict(list)
seen: set[str] = set()
with PATHS.open() as fh:
    next(fh)
    for line in fh:
        _fuid, _gid, path = line.rstrip("\n").split("\t")
        d = os.path.dirname(path)
        if d in seen:
            continue
        seen.add(d)
        try:
            st = os.lstat(d)
        except OSError:
            continue
        m = stat.S_IMODE(st.st_mode)
        if st.st_gid == TARGET_GID and not (m & 0o2000):
            by_owner[st.st_uid].append(d)


def emit(uid: int, dirs: list[str]) -> Path:
    where = LOGINS.get(uid, "*** NO LOGIN — appliance admin only ***")
    sp = OUT / f"setgid_dirs_uid{uid}.sh"
    L = [
        "#!/bin/bash",
        f"# PASS 1 — setgid the dats dirs OWNED by numeric uid {uid}.",
        f"# login(s): {where}",
        f"# {len(dirs)} dir(s). chmod needs ownership, so run AS this uid:",
        f"#   su - <that-user> -c 'bash {sp}'",
        "# Adds setgid + group-write (dir -> 2775) so any users-group member's",
        "# fresh file inherits gid 100. Idempotent; errors on non-owned dirs are",
        "# harmless. Follow with PASS 2 (each file-owner re-runs launder_fix).",
        "set -u",
        "ok=0; fail=0",
    ]
    for d in dirs:
        q = shlex.quote(d)
        L.append(
            f"if chmod g+rwxs -- {q} 2>/dev/null; then ok=$((ok+1)); "
            f"else fail=$((fail+1)); echo 'NOT-OWNER (skip): '{q} >&2; fi"
        )
    L += ["", f'echo "uid {uid}: $ok setgid-ed, $fail not-owned"', ""]
    sp.write_text("\n".join(L))
    sp.chmod(0o755)
    return sp


print("PASS-1 setgid scripts (run each AS the dir-owner uid, then re-run launder):")
for uid in sorted(by_owner, key=lambda u: -len(by_owner[u])):
    sp = emit(uid, by_owner[uid])
    where = LOGINS.get(uid, "ORPHAN — admin only")
    print(f"  uid {uid:>5}: {len(by_owner[uid]):>3} dirs -> regen/{sp.name}   [{where}]")
