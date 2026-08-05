# Brief: orientation-agnostic daughter naming in AceTree

Instructions for an agent picking this up cold. Read the whole thing before
touching anything — the safety section is not optional.

## The biology, stated plainly

A *C. elegans* embryo mounted for imaging normally sits in a stereotyped pose:
at the 4-cell stage the four nuclei lie in a rough diamond **in a single XY
plane**, i.e. the left-right axis of the embryo is aligned with the image Z
axis. Around gastrulation (~50-cell, over a span of timepoints, not
instantaneously) the embryo **rotates** to an orientation where the dorsoventral
axis is aligned with Z.

AceTree's daughter-naming rules assume that pose. Some embryos are mounted
**skewed** — the 4-cell diamond is tilted out of the canonical arrangement. In
those, the 4 cells themselves still get named correctly, but the next round of
divisions (`ABal`/`ABar`, `ABpl`/`ABpr`) is misnamed, and every descendant
inherits the error.

**The specific outlier to study: `20260603_ceh-27_JIM593_L2`.** This is a real,
reproducible case. It is believed misnamed from the ABa/ABp daughters down.

## What has already been established — do not re-derive this

Verified by reading the source at `/gpfs/fs0/l/murr/new_tools/acetree` and the
outlier's own config files.

**Naming is the sign of a single dot product.**
`DivisionCaller.assignNames()` (`src/org/rhwlab/snight/DivisionCaller.java:347`):

```java
double dot = getDotProduct(parent, dau1, dau2, r);
if (dot > 0) { newd1 = r.iDau1; newd2 = r.iDau2; }
else         { newd1 = r.iDau2; newd2 = r.iDau1; }
```

`r` is a per-parent rule from `src/org/rhwlab/snight/NewRules.txt` (621 lines,
columns `Parent, Rule, D1, D2, revisedXrule, revisedYrule, revisedZrule`) — a
canonical division-axis unit vector **expressed in the image frame**, derived
from the WT set. `getDotProduct` (`:306`) projects the daughter-to-daughter
displacement onto it. There is no confidence, no margin, no abstention: an
arbitrarily small dot still yields a definite answer.

**Two orientation-correction schemes exist, selected by which AuxInfo file is
present** (`src/org/rhwlab/snight/MeasureCSV.java:457-458`):

| | file | correction available |
|---|---|---|
| **V1** | `<series>AuxInfo.csv` | `handleRotation_V1` (`DivisionCaller.java:452`) rotates **X and Y only**, by a single angle `ang`; then an 8-way discrete octant sign-flip keyed on an `axis` string (`AVR`/`PVL`/`PDR`/`ARD`/`ALV`/`PLD`/`PRV`) at `diffsCorrected` (`:379`). **Z is never rotated, only scaled.** |
| **V2** | `<series>AuxInfo_v2.csv` | `CanonicalTransform` — a true 3D rotation built from AP and LR orientation vectors supplied as extra CSV columns (`AP_ORIENTATION`, `LR_ORIENTATION`), applied at `applyProductTransform` (`CanonicalTransform.java:293`). |

**The outlier is on the V1 path.** Its `dats/` holds only
`20260603_ceh-27_JIM593_L2AuxInfo.csv` — no `_v2` file — with
`ang=11.3329, axis=AVR`. Its `.xml` has `<axis axis=""/>`, so the axis comes
from AuxInfo.

**Therefore the skew is not merely mis-measured, it is unrepresentable.**
Correcting an arbitrary 3D mounting pose needs three rotational degrees of
freedom. V1 offers one continuous (an XY angle) plus a choice among eight
discrete octants. A tilt that lifts the LR axis out of Z cannot be expressed,
whatever numbers you put in the file.

**Why the damage lands exactly where the user says.** `ABal`/`ABar` and
`ABpl`/`ABpr` are **left-right** divisions, so their rule vectors point along
the very axis that is skewed. The projection of a mis-rotated LR displacement
onto the canonical LR template is near zero, and the sign of a near-zero number
is noise. Earlier (AP) divisions have large margins and survive; that is why the
4-cell stage names correctly and the failure begins one round later. The error
then propagates: once `ABal`/`ABar` are swapped, every descendant is named in a
mirrored frame.

**Also relevant:** `CanonicalTransform` is built **once**, before 4-cell
identity assignment, and never updated. Even the V2 path cannot follow the
gastrulation rotation.

## The proposal to evaluate

A naming scheme that is orientation-agnostic:

1. **Bootstrap the frame from the data, not from a config file.** Recognise the
   4-cell diamond in 3D and derive the AP / DV / LR axes from it directly,
   replacing the hand-entered `ang`/`axis`/AP-LR-vector inputs.
