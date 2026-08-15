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

function assertFragmentLinksResolve(html, pathname) {
  const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]));
  const fragments = [...html.matchAll(/\bhref="#([^"]+)"/g)].map((match) => match[1]);
  for (const fragment of fragments) {
    assert.ok(ids.has(fragment), `${pathname} links to missing #${fragment}`);
  }
}

test("server-renders the English MURAL launch page", async () => {
  const response = await render("/");
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>MURAL Presenter — Long-horizon presentation authoring<\/title>/i);
  assert.match(html, /A presentation is not a stack of slides/);
  assert.match(html, /Long-horizon is more than slide count\./);
  assert.match(html, /ONE WALL · MANY HANDS/);
  assert.doesNotMatch(html, /class="blog-nav-icon"/);
  assert.match(html, /mural-wall-hero-v2\.webp/);
  assert.match(html, /MATERIAL<\/span><i>→<\/i><span>PLAN/);
  assert.match(html, /EXECUTION GRANULARITY/);
  assert.match(html, /href="#overview">Overview/);
  assert.match(html, /href="#task">The Task/);
  assert.match(html, /href="#method">Method/);
  assert.match(html, /href="#benchmark">Benchmark/);
  assert.match(html, /OPEN RELEASE/);
  assert.match(html, /AUTHORING LIFECYCLE/);
  assert.match(html, /IMPACT-SCOPED REVISION/);
  assert.match(html, /EVALUATION &amp; RELEASE/);
  assert.match(html, /Multi-Agent/);
  assert.match(html, /Unified/);
  assert.match(html, /Revision-Aware/);
  assert.match(html, /Authoring/);
  assert.match(html, /Long-Horizon Presentations/);
  assert.match(html, /https:\/\/github\.com\/G1nkgo7\/MURAL-Presenter/);
  assert.match(html, /href="\/paper"/);
  assert.match(html, /og\.png/);
  assert.doesNotMatch(html, /Your site is taking shape|codex-preview|react-loading-skeleton/i);
});

test("server-renders the Chinese MURAL launch page", async () => {
  const response = await render("/zh");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<main class="blog-page" lang="zh-CN"/);
  assert.match(html, /演示文稿不是一摞页面/);
  assert.match(html, /长程，不只是页数。/);
  assert.match(html, /ONE WALL · MANY HANDS/);
  assert.match(html, /MURAL Presenter · 长程创作系统/);
  assert.match(html, /mural-wall-hero-v2\.webp/);
  assert.match(html, /执行粒度/);
  assert.match(html, /href="#overview">概览/);
  assert.match(html, /href="#task">任务/);
  assert.match(html, /href="#method">方法/);
  assert.match(html, /href="#benchmark">评测/);
  assert.match(html, /把完整创作过程，变成可继续执行的 Skill。/);
  assert.match(html, /修改，也要保持整册决定。/);
  assert.match(html, /href="\/zh\/paper"/);
  assert.match(html, /多角色并行/);
  assert.match(html, /共享整册状态/);
  assert.match(html, /按影响范围续作/);
  assert.match(html, /property="og:locale" content="zh_CN"/);
  assert.match(html, /property="og:title" content="MURAL Presenter — 面向长程演示文稿的完整生命周期创作"/);
  assert.match(html, /href="\/">English<\/a>/);
});

test("redirects the legacy English launch article to the project home", async () => {
  const response = await render("/blog");
  assert.ok([307, 308].includes(response.status));
  assert.equal(response.headers.get("location"), "/");
});

test("redirects the legacy Chinese launch article to the project home", async () => {
  const response = await render("/zh/blog");
  assert.ok([307, 308].includes(response.status));
  assert.equal(response.headers.get("location"), "/zh");
});

test("server-renders the English local manuscript reader", async () => {
  const response = await render("/paper");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /paper-title-mark">MURAL-Presenter<\/span><span class="paper-title-rest">Multi-Agent Unified Revision-Aware Authoring/);
  assert.match(html, /MURAL-Presenter \(MURAL\) is a Skill-driven multi-agent framework/);
  assert.match(html, /Local draft · Results pending/);
  assert.match(html, /mural-paper-cover-en\.png/);
  assert.match(html, /Read the current working manuscript/);
  assert.match(html, /href="\/mural-paper\.pdf"/);
  assert.match(html, /href="\/zh\/paper">中文<\/a>/);
});

test("server-renders the Chinese local manuscript reader", async () => {
  const response = await render("/zh/paper");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /paper-title-mark">MURAL-Presenter<\/span><span class="paper-title-rest">Multi-Agent Unified Revision-Aware Authoring/);
  assert.match(html, /MURAL-Presenter（简称 MURAL）是一个技能驱动的多智能体框架/);
  assert.match(html, /面向长程演示文稿的多智能体统一、修改感知创作/);
  assert.match(html, /本地草稿 · 实验结果待补/);
  assert.match(html, /mural-paper-cover-zh\.png/);
  assert.match(html, /阅读当前论文工作稿/);
  assert.match(html, /href="\/mural-paper-zh\.pdf"/);
  assert.match(html, /href="\/paper">English<\/a>/);
});

test("ships the approved brand and paper assets without starter remnants", async () => {
  const required = [
    "../public/favicon-v2.png",
    "../public/mural-mark-v2.png",
    "../public/mural-mascot.png",
    "../public/mural-wall-hero-v2.webp",
    "../public/execution-topologies.png",
    "../public/authoring-lifecycle.png",
    "../public/mural-paper.pdf",
    "../public/mural-paper-zh.pdf",
    "../public/mural-paper-cover-en.png",
    "../public/mural-paper-cover-zh.png",
    "../public/og.png",
  ];
  await Promise.all(required.map((path) => access(new URL(path, import.meta.url))));
  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));

  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);
  assert.match(page, /BlogPage/);
  assert.match(layout, /favicon-v2\.png|og\.png/);
  assert.doesNotMatch(packageJson, /starter|react-loading-skeleton/);
});

test("keeps every in-page navigation fragment resolvable", async () => {
  for (const pathname of ["/", "/zh", "/paper", "/zh/paper"]) {
    const response = await render(pathname);
    assert.equal(response.status, 200);
    assertFragmentLinksResolve(await response.text(), pathname);
  }
});
