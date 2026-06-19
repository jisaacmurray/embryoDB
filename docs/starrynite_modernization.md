# StarryNite modernization — strategy & license-gated plan

Status: **planning complete, implementation gated on a MATLAB license.**
This doc captures the analysis so we can act on licensing and resume later.

## Context

The lab toolchain is mid-modernization. embryoDB (Python) owns metadata,
import/staging, pipeline orchestration, and the LineagePhenotyping bridge. The
remaining legacy cores are the **LIF importer/exporter**, the **Java
extractors** (RedExtractor/Measure in `acebatch3.jar`), the **viewers** (legacy
Java AceTree + the in-development napari `acetree_py`), and — the hardest —
**StarryNite** (cell detection + lineage tracking).

This doc is about **StarryNite**. It was triggered by (a) the multichannel /
one-file-per-timepoint format discussion and (b) the realization that the lab
has **hundreds–thousands of curated embryos** that could drive a systematic
accuracy/flexibility effort *if* we could extend StarryNite's code.

Today the lab has **no MATLAB license** and runs an **old** StarryNite as a
compiled Linux MCR binary (MCR v714) wrapped by `tools3/matlab_SN_cluster.pl`:
MATLAB **detection** + a compiled **C tracer** (`starrynite_traceonly`).

A reference checkout of the current upstream is at `new_tools/StarryNite/`
(github.com/zhirongbaolab/StarryNite, last pushed 2025-01-13). Findings below
come from reading it.

## Key findings (from the 2025 source)

1. **The 2025 version is 100% MATLAB** — the C tracer is gone, replaced by an
   all-MATLAB **classifier-based tracker** (`distribution_lineaging/`,
   `tracking_driver_new_classifier_based_version.m`) with shipped `.mat` models.
   This is the "updated tracker."

2. **Image-format flexibility is already solved upstream.** `processSequence.m`
   (lines 99–134) auto-detects per-plane slices (`-pNN.tif`) vs. clean
   per-timepoint multipage stacks via a ported copy of AceTree's image-name
   logic (`getImagePrefix.m`) and a new loader `loadSimpleStackTiff.m` (page =
   plane, single channel). Loader selection is runtime — `readParameters.m`
   `eval`s the param file; flags `splitstack` / `newscope` / `zeropadding` /
   `MATLAB_STACK`. **No recompile is needed to change input format.** The 2025
   per-microscope param files (`example_parameter_files/newmatlab/`) cover
   diSPIM / iSIM / spinning-disk.

3. **License is GPLv3** (`distribution_code/license.txt`). The source is legally
   modifiable / forkable / redistributable — the only barrier is the MATLAB
   build/run platform, not the rights.

4. **Octave is effectively a dead end for the modern tracker.** Tracking uses
   `fitcnb` / `ClassificationNaiveBayes` / `posterior` (Statistics & ML Toolbox)
   plus `mvnpdf`; the shipped models are serialized MATLAB classifier objects
   Octave can't load, and `fitcnb`/`posterior` aren't in Octave's stats package.

5. **The curated-embryo corpus is only exploitable through MATLAB's training
   code** (`trainingDriver_SNModelRevisions2019.m`,
   `trainConfidenceClassifier.m`, `fitcnb`). Inference-only ports can't retrain.
   This is the decisive point.

## MATLAB toolboxes required (for the license request)

Enumerated from actual function calls across `distribution_code/`,
`distribution_lineaging/`, `launcher_interface/`.

**Required — desktop seats (run-from-source + retraining + parameter sweeps):**

| Toolbox | Why (representative calls) |
|---|---|
| **MATLAB** (base) | core; GUIDE launchers (`guidata`/`uicontrol`/`.fig`) and `.jar` secondary tasks (ImageJ/flanagan/acebatch2) run on base MATLAB + bundled JVM — no extra toolbox |
| **Image Processing Toolbox** | `im2double`/`im2single` (25), `imfilter` (21), `imresize` (20), `fspecial`, `watershed`, `imdilate` |
| **Statistics and Machine Learning Toolbox** | `mvnpdf` (43), `prctile` (23), `posterior` (17), `pca`/`princomp` (8), `fitcnb` (training) |

**Required only to deploy headless on the Linux cluster:**

| Toolbox | Why |
|---|---|
| **MATLAB Compiler** | build the standalone Linux binary + MCR **once** (one seat, one build). **MCR runtime is free** → unlimited cluster jobs at zero per-run cost afterward. |

**Not required** (confirmed absent): Optimization Toolbox (`fminsearch` is base;
no `fmincon`/`linprog`/`lsqnonlin`), Parallel Computing Toolbox (no
`parfor`/`gpuArray` — *optional* later to speed sweeps), any GUI toolbox.
NIFTI support is a bundled free toolbox (`NIFTI_20080408/`, `load_nii`).

