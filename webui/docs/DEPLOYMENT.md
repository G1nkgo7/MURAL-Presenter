# Deployment

Follow the [repository quick start](../../README.md) or the [WebUI guide](../README.md).
Use Python 3.12 and the two committed `uv.lock` files. Studio and generation have separate environments.
Launch via `start.sh`, `start.ps1` or `scripts/launch.py` so portable configuration is prepared.
Use a fresh workspace for the 0919 runtime; keep historical workspaces and their pinned source untouched.
The Docker build context is the repository root. See `../compose.yaml` and `../Dockerfile`.
