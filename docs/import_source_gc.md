# Reclaiming import sources (`gc-import-sources`)

Once a movie is staged, the original in `/murrlab3/Images` — a per-acquisition
directory of raw TIFs, or a `.lif` — is redundant. It is also **the only copy**,
so this command answers "did the staging actually capture everything?" rather
than "is there a DB row?".

Dry run by default. Nothing is deleted without `--apply`.

## The sequence to run

```bash
# 1. Full picture, including why the blocked ones are blocked
embryodb gc-import-sources --show-blocked

# 2. Scope to one file and re-confirm it
embryodb gc-import-sources --root /murrlab3/Images/20260625_RW10196_hnd-1.lif

# 3. Delete that one
embryodb gc-import-sources --root /murrlab3/Images/20260625_RW10196_hnd-1.lif --apply

# 4. Once confident, the whole batch
embryodb gc-import-sources --apply
```

`--root` accepts a **single `.lif` path**, not just a directory, which is what
makes step 2/3 a safe one-at-a-time path.

Step 1 is the one that matters. `--apply` acts on **every** verdict that passes
in that run — there is no per-source prompt and no interactive confirm — so
whatever step 1 lists as `would delete` is exactly what step 4 removes.

Two things about timing:

- **The eligible set grows between runs.** The grace period is computed at run
  time from `Acquisition.staged_at`, so sources blocked only by the 30-day
  window age in on their own. Re-run step 1 the same day you apply.
- **`--apply` is not acting on a stale plan.** It deletes using the verdicts
  computed in that same invocation, seconds after verification.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--older-than N` | `30` | Days since `staged_at` before a source is eligible. Raise it to be more conservative. |
| `--root DIR-or-LIF` | `/murrlab3/Images` | Restrict to sources under this path. Repeatable. A source outside every root is never proposed, whatever its DB state. |
| `--dirs-only` | off | Consider directory imports only, never `.lif` files. |
| `--show-blocked` | off | Also list the sources that failed a check, and why. |
| `--apply` | off | Actually delete. Without it this is a dry run. |

## What must hold before a source is proposed

Verification is **per series**, and an acquisition is deletable only when every
one of its series passes. Each series needs:

- `image_loc` set, and the directory present on disk.
- The staged tree **not inside the source** — legacy series backfilled in place
  look exactly like a staged import, and deleting the source would delete the
  only copy.
- Complete microscopy metadata (`n_timepoints`, `planes_per_volume`,
  `voxel_xy_um`, `voxel_z_um`).
- A **`COMPLETE`** `stage_images` run. `SKIPPED` is what `pipeline mark-legacy`
  writes for series predating the pipeline — it means staging never ran, not
  that it succeeded.
- A recorded plane count that is a whole multiple of `n_timepoints ×
  planes_per_volume`, with the derived channel count not exceeding
  `channels_per_plane`.
- The first and last plane still present and non-empty on disk.

A `.lif` additionally requires that **every TileScan holding a movie was
imported**. A position that was never imported leaves no DB row to miss, so the
container itself is read and its TileScan list compared against the `::` suffix
of `Acquisition.source_dir`. This is the check that stops a LIF holding six
TileScans from being deleted because one of them staged.

One `.lif` can back several acquisitions (LIF imports record
`<lif>::<series>` in `source_dir`), so deletion is decided per file, not per
acquisition.

## Why completeness comes from the DB, not from counting files

The plane count comes from the `stage_images` run's `output_summary`, never from
listing `tif/`. Two reasons, and the second is the surprising one.

**Cost.** Counting `tif/` caches one NFS dentry per plane; across thousands of
series that is the traversal pattern that OOM-killed penticton on 2026-06-26.
For the same reason a candidate's size is only measured **after** it has passed
every check — sizing every candidate would mean walking thousands of large TIF
trees. Verification of what is on disk is two `stat()` calls on deterministic
names (`<series>-t%03d-p%02d.tif`), which is enough to catch a tree removed
after staging.

**Correctness.** A filesystem census compared against metadata is wrong in two
real production cases:

- **Resumed staging runs.** `planes_skipped` means "destination already existed,
  left alone" — i.e. still staged. A resumed run legitimately reports
  `planes_written: 0` with a full complement in `planes_skipped` (real case:
  `20260716_JIM801_tab-1_pha-4_L4`, `0` written / `32160` skipped). Reading only
  `planes_written` calls a fully-staged movie empty. Total staged is written +
  skipped. Only `planes_omitted` — a channel given role `skip` — is genuinely
  absent, which is also why the channel count is derived from the plane total
  rather than assumed.
- **Segmented acquisitions.** The microscope saves a late extra volume as a
  separate LIF object; staging appends it to the main time course and records
  `appended_timepoints`. The movie is then *longer* than its own metadata claims
  (real case: `20260603_ceh-27_JIM593`, 241 timepoints staged against metadata
  of 240 — 241 × 67 × 2ch = 32294). An expectation built from metadata alone
  reads that as a 67-plane overrun on every position.

## What this replaces

`tools3/CheckImages.pl` stripped `_L<n>` off the legacy XML filenames and
deleted the whole source directory if *any* position had produced an XML. An
acquisition where L1 staged and L2 failed lost L2's only copy. There was no dry
run and no per-position verification. That regression is covered by a test
(`test_one_bad_position_keeps_the_whole_source`).

## Code

- `embryodb/import_sources.py` — `plan_source_gc()` and `verify_series()`.
  Nothing in that module deletes; it reports, and the caller decides.
- `embryodb/cli.py` — the `gc-import-sources` command.
- `tests/test_import_sources.py`.

No GUI twin. This is a deliberate, infrequent, destructive operation on the only
copy of the raw data, so it stays a typed command.
