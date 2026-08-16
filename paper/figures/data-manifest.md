# Figure Data Manifest

| Figure | Data file | Real/mock | Source | Script | Outputs | Status |
|---|---|---|---|---|---|---|
| Appendix Figure A1 | `data/pilot_a_distance_distribution.csv`; `data/pilot_b_worker_intervals.csv` | Real, observational | Frozen legacy benchmark cases (recorded before the public rename to THREAD-Bench); persisted `skill_enum3_exclusion` and `skill_enum6` traces from a legacy release | `experiments/fig_pilot_audit.py` | `experiments/fig_pilot_audit.png`, `.svg` | Ready; benchmark bookkeeping plus legacy trace-audit-method example only |
| Figure 1 | Author-approved conceptual topology diagram; no numeric data | N/A | Author-supplied 1872×840 PNG, SHA-256 `79be1408…`; current grouped MURAL narrative, with no performance claim | `architecture/approved/fig1_page_topologies.png`; `architecture/build_paper_figures.py`; `architecture/figure-style-spec.md` | `architecture/fig1_page_topologies.{svg,pdf,png}`; matching manuscript and GitHub copies | Ready; approved pixels are preserved byte-for-byte; Figure 1 covers group ownership and whole-deck closure, not later human-edit routing |
| Figure 2 | Author-approved full authoring and revision lifecycle; no numeric data | N/A | Author-supplied 1881×836 PNG, SHA-256 `b4e5d4b5…`; current grouped lifecycle and revision-router narrative | `architecture/approved/fig2_release_workflow.png`; `architecture/build_paper_figures.py`; `architecture/figure-style-spec.md` | `architecture/fig2_release_workflow.{svg,pdf,png}`; matching manuscript and GitHub copies | Ready; four lifecycle stages, local patch, group reactivation, replan, user edit, and multi-format delivery are visible |
| Figure 3 | Conceptual long-horizon agentic data-synthesis pipeline; no numeric data | N/A | Frozen 9B query, rollout, visual-critique, and trajectory-filtering contract | `architecture/fig3_long_horizon_agentic_data_synthesis.png` | matching manuscript PNG | Ready; labels distinguish the controlled 9B corpus from the heterogeneous 27B cohort |
| Figure 4 | ThreadBench requirement-thread and scoring anatomy; no numeric data | N/A | Formal v004 benchmark contract and the animal-navigation reference case | Built-in Image Generate using Figures 1--3 as style references; prompt at `concepts/imagegen/fig4_threadbench_anatomy_prompt_v1.txt`; selected master at `architecture/candidates/fig4_threadbench_anatomy_imagegen_v2.png` | `architecture/fig4_threadbench_anatomy.{png,pdf}`; matching manuscript copies | Ready; generated in the established robot/card visual system; Intermediate and Final remain independent outputs, and detailed rubric fields stay in the appendix |

The appendix figure must be captioned as benchmark bookkeeping plus a legacy-release trace audit.
It does not validate current-release execution, compare generation quality, or establish causal
long-horizon consistency.

The Pilot B source/contact-sheet sanity check is recorded in
`experiments/results/pilot_b_output_sanity.md`. It is a single observed requirement failure,
not a quality-rate figure.
