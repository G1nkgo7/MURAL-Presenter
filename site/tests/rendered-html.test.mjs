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
  assert.match(html, /A presentation is not/);
  assert.match(html, /THREAD-Bench follows requirements end to end/);
  assert.match(html, /MURAL research preview/);
  assert.match(html, /Multi-Agent/);
  assert.match(html, /Unified/);
  assert.match(html, /Revision-Aware/);
  assert.match(html, /Authoring/);
  assert.match(html, /Long-Horizon Presentations/);
  assert.match(html, /https:\/\/github\.com\/G1nkgo7\/MURAL-Presenter/);
  assert.match(html, /Local manuscript preview/);
  assert.match(html, /href="\/paper"/);
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
  assert.match(html, /代码、更新与发布/);
  assert.match(html, /本地论文预览/);
  assert.match(html, /href="\/zh\/paper"/);
  assert.match(html, /多角色并行/);
  assert.match(html, /共享整册状态/);
  assert.match(html, /按影响范围续作/);
  assert.match(html, /property="og:locale" content="zh_CN"/);
  assert.match(html, /property="og:title" content="MURAL Presenter — 面向长程演示文稿的完整生命周期创作"/);
  assert.match(html, /href="\/">English<\/a>/);
});

test("server-renders the English launch article", async () => {
  const response = await render("/blog");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /A presentation is not a stack of slides/);
  assert.match(html, /The missing unit between a deck and a slide/);
  assert.match(html, /Turning the lifecycle into an executable Skill/);
  assert.match(html, /Not more Agents\. Better responsibility boundaries\./);
  assert.match(html, /Dependency-aligned ownership/);
  assert.match(html, /CREATE AN AGENT BOUNDARY ONLY WHEN/);
  assert.match(html, /Let experiments decide which boundaries earn their cost\./);
  assert.doesNotMatch(html, /A centralized image stage resolves reusable assets/);
  assert.match(html, /MURAL-Presenter/);
});

test("server-renders the Chinese launch article", async () => {
  const response = await render("/zh/blog");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /演示文稿不是一摞页面/);
  assert.match(html, /整册和单页之间，缺少一个责任单元/);
  assert.match(html, /把完整生命周期写成可执行 Skill/);
  assert.match(html, /不是更多 Agent，而是更清楚的责任边界。/);
  assert.match(html, /依赖对齐 ownership/);
  assert.match(html, /让实验决定哪些边界真的有价值。/);
  assert.doesNotMatch(html, /统一 Image 阶段会在页面组开工前/);
  assert.match(html, /href="\/blog">English<\/a>/);
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
    "../public/favicon.png",
    "../public/mural-mark.png",
    "../public/mural-mascot.png",
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
  assert.match(page, /MuralSite/);
  assert.match(layout, /mural-mark|favicon\.png|og\.png/);
  assert.doesNotMatch(packageJson, /starter|react-loading-skeleton/);
});

test("keeps every in-page navigation fragment resolvable", async () => {
  for (const pathname of ["/", "/zh", "/blog", "/zh/blog", "/paper", "/zh/paper"]) {
    const response = await render(pathname);
    assert.equal(response.status, 200);
    assertFragmentLinksResolve(await response.text(), pathname);
  }
});
