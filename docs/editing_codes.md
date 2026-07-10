# Editing times & codes in embryoDB

When a curator hand-corrects a StarryNite lineage in AceTree, embryoDB records
**how far the curation is trustworthy**. This note explains where that lives and
how to interpret it — e.g. to scope a movie to its ground-truth extent when
training or evaluating a tracker / curation-effort model.

Four fields on each `Series` (the DB is authoritative; the legacy
`<series>.xml` is a mirror tag, shown in parentheses):

| Field | XML tag | Type | Meaning |
|---|---|---|---|
| `edited_timepts` | `<editedtimepts num>` | int | The movie is **fully curated from t=1 through this timepoint**. This is the global curation depth — at or below it the lineage is ground truth; above it it's raw StarryNite. This is the field downstream tools use to scope to curated data. |
| `partial_editing_code` | `<checkedby name>` | string | **Per-branch refinement** when some lineages were curated deeper than the global depth. See grammar below. |
| `edited_cells` | `<editedcells num>` | int | Approximate count of curated cells — coarse; don't use for precise scoping. |
| `edited_by` | `<editedby name>` | string | Curator's initials. |

## `partial_editing_code` grammar

- Comma-separated entries, each `CellName:time` (e.g. `ABpl:150,ABpr:150,P2:200`).
- Each entry means: **the sublineage rooted at `CellName` is curated through
  timepoint `time`** (deeper than, or instead of, the global `edited_timepts`).
- A bare integer `100` is shorthand for `P0:100` (whole embryo from the root
  cell `P0` to t=100).
- Sentinels meaning "no partial code" (fall back to `edited_timepts` alone):
  empty, `n/a`, `none`, `na`, `-`.
- Anything that doesn't match the grammar → treat as "no partial code."

The grammar matches `PartialCSV.java`'s `editArray` parser (looser than the old
`Partial.pl` regex). `partialCSV.jar` consumes the canonical
`CellName:time,...` form to trim each cell's descendants to its checked extent.

## How to extract

- **From the DB** (authoritative — the GUI writes here; the XML can lag): read
  `series_name, edited_timepts, partial_editing_code, edited_cells, edited_by`
  off `Series` via the `queries.series` API or a CLI export.
- **Parse the code** with the existing helper — don't re-implement the grammar:

  ```python
  from embryodb.partial import parse_editing_code, canonicalize

  rules = parse_editing_code(series.partial_editing_code)
  # rules: list[PartialEditRule(cell_name, time)]  — or None if empty/sentinel/invalid
  code = canonicalize(rules)  # render rules back to "CellName:time,..." if needed
  ```

## Interpreting "how much is ground truth"

1. **Global.** Timepoints `1 … edited_timepts` are curated for the **whole**
   embryo.
2. **Per branch.** For each `CellName:time` rule, the sublineage under
   `CellName` is additionally curated to `time` (typically ≥ the global depth on
   the branches that mattered).
3. **Everything else** is raw tracker output, **not** ground truth — exclude it
   when scoring the tracker or training/evaluating the effort predictor.