2. **Let the frame follow the embryo.** Update the axes over time from progeny
   positions, so the gastrulation rotation is tracked rather than assumed away.

Both are hypotheses. Your job is to test whether they hold up on real data
before anyone writes production code.

## Phase 1 — characterise, change nothing

Goal: quantify how common this is and confirm the diagnosis, using data only.
No AceTree, no Java, no code changes.

**Data sources, in preference order:**

- **The embryoDB corpus.** The curated lineages live in Postgres +
  per-series `dats/`. Query the DB for the series list — see
  `docs/data_access.md`. **Never** discover files by walking the filesystem
  (see Safety).
- **`/gpfs/fs0/l/murr/new_tools/LineagePhenotyping`** — the WT reference set the
  naming rules descend from. `Richard_et_al_plus_comma_WT/` is the WT reference
  directory (64 files; the basename is the file-name prefix used by the R code).
  `CDs` (a single CSV, ~2.3 MB) and `data/CA*.csv` hold per-cell, per-timepoint
  records with columns `cellTime, cell, time, none, global, local, blot, cross,
  z, x, y, size, gweight` — i.e. **named nuclei with 3D coordinates over time**,
  which is exactly what you need.
- `AnalyzeRotation.R` and `GetAngles_revRotate.pl` in that repo already compare
  division orientations against WT. Read them before writing anything new;
  prefer extending them to reinventing them.

**Questions to answer:**

1. **Is the 4-cell diamond really coplanar-in-XY across the WT set?** Fit a
   plane to the four nuclei per embryo, measure its tilt relative to the image
   axes, and get the distribution. This establishes what "canonical" empirically
   means and how much natural variation exists.
2. **Where does `20260603_ceh-27_JIM593_L2` fall in that distribution?** Confirm
   it is an outlier and by how much. If it is *not* an outlier on this metric,
   the diagnosis above is wrong — say so loudly and stop.
3. **Compute the dot-product margin for every division in every embryo.**
   Reimplement `getDotProduct` in your analysis language against `NewRules.txt`.
   The prediction is a bimodal picture: AP divisions with large `|dot|`, LR
   divisions with `|dot|` near zero in skewed embryos. **A margin near zero is
   the signal that a name is untrustworthy** — this may be the single most
   useful artifact of the whole exercise, independent of any renaming work.
4. **How many embryos in the corpus are at risk?** Rank by minimum margin at the
   `ABal`/`ABar` and `ABpl`/`ABpr` divisions. The user believes this recurs
   "periodically" — put a number on it.

Deliverable: a short written characterisation with plots. **Do not proceed to
Phase 2 until Phase 1 confirms the mechanism.**

## Phase 2 — prototype offline, still no AceTree

Build the orientation-agnostic namer as a standalone script operating on CD/CA
tables. This is pure geometry on coordinates you already have; dragging Java
into it this early will cost you days.

- Derive the frame from the 4-cell configuration. Note that the diamond is
  nearly but not exactly symmetric — establish which features break the
  ambiguity robustly (P1/AB size asymmetry and division timing are candidates;
  the WT set will tell you).
- **Handle the mirror ambiguity explicitly.** A rotation frame derived from
  points has a handedness; get it wrong and you produce a perfectly
  self-consistent, entirely mirrored embryo. This is the single most likely way
  to ship a subtly wrong result.
- Re-derive the frame over time and check it tracks the gastrulation rotation
  smoothly rather than jumping.
- **Evaluate against curated ground truth.** The corpus has hand-curated
  lineages; embryoDB records how deep each is trustworthy
  (`edited_timepts`, plus per-branch `partial_editing_code` — see
  `docs/editing_codes.md`). **Scope every comparison to the curated extent**;
  past that depth you are comparing against raw StarryNite output, not truth.
- The success bar is twofold: it must **fix** `20260603_ceh-27_JIM593_L2`, and
  it must **not regress** the WT embryos that the current code already gets
  right. The second is the harder one.

## Phase 3 — testing on a safe copy of AceTree

Only once Phase 2 works on tables.

**Build a private sandbox. Do not touch anything shared.**

```bash
mkdir -p ~/acetree_sandbox && cd ~/acetree_sandbox
# your own jar — never modify /gpfs/fs0/l/murr/tools3/AceTree_Santella.jar
cp /gpfs/fs0/l/murr/tools3/AceTree_Santella.jar ./AceTree_test.jar
# your own copy of the series' dats
cp -r /murrlab3/jmurr/images/20260603_ceh-27_JIM593_L2/dats ./dats
```

