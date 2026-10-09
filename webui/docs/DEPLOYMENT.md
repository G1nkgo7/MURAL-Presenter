# Deployment

Follow the [repository quick start](../../README.md) or the [WebUI guide](../README.md).
Use Python 3.12 and the two committed `uv.lock` files. Studio and generation have separate environments.
Launch via `start.sh`, `start.ps1` or `scripts/launch.py` so portable configuration is prepared.
Use a fresh workspace for the 0919 runtime; keep historical workspaces and their pinned source untouched.
The Docker build context is the repository root. See `../compose.yaml` and `../Dockerfile`.

## Shared port and subpath deployment

Set `STUDIO_BASE_PATH=/studio` in the Studio environment. The reverse proxy must send
`/studio/...` to Studio after removing `/studio`; `/studio` redirects to `/studio/`.
The application generates prefixed asset, API, preview, download, login and cookie paths.
Leave `STUDIO_BASE_PATH` empty for a standalone root deployment.

`webui/scripts/cci-gateway.mjs` provides a Node.js gateway for the three existing services:

| External path | Local upstream | Path handling |
| --- | --- | --- |
| `/` and other paths | `DASHBOARD_PORT`, default 8002 | Preserved |
| `/mural-presenter/` | `MURAL_PORT`, default 3102 | Preserved |
| `/studio/` | `STUDIO_PORT`, default 8010 | `/studio` removed |

The gateway listens on `PORT` (default 8001). Set the same `STUDIO_BASE_PATH` on gateway
and Studio. Existing dashboard `/api/` and `/static/` endpoints stay with the dashboard;
Studio only owns its explicit prefix. Request bodies and event streams pass through.
The gateway does not rewrite response bodies or published presentation files.

Start Studio on its internal port with the normal launcher, then start the gateway:

```bash
STUDIO_BASE_PATH=/studio python webui/scripts/launch.py --host 127.0.0.1 --port 8010
STUDIO_BASE_PATH=/studio PORT=8001 DASHBOARD_PORT=8002 MURAL_PORT=3102 STUDIO_PORT=8010 \
  node webui/scripts/cci-gateway.mjs
```

Check whether the gateway port is already occupied before starting it. Replace an existing
gateway only after recording its routes and confirming the intended cutover.