**Net ask:** MATLAB + Image Processing + Statistics & ML for desktop work; add
MATLAB Compiler if/when we want the free-to-run cluster MCR binary. Check
whether Penn's discounted/TAH license bundles these at no incremental cost
(likely).

## Options weighed

- **A. Get a MATLAB license (recommended).** Lowest-effort path to *all*
  features: run the 2025 source directly (format flexibility + better tracker,
  no recompile for param/loader changes) and — critically — unlock the
  **training** code to exploit the curated corpus. Accelerates even an eventual
  Python port (MATLAB becomes the reference oracle + label generator). Deploy
  cheaply via compile-once + free MCR. Tradeoff: re-tethers to the proprietary
  platform, but the hard-to-escape dependency is *training*, and licensing is
  likely cheap via Penn.
- **B. Adopt the prebuilt 2025 binary.** Repo ships only a *Windows* `.exe`; the
  lab is Linux → still needs MATLAB Compiler once (or a Linux build from the Bao
  lab). Collapses into A unless upstream provides a Linux binary (worth asking).
- **C. Octave.** Blocked for the classifier tracker (finding 4). Rejected.
- **D. Python reimplementation.** See pros/cons below — defer the decision.

## Long-term Python port — pros/cons (decide *after* goals 1+2)

**Pros:** platform independence (no seats, no MCR version pinning); native
embryoDB integration (no subprocess / param-file-eval indirection); modern stack
(`scipy.ndimage`/`scikit-image` for detection filters, `scikit-learn`/`torch`
for the classifier); openly distributable / pip-installable (GPLv3-compatible);
the curated corpus could train a *modern* model (3D U-Net / gradient-boosted)
that beats hand-tuned DoG + naive-Bayes.

**Cons / risks:** large, high-fidelity effort — detection (`createDiskSet` 3D
maxima, `findOverlookedNuclei` shape model, `resolveConflicts` merge/split) and
the classifier tracker are intricate and tuned over years; naive ports
underperform (what the colleague's and Gemini attempts showed). It **depends on
goal 2** (you must retrain — the `.mat` models don't transfer) and on
MATLAB-as-oracle for rigorous validation across the corpus. You also become
upstream and stop getting Bao-lab improvements for free (2025 shows they're
active).

**Framing:** the port's value is highest if the goal is deep-learning
detection/tracking and full license escape; lowest if Penn licensing is cheap
and 2025 accuracy is sufficient. After goals 1+2 we'll have a working modern
reference **and** a retraining/validation harness + corpus — i.e. the exact
instrument to *measure* a Python port's value instead of guessing. So defer.

## Recommended sequence

1. **Deploy the 2025 StarryNite on the cluster** (license-gated). Compile a
   Linux MCR binary (MATLAB Compiler, once), point embryoDB's
   `pipeline/subprocess_steps.py::step_run_starrynite` /
   `tools3/matlab_SN_cluster.pl` path at it, and adopt `loadSimpleStackTiff` so
   detection consumes the clean per-timepoint stacks the LIF-exporter rework
   (separate format-migration plan) will emit. Retire the C-tracer pipeline.
   Lowest-effort win; also resolves the one-file-per-timepoint StarryNite
   question.
2. **Stand up retraining + parameter sweeps on the curated corpus**
   (license-gated, desktop seats). Wire curated embryoDB lineages into
   StarryNite's `trainingDriver` / `trainConfidenceClassifier` to produce
   improved detection/tracking models; start with cheap parameter sweeps as the
   warm-up. High-value, lab-differentiating — and builds the validation harness.
3. **(Conditional) Python port.** Decide using the goal-2 harness as the
   measuring stick. Not committed now.

## Verification (once license is in place)

- **Goal 1:** compile the 2025 source to a Linux MCR binary; run it on one test
  series both ways — old per-plane `tif/` input *and* a clean per-timepoint
  multipage stack — and confirm equivalent detection + a completed lineage
  (`End time` reached) opening correctly in AceTree.
- **Goal 2:** retrain a model from a held-out split of curated embryos; score
  detection/tracking error (`calculateFullTrackingError.m`) against the curated
  ground truth and confirm improvement over the shipped model.

## Related

- Format-migration inventory (one-file-per-timepoint) — separate effort; the
  StarryNite side is the easy consumer (`loadSimpleStackTiff`, no recompile).
- Reference checkout: `new_tools/StarryNite/` (upstream 2025).
- Current production wrapper: `tools3/matlab_SN_cluster.pl` (old C-tracer SN).