Then edit `./dats/20260603_ceh-27_JIM593_L2.xml` so `<nuclei file=...>` points
into your sandbox copy. Leave `<image file=...>` pointing at the real `tif/`
tree — AceTree only reads it. Launch with:

```bash
java -mx500m -jar ./AceTree_test.jar ./dats/20260603_ceh-27_JIM593_L2.xml
```

**Rebuilding patched classes into a jar.** The source clone at
`/gpfs/fs0/l/murr/new_tools/acetree` reproduces the deployed jar's bytecode
exactly, so a targeted class swap is safe and is the established pattern here.
The full source tree does **not** compile (unrelated files reference a removed
`com.sun...bcel` class and a stale `LineageData` interface), so compile only the
files you changed and resolve everything else from the jar:

```bash
mkdir -p /tmp/emptysrc /tmp/out
javac --release 8 -nowarn -implicit:none -sourcepath /tmp/emptysrc \
  -cp ~/acetree_sandbox/AceTree_test.jar -d /tmp/out \
  src/org/rhwlab/snight/DivisionCaller.java
jar uf ~/acetree_sandbox/AceTree_test.jar -C /tmp/out org
```

Always verify first that recompiling the **pristine** file reproduces the jar's
existing bytecode (`javap -p -c`, ignoring the `Classfile`/`Last modified`/
checksum header lines). If it does, any later diff is provably yours alone.

**A note on the V1/V2 fork.** Adding an `AuxInfo_v2.csv` (with AP and LR vectors
you derive automatically in Phase 2) switches the series onto the existing 3D
`CanonicalTransform` path without any Java change at all. **Try this first** —
it may resolve the outlier on its own and it cleanly separates "the geometry was
unrepresentable" from "the code is wrong." Only if that fails do you need to
touch `DivisionCaller`. Note that V2 still cannot follow the gastrulation
rotation, so it addresses proposal (1) but not (2).

## Safety — read before running anything

**Never recursively walk `/murrlab`, `/murrlab2`, `/murrlab3`.** Every embryo
directory holds tens of thousands of TIFFs and there are thousands of embryos. A
`find`, `ls -R`, `du`, `os.walk`, `rglob`, or `glob('**')` over a mount root
caches one NFS inode + dentry per entry; this OOM-killed the `penticton` host on
2026-06-26 and required a reboot. Discover files **through the embryoDB
Postgres DB**. If you must walk, root it at one series' subdirectory and process
in bounded batches. Launch tools from a small local directory and pass NFS paths
as arguments — never `cd` into a million-file tree.

**`-edit.zip` is human curation and is irreplaceable.** Never overwrite,
regenerate, or "refresh" an existing one. Work on copies in your sandbox. Note
the outlier already has both `-edit.zip` and `-edit_orig.zip`; leave both alone.

**Never write into a series' `image_loc` or `annot_loc`.** Outputs go to your
own scratch directory.

**Do not modify `/gpfs/fs0/l/murr/tools3/AceTree_Santella.jar`.** It is the
lab-wide production jar and long-running curation sessions hold it open —
sessions lasting *weeks* have been observed. If a production change is ever
warranted, it is the user's call and the swap must be an atomic rename (never an
in-place overwrite), with a `.bak-<reason>` copy kept.

**Scope your actions to exactly the series you were asked about.** Do not
batch-process the corpus without explicit say-so.

## Reporting

Report to the user, not into a pile of files. Do not create planning or
analysis documents unless asked. What is wanted:

- Phase 1: does the data confirm the mechanism, and how many embryos are
  affected?
- The **margin metric** from Phase 1 question 3, which is valuable on its own as
  a QC flag — it would let AceTree mark a name as low-confidence rather than
  silently guessing.
- A clear statement of whether the V2/`AuxInfo_v2.csv` route alone fixes the
  outlier, before proposing any Java change.

## Open questions worth holding in mind

- Is `NewRules.txt` itself orientation-contaminated? It was derived from WT
  embryos in the canonical pose, so its vectors may encode mounting convention
  as well as biology. An orientation-agnostic namer might need re-derived rules,
  expressed in an embryo-intrinsic frame.
- `getRule()` (`DivisionCaller.java:207`) *synthesises* a rule when a parent is
  absent from the table. Understand that path before trusting results on deep
  lineages.
- Does StarryNite's own naming share this assumption, or is it AceTree-only?
  There is active MATLAB StarryNite work in the lab (`../StarryNite`,
  `../StarryNite_release_v1`) under a separate agent — coordinate rather than
  duplicate.
- `namesHash.txt` (61 lines) is a second, smaller table used alongside
  `NewRules.txt`. Establish its role.
