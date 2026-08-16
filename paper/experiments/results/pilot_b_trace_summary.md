# Pilot B — Existing production-trace audit

No new model calls were made.

| Run | Pages | Slide wall (s) | Worker sum (s) | Compression | Peak workers | Strict workers | Peer reads | Whole-deck reads | Shared writes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| skill_enum3_exclusion_0727_0330_adhoc | 3 | 407.3 | 759.2 | 1.86× | 3 | 3/3 | 0 | 0 | 0 |
| skill_enum6_0727_0305_adhoc | 6 | 369.0 | 1049.9 | 2.85× | 6 | 5/6 | 1 | 0 | 0 |

Interpretation boundary: this audit verifies observed scheduling and logged file/tool behavior. It does not estimate quality gains, causal token savings, or performance against another system.
