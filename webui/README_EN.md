# MURAL Presenter WebUI

The bundled Studio uses the frozen 0919 `mural-presenter-v1` Skill and Harness.
See the [root quick start](../README.md), [configuration](docs/CONFIGURATION.md),
[deployment](docs/DEPLOYMENT.md) and [release provenance](../docs/release-provenance.md).

Launch with `bash webui/start.sh` from the repository root (PowerShell: `webui/start.ps1`).
For a read-only dependency/release check use
`python webui/scripts/launch.py --check --no-install --no-browser-install`.
The launcher may create the ignored local runtime configuration on first use.
Existing credentials, databases, workspaces and website files are not migrated or deleted.
