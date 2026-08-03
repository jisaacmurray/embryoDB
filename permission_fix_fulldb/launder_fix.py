#!/usr/bin/env python3
"""Generate per-owner GROUP-LAUNDER scripts for the Cat2 residue.

Cat2 = files whose group is NOT gid 100 (`users`). The NFS `manage-gids`
server blocks a client-side `chgrp` to 100 for these owners, so the ONLY
client-side lever is setgid inheritance: a FRESH inode created inside a
setgid, gid-100 directory is born with gid 100 (no chgrp needed).

Per owner (run AS that owner) each script:
  Phase 1 — for every unique parent dir it can (dir is already gid 100),
            `chmod g+rwxs` so new files there inherit gid 100.
  Phase 2 — launder each file whose parent is gid 100:
              cp (fresh inode → inherits gid 100)  ->  cmp verify content
              ->  confirm tmp is gid 100 + group-writable  ->  atomic `mv`
            The original is only replaced AFTER content + group are verified;
            on any mismatch the temp is removed and the original left intact.

What each script deliberately SKIPS (reported, needs the appliance admin —
a real chgrp on the hosting NFS server, which is unsquashed and not
manage-gids-gated):
  * files whose parent dir is itself the wrong group (a fresh inode there
    would inherit the WRONG group), and
  * directories with the wrong group (can't be relaunder-copied in place;
    setgid'ing a wrong-group dir would poison its children).

Scripts are emitted ONLY for owners reachable via `su` on some box
(uid 1002=azach, 1001=eliciap, 1000=testuser). Orphan uid 1009 has no login
anywhere, so its residue is left for the admin.

DEPLOYMENT-SPECIFIC: gid 100 and the ACCESSIBLE uid->login table below are this
lab's, as recorded in July 2026. Run setgid_dirs.py (PASS 1) first, or most
files here will report SKIP-PARENT-WRONG-GROUP. Read README.md in this
directory before running.
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

# uid -> human label, only for owners that have a real login on some box.
ACCESSIBLE = {1002: "azach", 1001: "eliciap", 1000: "testuser"}

by_uid: dict[int, list[str]] = defaultdict(list)
with PATHS.open() as fh:
    next(fh)
    for line in fh:
        uid_s, _gid, path = line.rstrip("\n").split("\t")
        by_uid[int(uid_s)].append(path)


def emit(uid: int, label: str, paths: list[str]) -> Path:
    # unique parent dirs, stable order
    parents: list[str] = []
    seen = set()
    for p in paths:
        d = os.path.dirname(p)
        if d not in seen:
            seen.add(d)
            parents.append(d)

    sp = OUT / f"launder_fix_uid{uid}.sh"
    L: list[str] = [
        "#!/bin/bash",
        f"# GROUP-LAUNDER for numeric uid {uid} ({label}).",
        f"# {len(paths)} Cat2 entr(ies) whose group is not {TARGET_GID} and that",
        "# the NFS manage-gids server won't let this owner chgrp. We relaunder via",
        "# setgid-dir inheritance: a fresh inode in a gid-100 setgid dir is born",
        "# gid 100. Files in a wrong-group parent dir (and wrong-group dirs) can't",
        "# be fixed here and are reported for the appliance admin.",
        "#",
        f"# Run AS {label} (chmod/cp need ownership; chgrp is NOT used):",
        f"#   su - {label} -c 'bash {sp}'",
        "set -u",
        f"TG={TARGET_GID}",
        "laundered=0; skip_parent=0; skip_dir=0; fail=0",
        "",
        "gwritable() { [ $(( 0$(stat -c %a -- \"$1\") & 020 )) -ne 0 ]; }",
        "",
        "# --- Phase 1: setgid the gid-100 parent dirs we own ---",
    ]
    for d in parents:
        q = shlex.quote(d)
        L.append(
            f'if [ -d {q} ] && [ "$(stat -c %g -- {q})" = "$TG" ]; then '
            f'chmod g+rwxs -- {q} 2>/dev/null || true; fi'
        )
    L += ["", "# --- Phase 2: launder each file ---"]
    tmpl = (
        "if [ -d @Q@ ]; then skip_dir=$((skip_dir+1)); "
        'echo "SKIP-DIR (needs admin): "@Q@ >&2; '
        "elif [ ! -e @Q@ ]; then :; "
        'elif [ "$(stat -c %g -- "$(dirname -- @Q@)")" != "$TG" ]; then '
        "skip_parent=$((skip_parent+1)); "
        'echo "SKIP-PARENT-WRONG-GROUP (needs admin): "@Q@ >&2; '
        "else "
        'tmp="$(dirname -- @Q@)/.launder.$$.$RANDOM"; '
        "if cp --preserve=timestamps -- @Q@ \"$tmp\" 2>/dev/null "
        "&& chmod g+rw -- \"$tmp\" 2>/dev/null "
        "&& cmp -s -- @Q@ \"$tmp\" "
        "&& [ \"$(stat -c %g -- \"$tmp\")\" = \"$TG\" ] "
        "&& gwritable \"$tmp\" "
        "&& mv -f -- \"$tmp\" @Q@; then laundered=$((laundered+1)); "
        'else fail=$((fail+1)); rm -f -- "$tmp" 2>/dev/null; '
        'echo "FAIL-LAUNDER: "@Q@ >&2; fi; '
        "fi"
    )
    for p in paths:
        L.append(tmpl.replace("@Q@", shlex.quote(p)))
    L += [
        "",
        f'echo "{label} (uid {uid}): $laundered laundered, '
        '$skip_parent skipped(parent wrong group), '
        '$skip_dir skipped(dir), $fail failed"',
        "",
    ]
    sp.write_text("\n".join(L))
    sp.chmod(0o755)
    return sp


print("launder scripts:")
for uid in sorted(by_uid):
    if uid in ACCESSIBLE:
        sp = emit(uid, ACCESSIBLE[uid], by_uid[uid])
        print(f"  uid {uid} ({ACCESSIBLE[uid]}): {len(by_uid[uid])} entr(ies) -> regen/{sp.name}")
    else:
        print(f"  uid {uid}: {len(by_uid[uid])} entr(ies) -> NO LOGIN (admin residue, skipped)")
