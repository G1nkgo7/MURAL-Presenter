<a name="overview"></a>
<a name="readme-en"></a>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/exports/street-v2/mural-banner-dark.png">
    <img src="assets/logo/exports/street-v2/mural-banner-light.png" width="1000" alt="MURAL Presenter — Multi-Agent Units of Responsibility for Authoring Long-Horizon Presentations">
  </picture>
</p>

<h1 align="center"><img src="assets/logo/exports/street-v2/mural-avatar-circle.png" width="40" height="40" align="absmiddle" alt=""> MuralPresenter</h1>
<p align="center"><strong>Multi-Agent Units of Responsibility for Authoring Long-Horizon Presentations</strong></p>
<p align="center">From a prompt or documents to slides you can inspect and revise</p>

<p align="center">
  <strong>English</strong> · <a href="README_zh-CN.md">简体中文</a>
</p>
<p align="center">
  <a href="#see-it-in-action">🖼️ Examples</a> ·
  <a href="#quick-start">🚀 Quick start</a> ·
  <a href="webui/README_EN.md"><img src="webui/studio/static/logo.svg" width="18" height="18" align="absmiddle" alt=""> WebUI</a>
</p>

## News

Paper and release announcements will be added here.

## See it in action

> **Request, condensed:** Create 40 teaching slides for senior life-sciences undergraduates on
> geomagnetic, sun and star compasses, with scientific diagrams, real photographs and a comparison table.

**Actual output · MuralPresenter-27B (Global Selective, 2 epochs / checkpoint-64).**
Pages 19 and 37 from one completed 40-slide Chinese deck; click to view the original renders.

<table>
  <tr><th width="50%">Page 19 · Sun-compass mechanism</th><th width="50%">Page 37 · Calibration and redundancy</th></tr>
  <tr>
    <td><a href="assets/figures/animal-compass/slide-19.png"><img src="assets/figures/animal-compass/slide-19.png" width="100%" alt="Actual generated page 19: time-compensated sun compass, diagram and explanation"></a></td>
    <td><a href="assets/figures/animal-compass/slide-37.png"><img src="assets/figures/animal-compass/slide-37.png" width="100%" alt="Actual generated page 37: calibration and redundancy among three compass systems"></a></td>
  </tr>
</table>

[View all three selected pages, including the comparison table](assets/figures/animal-compass/README.md) ·
Original 1600 × 900 renders, copied without editing from an archived run.

<details>
<summary>Another example: revise an existing presentation</summary>

**The same deck, revised through its existing Owners.** The manuscript's qualitative case expands
a presentation of this paper from ten to twelve slides, then changes its blackboard styling to
light blue and white while refining diagrams. Below is page 3 before and after revision; the gallery contains six pairs.

<table>
  <tr><th width="50%">Before · Blackboard</th><th width="50%">After · Blue and white</th></tr>
  <tr>
    <td><a href="assets/figures/revision-case/before-03.png"><img src="assets/figures/revision-case/before-03.png" width="100%" alt="Before revision: page 3 explaining Single and Group Owners"></a></td>
    <td><a href="assets/figures/revision-case/after-03.png"><img src="assets/figures/revision-case/after-03.png" width="100%" alt="After revision: page 3 with blue and white styling and updated responsibility diagrams"></a></td>
  </tr>
</table>

[View all six before/after pairs](assets/figures/revision-case/README.md) ·
[Figure sources](assets/figures/README.md). These are manuscript exports, not a live WebUI demo;
the qualitative case is excluded from benchmark means.

</details>

## Quick start

Use Python 3.12 and `uv`. Provide your own model endpoint and credentials.

```bash
git clone https://github.com/G1nkgo7/MURAL-Presenter.git
cd MURAL-Presenter
uv sync --project webui/studio --frozen
uv sync --project webui/inference --frozen
test -f webui/.env || cp webui/.env.example webui/.env
bash webui/start.sh
```

Open the address printed by the launcher, normally `http://127.0.0.1:8001`.
Add a multimodal model with its API base URL, model name and API key.

<details>
<summary>Configuration, attachment parsing and optional services</summary>

Copy the example only on first setup; preserve an existing `.env`. On Windows use `webui/start.ps1`.
The first launch can install Chromium. It creates ignored `configs/runtime.local.json`
without overwriting an existing configuration. Edit that file for concurrency and ordered
GPU/CPU MinerU endpoints, or select an existing file with `MURAL_RUNTIME_CONFIG`.
PDF/Office/image attachments require MinerU; text-only tasks do not require an attachment parser.
Search and image generation require separately configured providers.

</details>

**Try a short task.** Create a case and send:

> Create a six-slide lesson on how animals use magnetic, sun and star compasses.
> Include explanatory diagrams and a comparison table.

**Then revise it.** Once generation finishes, send in the same case:

> Reduce the text and make the direction conventions consistent across the diagrams and comparison table. Keep six slides.

