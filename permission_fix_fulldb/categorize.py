#!/usr/bin/env python3
"""Categorize the CURRENTLY-LOCKED census files by type + owner, and classify each
as regenerable-by-extract vs irreplaceable curation source. Re-stats each path so
already-fixed entries drop out and owner is the true NUMERIC uid.

Reporting only -- changes nothing. The point is to tell a stuck file that a
pipeline re-run would recreate from one that is irreplaceable curation
(`-edit.zip`, AceTree XML), so effort goes where it has to.

DEPLOYMENT-SPECIFIC (gid 100, this lab's dats/matlab/MLtemp layout) -- read
README.md in this directory before running.
"""
from __future__ import annotations
import os, stat
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CENSUS = HERE / "census.tsv"
TARGET_GID = 100

# category -> regeneration class
REGEN = "REGENERABLE (extract/StarryNite output)"
SOURCE = "SOURCE (curation — must perm-fix, cannot regen)"
JUNK = "JUNK (editor autosave — could delete)"
CONFIG = "CONFIG (SN params — regen only if re-run from scratch)"

def classify(path: str):
    bn = path.rsplit("/", 1)[-1]
    low = bn.lower()
    in_dats = "/dats/" in path
    in_matlab = "/matlab/" in path
    in_mltemp = "/MLtemp/" in path
    # junk
    if bn.startswith("#") and bn.endswith("#"):
        return "autosave", JUNK
    # curation source
    if low.endswith("-edit.zip"):
        return "dats/edit.zip", SOURCE
    if low.endswith(".xml") or low.endswith(".xml.bak"):
        return "dats/AceTree.xml", SOURCE
    if low.endswith(".zip"):  # non-edit zip (raw AceTree save)
        return "dats/other.zip", SOURCE
    # extract-derived dats by prefix
    if in_dats:
        for pre in ("SCD", "SCA", "CD", "CA"):
            if bn.startswith(pre):
                return f"dats/{pre}", REGEN
        if low.endswith("auxinfo.csv"):
            return "dats/AuxInfo.csv", REGEN
        if low.endswith(".log"):
            return "dats/log", REGEN
        return "dats/other", REGEN
    if in_matlab:
        if low.endswith("_matlabnuclei"):
            return "matlab/nuclei", REGEN
        if low.endswith("fullmatlabresult.mat"):
            return "matlab/result.mat", REGEN
        if low.endswith("_sn_parameterfile.txt"):
            return "matlab/sn_paramfile", REGEN
        return "matlab/other", REGEN
    if in_mltemp:
        return "MLtemp/*", REGEN
    if bn == "matlabParams":
        return "matlabParams", CONFIG
    return "other", SOURCE

by_cat = defaultdict(int)
by_cat_class = defaultdict(int)
by_owner_class = defaultdict(lambda: defaultdict(int))
by_owner_cat = defaultdict(lambda: defaultdict(int))
locked = skipped = 0

with CENSUS.open() as fh:
    next(fh)
    for line in fh:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 6:
            continue
        path = parts[1]
        try:
            st = os.lstat(path)
        except OSError:
            skipped += 1
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        mode = stat.S_IMODE(st.st_mode)
        # still locked? = NOT (gid==100 AND group-writable)
        if st.st_gid == TARGET_GID and (mode & 0o020):
            continue
        locked += 1
        uid = st.st_uid
        cat, cls = classify(path)
        by_cat[cat] += 1
        by_cat_class[(cls, cat)] += 1
        by_owner_class[uid][cls] += 1
        by_owner_cat[uid][cat] += 1

classes = [REGEN, CONFIG, SOURCE, JUNK]

print(f"currently-locked entries categorized: {locked}   (skipped/unreachable: {skipped})\n")

print("=== by regeneration class ===")
tot = defaultdict(int)
for (cls, cat), n in by_cat_class.items():
    tot[cls] += n
for cls in classes:
    print(f"  {tot[cls]:>5}  {cls}")

print("\n=== by category (with class) ===")
CLS_OF = {}
for (cls, cat), n in by_cat_class.items():
    CLS_OF[cat] = cls
for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
    print(f"  {n:>5}  {cat:<20} [{CLS_OF[cat].split(' ')[0]}]")

print("\n=== by owner uid x class ===")
hdr = "  uid     " + "".join(f"{c.split(' ')[0]:>13}" for c in classes) + "     total"
print(hdr)
for uid in sorted(by_owner_class, key=lambda u: -sum(by_owner_class[u].values())):
    row = by_owner_class[uid]
    cells = "".join(f"{row.get(c,0):>13}" for c in classes)
    print(f"  {uid:<8}{cells}   {sum(row.values()):>7}")

print("\n=== by owner uid x category ===")
allcats = sorted(by_cat)
for uid in sorted(by_owner_cat, key=lambda u: -sum(by_owner_cat[u].values())):
    row = by_owner_cat[uid]
    detail = ", ".join(f"{c}={row[c]}" for c in allcats if row.get(c))
    print(f"  uid {uid}: {detail}")
