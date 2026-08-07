# MURAL project site

This directory contains the bilingual, deployable project site for MURAL — Multi-Agent Unified
Revision-Aware Authoring.

## Routes

- `/` — English project story and launch article
- `/zh` — Chinese project story and launch article

## Local development

The site requires Node.js 22.13 or newer.

```bash
npm ci
npm run dev
npm run build
npm test
```

The site is intentionally static: it uses no database, object storage, sign-in, analytics, or runtime
secrets. The public release boundary is part of the page copy; do not add model results, code-release
claims, author metadata, or licenses until those artifacts are frozen.

Brand and paper-figure sources live one level above this directory. Copies in `public/` are deployment
assets and should be refreshed when their approved source changes.

