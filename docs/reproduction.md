# Reproducing the 0919 experiment

The exact runtime identity is `mural-presentbench-qwen35-9b-20260919T163556Z`. The source inventory,
pin and four original digests are preserved under `configs/releases/upstream-0919/`.
The original experiment's `publication-pin.json` and `runset.json` matched that runtime during audit.

The reported preparation used Qwen3.5-9B, thinking enabled, Critic/search/image-generation disabled,
238 selected PresentBench cases and at most three generation attempts per case. This is an experiment
profile, not the default policy for every WebUI model. See `configs/experiments/presentbench-qwen35-9b.json`.

Required external inputs:

1. The exact benchmark inputs, attachments and their checksums from the experiment runset.
2. A compatible model endpoint with the intended model weights and tokenizer/template settings.
3. The original evaluation adapter, font shim and Judge configuration for score reproduction.
4. MinerU services for document/image attachments and the relevant font inventory.

This repository contains the frozen runtime and the corresponding PresentBench runner/Judge sources.
The additional experiment-specific adapter/shim and original results are not certified as portable
by this packaging work. Code installation alone is not a claim that all paper scores are reproduced.
Do not substitute the WebUI defaults for the experimental generation and Judge settings.

`fonts/` contains the existing public font bundle. The original evaluation font library is larger;
using a different font inventory can change rendering. Publish appropriate acquisition instructions
and license records for any additional fonts before claiming pixel-identical reproduction.

For fresh runs, launch through `scripts/run_inference.py`, use a new workspace and retain the
release pin, model settings, input checksums and complete coverage/failure counts with results.
Do not migrate or overwrite historical workspaces. Avoid selecting successful cases after observing
scores. Add the final paper metadata, result artifacts and immutable paper tag when they are confirmed.
