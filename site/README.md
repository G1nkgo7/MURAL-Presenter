# MURAL-Presenter project site

This directory contains the bilingual, deployable project site for MURAL — Multi-Agent Unified
Revision-Aware Authoring for Long-Horizon Presentations.

This is the paper/project website, not the interactive presentation-authoring product. The latter
is maintained under [`../webui/`](../webui/), with its server boundary under
[`../services/api/`](../services/api/).

## Routes

- `/` — English project page
- `/zh` — Chinese project page
- `/blog` — legacy redirect to `/`
- `/zh/blog` — legacy redirect to `/zh`
- `/paper` — English working-manuscript reader
- `/zh/paper` — Chinese working-manuscript reader

## Local development

The site requires Node.js 22.13 or newer.

```bash
npm ci
cp .env.example .env.local
npm run dev
npm run build
npm test
```

Set `NEXT_PUBLIC_SITE_URL` to the canonical public base before a production build so Open Graph and
Twitter metadata resolve to the deployed site rather than the localhost development fallback.

## Dependency-free dashboard export

After building and starting the site, the repository-level exporter can copy the four canonical routes into a
static directory. Deployment-specific paths and hosts are intentionally passed at invocation time;
they are not stored in the public source tree.

```bash
python ../tools/export_dashboard_static.py \
  --origin http://127.0.0.1:3100 \
  --target ../dist/dashboard-static \
  --public-prefix /static/mural \
  --external-base https://example.org/static/mural
```

The site is intentionally static: it uses no database, object storage, sign-in, analytics, or runtime
secrets. The public release boundary is part of the page copy; do not add model results, code-release
claims, author metadata, or licenses until those artifacts are frozen.

Brand and paper-figure sources live one level above this directory. Copies in `public/` are deployment
assets and should be refreshed when their approved source changes. `public/og.png` is the approved
art-directed social card; the deterministic brand builder intentionally preserves it and writes a
fallback composition under `assets/social/` instead. The two `mural-paper-cover-*.png` previews are
rendered from page 1 of the checked-in working PDFs so the Paper routes do not depend on a browser PDF
plug-in.
