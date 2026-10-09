# Minimal example

From the repository root, after dependency and model configuration:

```bash
webui/inference/.venv/bin/python scripts/run_inference.py --query "Explain the water cycle in six slides" --workspace "$PWD/artifacts/water-cycle"
```

Use a new workspace. PDF/Office attachments require configured external MinerU services.
