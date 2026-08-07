import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

async function render(pathname) {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}-${pathname}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${pathname}`, {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the English MURAL launch page", async () => {
  const response = await render("/");
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>MURAL — Long-horizon presentation authoring<\/title>/i);
  assert.match(html, /A presentation is not/);
  assert.match(html, /THREAD-Bench follows requirements end to end/);
  assert.match(html, /MURAL research preview/);
  assert.match(html, /og\.png/);
  assert.doesNotMatch(html, /Your site is taking shape|codex-preview|react-loading-skeleton/i);
});

test("server-renders the Chinese MURAL launch page", async () => {
  const response = await render("/zh");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<main lang="zh-CN">/);
  assert.match(html, /演示文稿不是/);
  assert.match(html, /机制已经实现，效果结论仍等待正式实验/);
  assert.match(html, /href="\/">English<\/a>/);
});

test("ships the approved brand and paper assets without starter remnants", async () => {
  const required = [
    "../public/favicon.png",
    "../public/mural-mark.png",
    "../public/mural-mascot.png",
    "../public/execution-topologies.png",
    "../public/authoring-lifecycle.png",
    "../public/og.png",
  ];
  await Promise.all(required.map((path) => access(new URL(path, import.meta.url))));
  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));

  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);
  assert.match(page, /MuralSite/);
  assert.match(layout, /mural-mark|favicon\.png|og\.png/);
  assert.doesNotMatch(packageJson, /starter|react-loading-skeleton/);
});

