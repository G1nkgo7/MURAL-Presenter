# Troubleshooting

- Release hash mismatch: run `python tools/check_paper_release.py` from the repository root;
  restore the intended source version rather than enabling a dirty override in production.
- Missing runtime config: launch through the provided launcher; check `MURAL_RUNTIME_CONFIG`.
- Missing dependencies: synchronize both WebUI environments using their committed locks.
- Missing Chromium: allow the launcher to install it, or set `PLAYWRIGHT_BROWSERS_PATH`.
- Attachment parsing failed: check both MinerU endpoints in the local runtime config.
- Wrong model or authentication: review local model settings; do not put credentials in a bug report.
- Old workspace incompatibility: preserve it and start a new workspace; no automatic migration is performed.
