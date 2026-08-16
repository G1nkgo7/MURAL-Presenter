# Table Schema

All result tables are placeholders until backed by real logs.

## Active manuscript result tables

| Table | Purpose | Row blocks | Metrics | Data source | Interpretation boundary |
|---|---|---|---|---|---|
| RQ1 / Table 1 | Position general generation quality against both matched controls and published benchmark leaders | selected original-paper systems (`reported`); five-system unified rerun cohort | PresentBench Overall; SlidesGen-Bench Content, Aesthetics, PEI | primary-paper values in `plan/review/reported-benchmark-scores-20260813.md`; frozen rerun logs | reported rows retain source protocols and are not pooled with reruns |
| RQ2 / Table 2 | Position multi-turn revision and supplementary PowerPoint editing against published leaders | selected DECKBench systems/configurations (`reported`); five-system unified rerun cohort; PPT-Eval reported and ours | Fidelity, Layout, Transition Similarity, signed multi-turn deltas; PPT-Eval Success/Partial | primary-paper/project values in the score ledger; frozen rerun logs | dashes denote unreported/inapplicable fields; PPT-Eval remains a separate protocol |
| RQ3 / Table 3 | Compare process–artifact consistency | six strong general-purpose controls; MuralPresenter-9B/27B | ThreadBench Intermediate, Final, effective Deck/Page | frozen ThreadBench runs | end-to-end system comparison; one formal case limits domain inference |
| RQ4 / Table 4 | Isolate design choices | shared full 9B reference and single-factor variants | ThreadBench Intermediate, Final, cross-page semantic consistency | controlled 9B ablation logs | same 1K corpus and fixed non-ablated variables |

| Table | Purpose | Rows | Metrics | Data source | Replacement owner |
|---|---|---|---|---|---|
| Table 1 | Compare related systems and benchmarks | PPTAgent, PreGenie, SlideBot, DeepPresenter, LH-Bench, PresentBench, SlidesGen-Bench, DECKBench, PPTArena, MuralPresenter/THREAD-Bench | task, trajectory/output horizon, grounding, Skill-grounded process evaluation, deck-level requirements, cross-slide evaluation, explicit source/target/span, editability | Verified papers and benchmark metadata | Paper lead |
| Table 2 | Main full-deck comparison | External systems and controlled variants | deck-level must-requirement satisfaction, dependency closure/distance slices, deployable-as-is, factual/terminology/narrative/visual consistency, completion, 95% CI; no official scalar overall | Frozen benchmark runs | Experiment owner |
| Table 3 | Skill/global-plan/page-brief/Review ablations | generic Skill, no global plan, isolated page request, full-context worker, local-only/no Review, full MuralPresenter | requirement satisfaction, dependency closure/span, four consistency families, Review repair/regression, conformance and completion; operational fields last | Controlled ablation logs | Systems experiment owner |
| Table 4 | Judge calibration | rubric dimensions and horizon strata | inter-human agreement, human–judge agreement, precision/recall where applicable | Human annotation export | Evaluation owner |
| Appendix Table A1 | Secondary operational accounting | controlled systems and principal ablations | wall-clock, critical path, calls, tokens, estimated cost, context overlap, renders/views, total work, three concurrency settings | Controlled run traces | Systems experiment owner |
| Pilot table | Historical implementation audit, not a main result | existing 3-page and 6-page traces | observable scheduling/read behavior, calls/tokens/renders/views, and one bounded output sanity check | Persisted legacy manifests/events/tool logs, canonical CSS, and contact sheet | Paper lead |
