#!/usr/bin/env python3
"""Re-stat the census paths for CURRENT on-disk truth and regenerate fix scripts.

Splits into:
  Cat1 (gid==100 but not group-writable)  -> chmod-only, owner-fixable NOW from any
        client. Emitted as chmod_fix_uid<UID>.sh, keyed by NUMERIC uid, chmod runs
        unconditionally (no chgrp && chmod trap).
  Cat2 (gid!=100)                          -> needs a real chgrp to gid 100, blocked
        on every client by NFS manage-gids. Emitted as chgrp_admin.tsv for the
        appliance admin (grouped uid x current-gid).
Already-fine entries (gid==100 AND group-writable) and paths not visible from this
host are counted and skipped. Numeric gid 100 is used everywhere (never the name).

DEPLOYMENT-SPECIFIC (gid 100, NFS manage-gids, root squashed on clients) --
read README.md in this directory before running.
"""
from __future__ import annotations
import os, shlex, stat
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CENSUS = HERE / "census.tsv"
OUT = HERE / "regen"
OUT.mkdir(exist_ok=True)
TARGET_GID = 100

cat1: dict[int, list[tuple[str, bool]]] = defaultdict(list)   # uid -> [(path, is_dir)]
cat2: dict[tuple[int, int], list[str]] = defaultdict(list)    # (uid,gid) -> [paths]
already = unreachable = 0

with CENSUS.open() as fh:
    next(fh)  # header
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 6:
            continue
        path = parts[1]
        try:
            st = os.lstat(path)
        except OSError:
            unreachable += 1
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        mode = stat.S_IMODE(st.st_mode)
        is_dir = stat.S_ISDIR(st.st_mode)
        gid = st.st_gid
        gwrite = bool(mode & 0o020)
        if gid == TARGET_GID and gwrite:
            already += 1
        elif gid == TARGET_GID and not gwrite:
            cat1[st.st_uid].append((path, is_dir))
        else:
            cat2[(st.st_uid, gid)].append(path)

# --- Cat1: chmod-only scripts, one per numeric uid ---
for uid, entries in sorted(cat1.items()):
    sp = OUT / f"chmod_fix_uid{uid}.sh"
    L = [
        "#!/bin/bash",
        f"# Cat1 chmod-only fix for files owned by numeric uid {uid}.",
        f"# {len(entries)} entr(ies), already gid {TARGET_GID}, just not group-writable.",
        f"# chmod needs ownership only (works over NFS) — run AS uid {uid}:",
        f"#   su - <that-user> -c 'bash {sp}'",
        "ok=0; fail=0",
    ]
    for path, is_dir in entries:
        q = shlex.quote(path)
        mod = "g+rwxs" if is_dir else "g+rw"
        L.append(
            f"if chmod {mod} -- {q}; then ok=$((ok+1)); "
            f"else fail=$((fail+1)); echo FAILED: {q} >&2; fi"
        )
    L += ["", f'echo "uid {uid}: $ok fixed, $fail failed"', ""]
    sp.write_text("\n".join(L))
    sp.chmod(0o755)

# --- Cat2: admin work-order (can't be fixed client-side) ---
awo = OUT / "chgrp_admin.tsv"
with awo.open("w") as fh:
    fh.write("owner_uid\tcurrent_gid\tn_entries\tsample_path\n")
    for (uid, gid), paths in sorted(cat2.items(), key=lambda kv: -len(kv[1])):
        fh.write(f"{uid}\t{gid}\t{len(paths)}\t{paths[0]}\n")
# full path list for the admin, grouped
awp = OUT / "chgrp_admin_paths.tsv"
with awp.open("w") as fh:
    fh.write("owner_uid\tcurrent_gid\tpath\n")
    for (uid, gid), paths in sorted(cat2.items()):
        for p in paths:
            fh.write(f"{uid}\t{gid}\t{p}\n")

print(f"already fine (gid {TARGET_GID} + group-writable): {already}")
print(f"unreachable from this host (skipped):            {unreachable}")
print(f"Cat1 chmod-only entries:  {sum(len(v) for v in cat1.values())} across {len(cat1)} uid(s)")
for uid, e in sorted(cat1.items(), key=lambda kv: -len(kv[1])):
    print(f"   uid {uid:>6}: {len(e)}  -> regen/chmod_fix_uid{uid}.sh")
print(f"Cat2 chgrp-needed (admin): {sum(len(v) for v in cat2.values())} across {len(cat2)} (uid,gid) group(s)")
for (uid, gid), paths in sorted(cat2.items(), key=lambda kv: -len(kv[1])):
    print(f"   uid {uid:>6} gid {gid:>6}: {len(paths)}")