Preview and download the result. For deployment, data paths and troubleshooting, see the [WebUI guide](webui/README_EN.md).

<a name="command-line-inference"></a>

<details>
<summary>Command-line inference</summary>

```bash
webui/inference/.venv/bin/python scripts/run_inference.py \
  --query "Create a six-slide research presentation" \
  --workspace "$PWD/artifacts/example"
```

Use `--help` for arguments or `--doctor --json artifacts/doctor.json` for diagnostics.
Use a new workspace; preserve historical workspaces and their pinned runtime.

</details>

## Research

<a name="results-at-a-glance"></a>

Scores below are transcribed from the current manuscript's public-benchmark tables, not new
runs of this repository. All four rows use the MuralPresenter authoring framework.

| Backbone / trained model | PresentBench Overall ↑ | SlidesGen Content (%) ↑ | SlidesGen Aesthetics ↑ |
| --- | ---: | ---: | ---: |
| Qwen-3.5-9B | 41.1 | 74.71 | 14.46 |
| **MuralPresenter-9B** | **47.3** | **90.21** | **18.38** |
| Qwen-3.8-27B | 69.4 | 93.33 | 18.58 |
| **MuralPresenter-27B** | **72.6** | **94.92** | **19.12** |

PresentBench covers 238 tasks; SlidesGen-Bench covers 189. The metrics use different scales.
Checkpoint selection and generation conditions matter; see [evaluation protocols](#evaluation).

<details>
<summary><strong>Analysis figures · responsibility workload and selective supervision</strong></summary>

**Responsibility workload.** Mean recorded production input and output tokens per task across
35 matched tasks, excluding shared preparation. Counts are lower bounds and do not measure
elapsed-time speedup.

<p align="center">
  <a href="assets/figures/responsibility-workload.png"><img src="assets/figures/responsibility-workload.png" width="800" alt="Recorded production tokens by role for Whole-deck, All-Single and Unit-adaptive policies"></a>
</p>

**Selective supervision.** Global versus Full quality differences with paired 95% bootstrap
intervals, alongside input tokens by role. These exploratory results describe runs with differing
tool conditions; they do not isolate the effect of training.

<p align="center">
  <a href="assets/figures/supervision-ablation.png"><img src="assets/figures/supervision-ablation.png" width="640" alt="Global versus Full supervision: quality differences and input tokens by agent role"></a>
</p>

</details>

<a name="method"></a>

<details>
<summary>How it works: shared knowledge and page Owners</summary>

The Orchestrator maintains shared facts, page plans and assets. A page's **Owner** is the agent
responsible for creating, checking and revising it. Independent pages use Single Owners;
pages whose content or design decisions need joint adjustment share a Group Owner.
Later changes return to the responsible Owners, followed by another visual check and deck review.

<p align="center">
  <a href="assets/figures/ownership-workflow.png"><img src="assets/figures/ownership-workflow.png" width="100%" alt="Shared knowledge and plans, Single and Group Owners, visual inspection and revision"></a>
</p>

The animal-compass figure illustrates the method, not the archived example run's exact grouping
or a recorded edit sequence. See [cross-page dependencies](assets/figures/cross-page-dependencies.png) and [agent roles](assets/figures/roles-and-tools.png).
HTML/CSS/SVG is the editable authoring source; PPTX, PDF and image exports are delivery formats.

</details>

<a name="models-and-trajectory-training"></a>

<details>
<summary>Models and training details</summary>

The paper trains **MuralPresenter-9B** and **MuralPresenter-27B** on **9,000 quality-filtered
execution segments from 686 native source records**. These counts describe segments and sources,
not 9,000 completed presentations. Each segment keeps its preceding history as input context.

The public-benchmark checkpoints use **Global Selective**:

| Model | Backbone | Evaluated checkpoint |
| --- | --- | --- |
| MuralPresenter-9B | Qwen-3.5-9B | 4 epochs, 128 optimizer steps |
| MuralPresenter-27B | Qwen-3.8-27B | 2 epochs, 64 optimizer steps |

Model weights are not bundled with this source release.

**Training recipe and supervision policies**

| Supervision policy | Assistant actions receiving training loss |
| --- | --- |
| Full | Historical and current-segment actions. |
| Global Selective | Only current-segment actions, for every role. |
| Role-wise Selective | Current-segment actions for Slide; Full supervision for the other roles. |

Training uses MS-SWIFT, Megatron and BF16 with frozen vision encoders and mergers, a maximum
sequence length of 131,072 tokens and batch size 64. The supervision-policy comparison uses
four-epoch 27B models, distinct from the two-epoch 27B public-benchmark checkpoint.
These are paper experiment settings, not WebUI generation defaults.

</details>

<a name="evaluation"></a>

<details>
<summary>Benchmark coverage, metrics and interpretation</summary>

| Evaluation | Coverage in the manuscript | What it measures |
| --- | --- | --- |
| PresentBench | 238 tasks | Delivered presentation quality under task-specific rubrics. |
| SlidesGen-Bench | 189 tasks | Content accuracy and computational aesthetics. |
| DECKBench | 294 tasks, five editing turns each | Response to multi-turn editing through benchmark-defined similarity changes. |
| Internal long-deck tasks | 35 Chinese/English requests across six domains, requiring 20–40 pages | Long-deck final quality and separate process evidence. |

The internal evaluation separates quality and process. **Overall = 70% Deck + 30% Page**; Knowledge is part of Deck.
**Process** independently evaluates research support and adoption (**R**), executable planning
(**P**), asset preparation and actual-use handoffs (**I**), and plan realization, render inspection
and slide–speech alignment (**S**). Process is not included in Overall. The current process
analysis excludes criteria requiring authoring trajectories and includes formal and diagnostic
records. Missing compatible action records do not by themselves establish that an action was omitted.

**Experiment protocols and interpretation**

Interpret the experiments under their recorded protocols:

- Ten PresentBench tasks and ten internal long-deck tasks informed checkpoint selection and remain in the respective reported means. The complete selection history has not been reconstructed.
- DECKBench imports preceding artifacts into a fresh workspace each turn, evaluating artifact continuity rather than persistent Owner history.
- Adaptive and per-slide ownership achieve similar mean Overall scores; both use more recorded production work than whole-deck ownership. The study does not establish elapsed-time speedup.
- Search availability, image services and quotas differ across the supervision-study runs, limiting causal attribution to the training policy.

</details>

## Brand assets

The standalone mark, avatar and painter mascot share the banner’s street-mural identity. Click a preview to open its original PNG.

<table>
  <tr><th width="33%">Primary mark · Transparent</th><th width="33%">Avatar · Circular badge</th><th width="33%">Painter mascot · Transparent</th></tr>
  <tr><td align="center"><a href="assets/logo/exports/street-v2/mural-mark.png"><img src="assets/logo/exports/street-v2/mural-mark.png" width="160" alt="Primary mark · Transparent"></a></td><td align="center"><a href="assets/logo/exports/street-v2/mural-avatar-circle.png"><img src="assets/logo/exports/street-v2/mural-avatar-circle.png" width="160" alt="Avatar · Circular badge"></a></td><td align="center"><a href="assets/logo/exports/street-v2/mural-mascot.png"><img src="assets/logo/exports/street-v2/mural-mascot.png" width="160" alt="Painter mascot · Transparent"></a></td></tr>
</table>

<details>
<summary>Wordmarks and paper sharing card</summary>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/exports/street-v2/mural-wordmark-dark.png">
    <img src="assets/logo/exports/street-v2/mural-wordmark-light.png" width="440" alt="MURAL Presenter">
  </picture>
</p>

The wordmark follows your reader’s light or dark theme.

<p align="center">
  <a href="assets/logo/exports/street-v2/mural-social-card.png"><img src="assets/logo/exports/street-v2/mural-social-card.png" width="640" alt="MuralPresenter — Multi-Agent Units of Responsibility for Authoring Long-Horizon Presentations"></a>
</p>

[Light wordmark](assets/logo/exports/street-v2/mural-wordmark-light.png) · [Dark wordmark](assets/logo/exports/street-v2/mural-wordmark-dark.png) · [Paper sharing card](assets/logo/exports/street-v2/mural-social-card.png)

</details>

See the [brand guide](assets/logo/exports/street-v2/README.md) for the full asset set, usage and version history.

## Release status

**Available:** the frozen 0919 Skill/Harness, WebUI, launchers and public artwork. Training data,
internal evaluation pipelines and model weights are not bundled. Paper link and final metadata: to be added.

<details>
<summary>Source versions and paper-score reproduction</summary>

The runtime source pin and trained model checkpoints are separate version identities.
The 0919 source pin does not establish that every later paper experiment used an identical
runtime and configuration.

Score reproduction additionally requires exact inputs and checksums, weights and chat templates,
evaluation adapters, font shims, Judge settings and output-selection/coverage records.
The original evaluation font inventory was larger than this repository's font bundle.
Running the application or passing source checks does not reproduce the paper's scores.

</details>

<a name="repository-layout"></a>
<a name="versioning-and-validation"></a>

Directory layout, source checks and known limitations: [Contributing](CONTRIBUTING.md#repository-layout) · [CI](.github/workflows/public-surface.yml).

<a name="license-and-citation"></a>

## Citation

Provisional citation for the manuscript, which has not yet been submitted. Author and publication details will be updated.

```bibtex
@misc{muralpresenter2026,
  title  = {MuralPresenter: Multi-Agent Units of Responsibility for Authoring Long-Horizon Presentations},
  author = {Anonymous Authors},
  year   = {2026}
}
```

Machine-readable metadata: [CITATION.cff](CITATION.cff).

## License

Project license: to be confirmed. Existing [third-party terms](webui/THIRD_PARTY_NOTICES.md) and [font licenses](fonts/FONTS.md) apply.
