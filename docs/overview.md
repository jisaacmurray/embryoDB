# Analysis tiers — quick map of options

A one-page summary of the lab toolchain from raw microscopy to phenotype
calls, and the main option at each tier. Every tier has a **GUI** and a
**CLI** path that share one code path (CLI ↔ GUI parity), so pick whichever
fits. Commands assume the system-wide install (`embryodb` on `$PATH`); see
the README for setup.

The three repos involved:
- **embryoDB** (`new_tools/embryoDB`) — metadata DB, import/staging, pipeline
  orchestration, launchers, phenotyping freeze.
- **acetree_py** (`new_tools/acetree_py`) — napari-based AceTree rewrite (curation).
- **LineagePhenotyping** (`new_tools/LineagePhenotyping`) — R analysis of
  cell-cycle / position / division-orientation defects.

---

## Tier 0 — Browse & edit metadata

What: filter / inspect / edit the 16-field series records and datasets.

| | |
|---|---|
| GUI | `embryodb-gui` → filter bar, browser table, detail panel |
| CLI | `embryodb list [--gene G --person P --status S …]`, `embryodb show <series>`, `embryodb dataset …` |
| Notes | Works over SSH X11 forwarding. Read-only consumers: see `docs/data_access.md`. |

## Tier 1 — Import & stage an acquisition

What: raw Leica TileScan → `tif/`,`tifR/`,`DIC/` + `dats/<series>.xml` +
legacy embryoDB XML + matlab params.

| | |
|---|---|
| GUI | **File → Import acquisition…** wizard (previews positions, per-series overrides, spawns worker) |
| CLI | `embryodb pipeline import-acquisition <src> --protocol P --person U --strain S` ; `.lif`: `embryodb pipeline import-lif` |
| Notes | `stage_images` is the slow step; the wizard's **Delay (hours)** defers it to off-hours via the worker. |

## Tier 2 — Lineage detection (StarryNite) + red/measure

What: nucleus detection & tracking (StarryNite), then RedExtractor + Measure.

| | |
|---|---|
| GUI | runs automatically as pipeline steps 7–9 (Pipeline column shows progress); **Re-run pipeline…** to re-queue a subset |
| CLI | `embryodb pipeline rerun <series> [--steps …]` |
| Notes | Legacy compiled Matlab/Java under `tools3/`. Detection-collapse auto-recovery runs inside `step_run_starrynite`. Modernization strategy (2025 upstream + retraining on the curated corpus, license-gated): `docs/starrynite_modernization.md`. |

## Tier 3 — Curate & annotate (AceTree)

What: review/correct nuclei and the lineage tree. **Two** AceTree options:

| Option | Launch (GUI right-click / CLI) | Channels | Display |
|---|---|---|---|
| Java AceTree (legacy) | "Open in AceTree" / `embryodb launch-acetree <series>` | green + red (2-ch, per-plane 8-bit) | fine over X11 |
| AceTree-Py (napari) | "Open in AceTree (Python)" / `embryodb launch-acetree-py <series>` | green + red + DIC (auto-discovers `tifR/`,`tifC<n>/`) | needs OpenGL ≥2.1 → over remote X11 use **`acetree-py-vnc <config>`** (see `docs/troubleshooting.md`) |

Notes: the Java jar cannot show a 3rd channel from per-plane 8-bit slices;
AceTree-Py de-interleaves / composites extra channels natively.

## Tier 4 — Phenotyping (downstream analysis)

What: freeze a dataset's `dats/*.csv`, build the four input tables, then run
the defect-calling analysis + figures.

| Step | Command |
|---|---|
| Freeze (embryoDB) | GUI **Freeze for phenotyping** / `embryodb phenotyping freeze <dataset> [--output-base DIR]` → freeze dir + `configs/<dataset>.yaml` + list + report |
| ACD gen | `embryodb phenotyping getacd <dataset>` — runs the Python port (`embryodb.getacd`) per series; same code as the `getacd` extract step |
| Build inputs (R) | `Rscript build_inputs.R <freeze_dir> <name> [output_dir]` (LineagePhenotyping) → `…DivTimeNorm.tsv`,`…CCLengthNorm.tsv`,`…CCLengthMinTerminal.tsv`,`…positions.txt` |
| Analyze (R) | `Rscript run_pipeline.R configs/<dataset>.yaml` → defect calls + figures |

Notes: everything lands under `/murrlab3/<user>/phenotyping/<dataset>/`. The
freeze dir doubles as the R `data_dir`, so no file shuffling between steps.
A collaborator with only a `dats/*.csv` freeze can run the R half standalone.

---

## Display transport (which tier needs what)

- **embryodb-gui** and **Java AceTree**: SSH X11 forwarding is fine (see
  `docs/troubleshooting.md` for XQuartz black-fill fixes).
- **AceTree-Py (napari)**: X11 forwarding cannot supply the required OpenGL;
  use the `acetree-py-vnc` VNC launcher.
- **R analysis**: headless — produces files/figures, no live display needed.
