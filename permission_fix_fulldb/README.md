# Permission repair — DEPLOYMENT-SPECIFIC

**These scripts are written for the Murray lab's NFS deployment and are not
portable.** They are kept in the repo because the *technique* is hard-won and
the situation recurs, not because they can be run as-is anywhere else. Read
this whole file before running any of them.

## The problem they solve

embryoDB's handoff model is that curation passes from lab member to lab member:
a series' `dats/`, `matlab/`, `MLtemp/` and `matlabParams` must be writable by
any member of group `users` (gid 100). Files written by an older tool, or by a
member whose default group was wrong, are owned by one person and unwritable by
everyone else. `embryodb plan-permission-fix` finds them; the additive
`chgrp users && chmod g+rw` it emits fixes most of them.

The residue is the interesting part, and it is where these scripts start.

## What is specific to this deployment

Everything below is baked in and must be re-derived for any other site:

- **`TARGET_GID = 100`** — the numeric gid of group `users` on the NFS server.
  Used numerically on purpose: the *name* `users` resolves to different gids on
  different client boxes, so trusting it silently corrupts the fix.
- **The uid tables** in `setgid_dirs.py` (`LOGINS`) and `launder_fix.py`
  (`ACCESSIBLE`). The same numeric uid is a *different human* on `penticton`
  than on `alcatraz` (e.g. uid 1005 is `azach` on penticton and `jrumley` on
  alcatraz), and some uids — 1009 in the July 2026 run — are orphans with no
  login on any box. Every script is keyed by numeric uid for this reason.
- **Absolute paths** under `/gpfs/fs0/u/<user>/images/...`, inherited from the
  census.
- **Root cannot override permissions.** The store is NFSv3 from
  `192.168.204.10:/data/murrlab3` with root squashed on clients, so every fix
  script must be run *as the owning account* (`su - <user> -c '...'`). Only
  root **on the hosting server** can do a real `chgrp` for an orphan uid.
- **The server runs `manage-gids`**, which computes a client's group membership
  server-side and therefore refuses a client-issued `chgrp` to gid 100 for
  users it does not consider members. This is what makes the residue unfixable
  by the obvious route, and what the laundering trick works around.

## The reusable idea: group laundering

When `chgrp` is refused, the one remaining client-side lever is **setgid
inheritance**: a *fresh* inode created inside a setgid, gid-100 directory is
born with gid 100, no `chgrp` involved. So instead of changing the file's
group, you create a new file that already has the right group and atomically
swap it in.

That is a two-pass dance, because the two `chmod`s need two different owners:

1. **PASS 1 — `setgid_dirs.py`.** Setting the setgid bit on a directory
   requires owning *the directory*. The per-series `dats` dir is owned by
   whoever first ran the pipeline for that series, usually **not** the person
   who owns the stuck file. So the dir-owners run this pass first
   (`chmod g+rwxs`, i.e. mode 2775, which is also the policy dir mode).
2. **PASS 2 — `launder_fix.py`.** Now each *file* owner re-runs their launder
   script: `cp` into the now-setgid dir (fresh inode → gid 100), `cmp` to
   verify the content, confirm the temp really is gid 100 and group-writable,
   and only then `mv -f` over the original. On any mismatch the temp is removed
   and the original left untouched.

Two cases are deliberately **not** laundered and are reported for the
appliance admin instead:

- files whose *parent dir* is itself the wrong group — a fresh inode there
  would inherit the wrong group, silently making things worse;
- directories with the wrong group — they cannot be copy-replaced in place, and
  setgid'ing a wrong-group dir would poison every child created afterward.

## Files

Tracked (the generators — hand-written, reusable logic):

| file | role |
| --- | --- |
| `regen_fix.py` | Re-stats the census for current truth and splits it: **Cat1** (already gid 100, just not group-writable → owner-fixable `chmod` now) vs **Cat2** (wrong gid → needs the laundering dance or the admin). Emits `regen/chmod_fix_uid<UID>.sh` and the admin work-order TSVs. |
| `setgid_dirs.py` | PASS 1 above. Emits `regen/setgid_dirs_uid<UID>.sh`, keyed by **dir** owner. |
| `launder_fix.py` | PASS 2 above. Emits `regen/launder_fix_uid<UID>.sh`, keyed by **file** owner. |
| `categorize.py` | Reporting only. Buckets the still-locked residue into regenerable-by-pipeline vs irreplaceable curation source (`-edit.zip`, AceTree XML) vs editor autosave junk, so effort goes where re-running the pipeline can't help. |
| `test_alcatraz_chgrp.sh` | Single-file probe. Run as a target owner on a given box to find out whether the server will accept a `chgrp` for that uid at all. **Run this first** — it is what tells you whether you need the laundering dance or not. |

Untracked (generated output — see `.gitignore`): `census.tsv`,
`fix_permissions_*.sh`, `regen/`.

## Order of operations

```
embryodb plan-permission-fix --all --out permission_fix_fulldb   # census + naive fix scripts
bash test_alcatraz_chgrp.sh                                      # as one target owner: can we chgrp at all?
# each owner runs their fix_permissions_<owner>.sh
python regen_fix.py                                              # re-stat; what's left, and in which category
# each Cat1 owner runs regen/chmod_fix_uid<UID>.sh
python setgid_dirs.py                                            # PASS 1 scripts
# each DIR owner runs regen/setgid_dirs_uid<UID>.sh
python launder_fix.py                                            # PASS 2 scripts
# each FILE owner runs regen/launder_fix_uid<UID>.sh
python categorize.py                                             # what's still stuck, and does it matter
```

Every generator re-stats the paths, so re-running after a partial fix simply
narrows the work. Nothing here is destructive: the fixes are additive (group
`users` + group-write, setgid on dirs; world bits are never touched) and the
launder verifies content before replacing anything.

## Caveats

- **Never** turn the census into a filesystem walk of a `/murrlab*` mount root.
  `plan-permission-fix` discovers paths through the database and stats only
  `dats/`, `matlab/`, `MLtemp/` and `matlabParams` — never the `tif/` image
  tree. A recursive walk of the image tree OOM-killed `penticton` in June 2026.
- The generated scripts embed a literal list of paths and go stale as soon as
  anyone re-saves a file. Regenerate rather than re-running an old one.
- `su - <user>` needs sudo, and the account must exist *on the box you are on*.
  Check `id` and `getent group 100` on each box before assuming.
