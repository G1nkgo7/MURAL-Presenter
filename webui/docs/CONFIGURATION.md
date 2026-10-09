# Configuration

Use the [WebUI configuration guide](../README.md#配置与数据).
`configs/runtime.json` is a versioned portable template; the launcher resolves it into ignored
`configs/runtime.local.json` without overwriting an existing deployment configuration.
Set `MURAL_RUNTIME_CONFIG` to use an existing absolute configuration file.
The frozen runtime requires distinct ordered GPU/CPU MinerU endpoints and absolute shared resource paths.
Configure your own model in the UI or the local environment; no service credentials are supplied.
