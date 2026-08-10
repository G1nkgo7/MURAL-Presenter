import { BrandLockup } from "./MuralSite";

type Language = "en" | "zh";

type ArticleSection = {
  id: string;
  title: string;
  paragraphs: readonly string[];
  figure?: {
    src: string;
    alt: string;
    caption: string;
  };
};

const article = {
  en: {
    homeLabel: "Project home",
    languageLabel: "中文",
    languageHref: "/zh/blog",
    eyebrow: "MURAL Presenter · Field Note 01",
    title: "A presentation is not a stack of slides.",
    subtitle: "Introducing MURAL-Presenter: dependency-aligned lifecycle authoring for long-horizon presentations",
    dek:
      "Drafting slides is no longer the only hard part. The systems problem is deciding where long-lived state should live, who owns cross-page relations, and how to revise one region without destabilizing the deck.",
    readTime: "Launch article · 10 min read",
    brandTagline: "One blueprint. Dependency-aligned groups. Revision at the smallest reliable scope.",
    contents: "In this article",
    closing:
      "MURAL starts from a simple premise: the goal is not to replace one workflow with more Agents. It is to give every long-lived decision an explicit state, an accountable owner, and a reliable path for review and revision.",
    repository: "View the research-preview repository",
    sections: [
      {
        id: "horizon",
        title: "The horizon is the life of a decision",
        paragraphs: [
          "Making a deck begins before the first slide and continues after the initial export. Authors clarify the audience and purpose, organize supplied material, close evidence gaps, choose a design language, plan the narrative, produce related pages, and finally review the deck as a whole. A delivered deck often returns for another round of changes.",
          "The difficult part is not any one moment. It is keeping a decision alive across the chain. A term introduced near the opening may return much later. A color can acquire a meaning that every subsequent chart must preserve. A closing slide may need to answer a question posed twenty pages earlier. A request that appears local can quietly affect several related pages.",
          "We call this long-horizon authoring. The horizon is not simply slide count or elapsed generation time. It is the distance over which facts, narrative promises, design rules, and user requirements must remain valid across stages, pages, and revisions.",
        ],
      },
      {
        id: "groups",
        title: "The missing unit between a deck and a slide",
        paragraphs: [
          "One Agent can carry a deck from beginning to end, retaining one owner and a continuous context. Its trajectory, however, accumulates research notes, plans, tool observations, slide code, render feedback, and later edits. Per-slide Agents shorten individual trajectories and expose parallelism, but related pages now live in separate execution contexts. Neither topology decides who should jointly own a cross-page relation.",
          "MURAL introduces a middle unit: the slide group. Pages belong to the same group when they share a narrative responsibility, visual family, asset series, or explicit dependency. A cover and its closing response can form one group even when they are far apart. Different groups still run in parallel, while every relation inside a group has one accountable producer.",
          "A Group Agent writes all pages in its assignment, renders them together, inspects a group contact sheet, consolidates visible defects, and revises the affected pages. Grouping is not a rigid layout template; it is shared responsibility for content and design decisions that should survive together.",
        ],
        figure: {
          src: "/execution-topologies.png",
          alt: "Sequential, full-context parallel, and MURAL slide-group execution topologies",
          caption:
            "MURAL moves joint responsibility from isolated pages into complete slide groups, then restores whole-deck validation after assembly.",
        },
      },
      {
        id: "skill",
        title: "Turning the lifecycle into an executable Skill",
        paragraphs: [
          "The MURAL Authoring Skill defines role triggers, persisted artifacts, quality gates, and continuation paths. It does not assign one Agent to every stage. Parsing, download, registration, validation, rendering, and packaging remain deterministic tools; a fresh context is reserved for bounded work with explicit ownership and an independently checkable output.",
          "The Orchestrator then fixes the audience, communicative goal, narrative arc, terminology, visual direction, page map, asset strategy, and production groups. These decisions are externalized as a shared deck blueprint and compiled into group and page briefs. Downstream Agents receive the state relevant to their responsibility instead of reconstructing the talk from a growing dialogue.",
          "Material and Research appear only when attachments or consequential evidence gaps require them. One or more Image Agents handle disjoint asset groups only when real or generated imagery is needed; deterministic asset transfer and validation remain tools. Each Group Agent closes a render–inspect–revise loop on its pages. Assembly then creates the authoritative deck and whole-deck contact sheet, and Review checks relations that cross groups.",
        ],
        figure: {
          src: "/authoring-lifecycle.png",
          alt: "The MURAL material, planning, group-authoring, review, revision, and delivery lifecycle",
          caption:
            "The reusable object is the procedure: who owns each decision, what becomes durable state, when pixels count as evidence, and where execution resumes.",
        },
      },
      {
        id: "revision",
        title: "Revision is authoring, not an afterthought",
        paragraphs: [
          "A generated presentation remains useful only if people can change it. MURAL routes later requests by impact. A precisely local request patches one page and rerenders it. A change to a cross-slide relation or shared visual language reactivates the corresponding group. New evidence or imagery adds only the necessary stages. Section structure, slide count, or shared systems can trigger affected-structure replanning.",
          "The goal is not the smallest edit at any cost. It is the smallest scope that can reliably preserve the deck’s decisions. When impact is uncertain, the router escalates rather than pretending a local patch is safe.",
          "This design also motivates a training hypothesis: generation trajectories already contain inspect, diagnose, patch, and rerender behavior. Those traces may support post-generation revision even without a separate edit-only dataset. Controlled experiments are still required to isolate that effect.",
        ],
      },
      {
        id: "html",
        title: "Why the authoring source is HTML",
        paragraphs: [
          "Image-only slides can look rich, but they collapse text, layout, and graphics into pixels. Changing a number, moving an annotation, preserving selectable text, or adding an interaction often requires regeneration or reconstruction.",
          "MURAL keeps HTML, CSS, SVG, text, and media separately addressable while treating browser pixels as the visual truth used for inspection. An Agent can patch a specific element, render the page, and inspect the actual result. PPTX, PDF, and images remain delivery adapters whose fidelity and editability must be measured rather than assumed.",
        ],
      },
      {
        id: "evaluation",
        title: "Evaluating relationships, not only pages",
        paragraphs: [
          "Per-slide quality is necessary, but it cannot show whether a deck kept its promises. Attractive pages can still use one metric in incompatible ways, abandon an opening question, or change the meaning of a color halfway through.",
          "THREAD-Bench is designed to make these long-range relations observable. Each probe records where a decision is established, which distant targets must consume or preserve it, and what observable predicate closes the relation. PresentBench and SlidesGen-Bench provide complementary measures of general generation quality, while DECKBench supplies multi-turn editing tasks.",
          "The current release is a research preview. The evaluation is designed to separate state externalization, fresh-context delegation, per-slide ownership, and dependency-aligned grouping under matched tools and budgets. Formal results, canonical benchmark cases, training artifacts, and frozen implementation releases will follow reproducibility and redistribution review.",
        ],
      },
    ] satisfies readonly ArticleSection[],
  },
  zh: {
    homeLabel: "项目主页",
    languageLabel: "English",
    languageHref: "/blog",
    eyebrow: "MURAL Presenter · 研究札记 01",
    title: "演示文稿不是一摞页面。",
    subtitle: "介绍 MURAL-Presenter：面向长程演示文稿的依赖对齐生命周期创作框架",
    dek:
      "写出单页已经不是唯一难点。真正的系统问题，是长期状态应该放在哪里、跨页关系由谁共同负责，以及如何修改一个区域而不破坏整册。",
    readTime: "发布文章 · 约 10 分钟",
    brandTagline: "一份共享蓝图，依赖对齐的页面组，以及最小可靠范围内的修改。",
    contents: "本文内容",
    closing:
      "MURAL 的出发点并不是把一个 workflow 换成更多 Agent，而是让每项长期决策都有显式状态、共同负责者，以及可靠的复审与修改路径。",
    repository: "查看 Research Preview 仓库",
    sections: [
      {
        id: "horizon",
        title: "真正的 horizon，是一项决策的有效期",
        paragraphs: [
          "制作一套演示文稿，开始于第一页之前，也不会在第一次导出时结束。作者需要先澄清受众与目标，整理已有材料、补齐证据、确定设计语言和叙事结构，再制作关联页面并从整册角度复审。交付后的演示文稿往往还会进入下一轮修改。",
          "困难不在某一个瞬间，而在于让一项决策沿整条链路持续有效。开场给出的术语可能隔了十几页才再次出现；一种颜色一旦获得语义，后面的图表就需要延续它；结尾可能要回答二十页前的问题；一个看似局部的修改，也可能影响多张关联页面。",
          "这就是 long-horizon authoring。这里的 horizon 不只是页数或生成时间，而是事实、叙事承诺、设计规则和用户要求，需要跨越阶段、页面与修改轮次保持有效的距离。",
        ],
      },
      {
        id: "groups",
        title: "整册和单页之间，缺少一个责任单元",
        paragraphs: [
          "单 Agent 可以从头到尾负责整册，保留连续上下文，但它的轨迹会不断累积研究材料、规划决策、工具反馈、页面代码、渲染检查和后续修改。逐页 Agent 缩短了单条轨迹，也便于并行，却让关联页面落入彼此独立的执行上下文。这两种拓扑都没有直接回答：一组跨页关系究竟该由谁共同负责？",
          "MURAL 在两者之间增加了 slide group。只要几张页面共享叙事职责、视觉家族、素材系列或显式依赖，就交给同一个 Group Agent。封面与结尾即使相距很远，也可以共同负责一次首尾呼应；不同页面组仍然能够并行。",
          "Group Agent 会完成组内全部页面，联合渲染，查看组联系表，合并可观察的问题，再修改受影响页面。页面组不是固定版式模板，而是让需要一起存续的内容与设计决策拥有一个共同负责者。",
        ],
        figure: {
          src: "/execution-topologies.png",
          alt: "顺序式、完整上下文并行和 MURAL 页面组执行拓扑",
          caption:
            "MURAL 把共同责任从彼此隔离的单页移动到完整页面组，并在页面汇合后恢复整册验证。",
        },
      },
      {
        id: "skill",
        title: "把完整生命周期写成可执行 Skill",
        paragraphs: [
          "MURAL Authoring Skill 定义角色触发、落盘产物、质量门和继续执行路径，但不会给每个阶段都配置一个 Agent。解析、下载、登记、校验、渲染和封装仍由确定性工具完成；只有输入有界、写入责任明确且产物可以独立验收的任务，才值得获得 fresh context。",
          "Orchestrator 随后确定受众、沟通目标、叙事弧、术语、视觉方向、页面地图、素材策略和 production groups。这些决定被外置为 shared deck blueprint，再编译成 group/page briefs。下游 Agent 只接收与自身职责相关的状态，不必从持续增长的对话中重新猜整场演讲。",
          "Material 与 Research 只在附件或关键证据缺口需要时出现；真实图片或生成图任务则由一个或多个 Image Agents 按互不重叠的 asset groups 处理，确定性素材传输与校验仍是工具。每个 Group Agent 关闭自己的 render–inspect–revise 循环；assembly 生成权威整册和 contact sheet，Whole-deck Review 再检查跨组关系。",
        ],
        figure: {
          src: "/authoring-lifecycle.png",
          alt: "MURAL 从材料、规划、分组制作到复审、修改与交付的完整生命周期",
          caption:
            "真正可复用的是这套创作程序：谁负责哪项决定，哪些状态需要持久化，什么时候像素才算证据，以及失败或修改后从哪里继续。",
        },
      },
      {
        id: "revision",
        title: "修改是 authoring 的延续，而不是附加功能",
        paragraphs: [
          "一套演示文稿只有能够继续修改，才真正具备使用价值。MURAL 按实际影响范围路由请求：精确局部修改只 patch 单页；跨页关系或共同视觉语言改变时，重新激活对应页面组；需要新证据或图片时，只增加必要阶段；章节结构、页数或共享系统变化时，才重规划受影响结构。",
          "目标并不是不惜代价地追求最小修改，而是选择能够可靠保持整册决策的最小范围。当影响边界不确定时，router 会向上升级，而不是假设局部 patch 一定安全。",
          "这也带来一个训练假设：生成轨迹本身已经包含 inspect、diagnose、patch 与 rerender，这些行为可能在没有独立 edit-only 数据集时迁移到生成后修改。它仍需要受控实验验证。",
        ],
      },
      {
        id: "html",
        title: "为什么把 HTML 作为创作源",
        paragraphs: [
          "图片式页面可以丰富，但它会把文字、布局和图形一起压成像素。改一个数字、移动标注、保留可选择文字或增加交互，往往需要重新生成或重建结构。",
          "MURAL 保留 HTML、CSS、SVG、文字与媒体的独立可寻址结构，同时把浏览器像素作为视觉检查依据。Agent 可以定向修改一个元素、重新渲染并检查真实结果。PPTX、PDF 和图片则是 delivery adapters，其保真度和可编辑性需要实际测量。",
        ],
      },
      {
        id: "evaluation",
        title: "评测跨页关系，而不只评测单页",
        paragraphs: [
          "单页质量不可缺少，但它无法说明整册有没有兑现承诺。一套页面可以张张好看，却在两处使用不兼容的指标、忘记回答开场问题，或在中途改变颜色含义。",
          "THREAD-Bench 希望把这些长程关系变成可观察对象。每个 probe 都记录决策在哪里建立，哪些远处目标需要消费或保持它，以及用什么可观察条件判定关系闭合。PresentBench 与 SlidesGen-Bench 补充通用生成质量，DECKBench 则提供多轮修改任务。",
          "当前公开的是 research preview。实验会在工具和预算匹配的前提下，分别隔离状态外置、fresh-context 委派、逐页 ownership 与依赖对齐分组的作用；正式结果、canonical benchmark cases、训练产物和冻结实现将在完成复现与再分发审查后陆续开放。",
        ],
      },
    ] satisfies readonly ArticleSection[],
  },
} as const;

export function BlogPage({ language }: { language: Language }) {
  const t = article[language];
  const isZh = language === "zh";
  const homeHref = isZh ? "/zh" : "/";
  const paperHref = isZh ? "/zh/paper" : "/paper";
  const sections = t.sections as readonly ArticleSection[];
  const acronym = isZh
    ? [
        ["M", "Multi-Agent", "多角色并行"],
        ["U", "Unified", "共享整册状态"],
        ["R", "Revision-Aware", "按影响范围续作"],
        ["A", "Authoring", "可执行创作流程"],
        ["L", "Long-Horizon Presentations", "跨阶段、页面与修改"],
      ] as const
    : [
        ["M", "Multi-Agent", "parallel specialists"],
        ["U", "Unified", "one shared deck state"],
        ["R", "Revision-Aware", "resume by impact"],
        ["A", "Authoring", "an executable lifecycle"],
        ["L", "Long-Horizon Presentations", "decisions persist"],
      ] as const;
  const researchSummary = isZh
    ? [
        ["01", "长期状态", "先把证据、叙事、术语与设计决策外置为 shared deck blueprint。"],
        ["02", "共同责任", "让具有跨页依赖的页面由同一 Group Agent 联合生成和检查。"],
        ["03", "两级闭环", "组内先闭合局部关系，装配后再由 whole-deck Review 检查跨组关系。"],
        ["04", "范围化修改", "按影响范围回到单页、页面组、证据阶段或整册规划。"],
      ] as const
    : [
        ["01", "Persistent state", "Externalize evidence, narrative, terminology, and design into one shared deck blueprint."],
        ["02", "Joint ownership", "Give cross-slide dependencies one Group Agent that authors and inspects them together."],
        ["03", "Two-level closure", "Close local relations inside groups, then validate cross-group relations after assembly."],
        ["04", "Scoped revision", "Resume at page, group, evidence, or deck scope according to the actual impact."],
      ] as const;
  const topologies = isZh
    ? [
        ["A", "单条轨迹", "共享上下文", "整册历史持续累积；局部责任与重执行边界不明确。"],
        ["B", "逐页并行", "独立页面 ownership", "单条轨迹更短，但跨页关系落在多个执行上下文之间。"],
        ["C", "MURAL", "依赖对齐 ownership", "共享蓝图投影到页面组；组内共同负责，整册复审闭合跨组关系。"],
      ] as const
    : [
        ["A", "Single trajectory", "Shared context", "Deck history keeps growing; local ownership and replay boundaries remain implicit."],
        ["B", "Per-slide parallel", "Independent slide ownership", "Workers are shorter-lived, but cross-slide relations fall between contexts."],
        ["C", "MURAL", "Dependency-aligned ownership", "A shared blueprint projects into groups; joint ownership and deck review close the relations."],
      ] as const;
  const evaluationCards = isZh
    ? [
        ["E1b", "受控主比较", "比较 monolithic、同 Skill 单上下文、逐页 workers 与完整 MURAL。"],
        ["E4", "边界消融", "区分 conditional delegation、always-delegate、Image boundary 与页面责任拓扑。"],
        ["E5", "修改与保持", "同时检验修改成功、未涉及页面保持、回归错误与 replay scope。"],
      ] as const
    : [
        ["E1b", "Matched main comparison", "Compare monolithic, same-Skill single-context, per-slide workers, and full MURAL."],
        ["E4", "Boundary ablations", "Separate conditional delegation, always-delegate, Image boundaries, and ownership topology."],
        ["E5", "Revision and preservation", "Measure edit success, untouched-slide preservation, regressions, and replay scope together."],
      ] as const;
  const figureShowcase = isZh
    ? [
        {
          index: "01",
          label: "责任拓扑",
          title: "从逐页拆分，到依赖对齐的共同责任",
          description: "比较单条轨迹、逐页并行与 MURAL，重点不是 Agent 数量，而是跨页关系在哪里被共同生成、检查和重放。",
          src: "/execution-topologies.png",
          alt: "顺序式、完整上下文并行和 MURAL 页面组执行拓扑",
        },
        {
          index: "02",
          label: "完整生命周期",
          title: "从需求接地，到分组制作与后续修改",
          description: "MURAL 把长期决策外置为共享状态，并让材料、规划、页面组、整册复审与修改路由形成可继续执行的创作链路。",
          src: "/authoring-lifecycle.png",
          alt: "MURAL 从材料、规划、分组制作到复审、修改与交付的完整生命周期",
        },
      ] as const
    : [
        {
          index: "01",
          label: "Responsibility topology",
          title: "From per-slide decomposition to dependency-aligned ownership",
          description: "The comparison is not about Agent count. It shows where cross-slide relations are jointly authored, inspected, and replayed.",
          src: "/execution-topologies.png",
          alt: "Sequential, full-context parallel, and MURAL slide-group execution topologies",
        },
        {
          index: "02",
          label: "Full lifecycle",
          title: "From grounded intent to grouped authoring and later revision",
          description: "MURAL externalizes long-lived decisions, then connects material, planning, slide groups, whole-deck review, and impact-scoped revision.",
          src: "/authoring-lifecycle.png",
          alt: "The MURAL material, planning, group-authoring, review, revision, and delivery lifecycle",
        },
      ] as const;

  return (
    <main className="blog-page" lang={isZh ? "zh-CN" : "en"} id="top">
      <header className="nav-shell blog-nav">
        <a className="brand" href={homeHref} aria-label={t.homeLabel}>
          <BrandLockup />
        </a>
        <nav aria-label={isZh ? "文章导航" : "Article navigation"}>
          <a href={homeHref}>{t.homeLabel}</a>
          <a href="#groups">{isZh ? "页面组" : "Slide groups"}</a>
          <a href="#revision">{isZh ? "修改" : "Revision"}</a>
          <a href="#evaluation">THREAD-Bench</a>
        </nav>
        <a className="language-link" href={t.languageHref}>{t.languageLabel}</a>
      </header>

      <article>
        <header className="blog-mural-hero">
          <div className="blog-mural-brush blog-mural-brush-a" aria-hidden="true" />
          <div className="blog-mural-brush blog-mural-brush-b" aria-hidden="true" />
          <div className="blog-mural-grid section-shell">
            <div className="blog-mural-copy">
              <div className="blog-mural-overline">
                <span>{t.eyebrow}</span>
                <b>FIELD NOTE · 01 / 2026</b>
              </div>
              <div className="blog-mural-word" aria-label="MURAL Presenter"><strong>MURAL</strong><span>PRESENTER</span></div>
              <h1 aria-label={t.title}>
                <span>{isZh ? "演示文稿不是" : "A presentation is not"}</span>
                <em>{isZh ? "一摞页面。" : "a stack of slides."}</em>
              </h1>
              <p className="blog-subtitle">{t.subtitle}</p>
              <p className="blog-dek">{t.dek}</p>
              <div className="blog-mural-actions">
                <div className="blog-project-actions" aria-label={isZh ? "项目资源" : "Project resources"}>
                  <a href="https://github.com/G1nkgo7/MURAL-Presenter" target="_blank" rel="noreferrer">GitHub <span>↗</span></a>
                  <a href={paperHref}>Paper <small>{isZh ? "本地预览" : "LOCAL PREVIEW"}</small></a>
                  <a href={homeHref}>{isZh ? "项目主页" : "Project page"} <span>→</span></a>
                </div>
                <div className="blog-meta">{t.readTime}</div>
              </div>
            </div>
            <div className="blog-mural-art">
              <div className="blog-mural-art-label"><span>THE NAME IS THE METHOD</span><b>01 → N</b></div>
              <div className="blog-mural-panels" aria-hidden="true"><i /><i /><i /></div>
              <img
                src="/mural-mascot.png"
                alt={isZh ? "三个 MURAL Agent 共同绘制 M 形壁画" : "Three MURAL agents jointly painting an M-shaped mural"}
              />
              <p><span>03 AGENTS · 01 DECK</span>{t.brandTagline}</p>
            </div>
          </div>
          <div className="blog-mural-legend section-shell" aria-label="Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations">
            <div className="blog-mural-legend-intro">
              <span>{isZh ? "五个字母" : "FIVE LETTERS"}</span>
              <strong>{isZh ? "一套完整方法" : "ONE AUTHORING SYSTEM"}</strong>
            </div>
            {acronym.map(([initial, term, meaning], index) => (
              <span className={initial === "R" ? "is-revision" : ""} key={initial}>
                <i>0{index + 1}</i>
                <b>{initial}</b>
                <span><strong>{term}</strong><small>{meaning}</small></span>
              </span>
            ))}
          </div>
        </header>

        <section className="blog-summary section-shell" aria-labelledby="summary-title">
          <div className="blog-summary-head">
            <span className="kicker">{isZh ? "MURAL · 一分钟读懂" : "MURAL · AT A GLANCE"}</span>
            <h2 id="summary-title">{isZh ? "不是更多 Agent，而是更清楚的责任边界。" : "Not more Agents. Better responsibility boundaries."}</h2>
            <p>{isZh
              ? "MURAL 把演示文稿看作一组需要跨阶段、跨页面和跨修改持续有效的决策。多 Agent 只是执行机制；真正的方法是状态如何外置、依赖由谁负责、在哪里验收，以及失败后从哪里继续。"
              : "MURAL treats a presentation as decisions that must survive stages, pages, and revisions. Multi-Agent execution is only the mechanism; the method is where state lives, who owns dependencies, where outputs close, and where work resumes."}</p>
          </div>
          <div className="blog-summary-grid">
            {researchSummary.map(([index, title, description]) => (
              <article key={index}>
                <span>{index}</span>
                <h3>{title}</h3>
                <p>{description}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="topology-story" aria-labelledby="topology-title">
          <div className="section-shell">
            <div className="topology-story-head">
              <span className="kicker kicker-light">{isZh ? "三种执行拓扑" : "THREE EXECUTION TOPOLOGIES"}</span>
              <h2 id="topology-title">{isZh ? "拆得开，不等于管得住。" : "Decomposable does not mean accountable."}</h2>
              <p>{isZh
                ? "关键不是 worker 数量，而是跨页关系能否找到共同生产者、共同检查者和可重放的责任单元。"
                : "The question is not worker count. It is whether a cross-slide relation has a joint producer, a joint inspector, and a replayable unit of responsibility."}</p>
            </div>
            <div className="topology-cards">
              {topologies.map(([index, title, signal, description], cardIndex) => (
                <article className={cardIndex === 2 ? "is-mural" : ""} key={index}>
                  <div className="topology-card-index">({index})</div>
                  <div className="topology-mini" aria-hidden="true">
                    <i /><i /><i />
                    <b />
                  </div>
                  <h3>{title}</h3>
                  <strong>{signal}</strong>
                  <p>{description}</p>
                </article>
              ))}
            </div>
            <div className="delegation-rule">
              <div>
                <span>{isZh ? "保留为工具" : "KEEP AS TOOLS"}</span>
                <strong>{isZh ? "解析 · 下载 · 登记 · 校验 · 渲染 · 构建" : "parse · transfer · register · validate · render · build"}</strong>
              </div>
              <i aria-hidden="true">→</i>
              <div>
                <span>{isZh ? "才建立 Agent 边界" : "CREATE AN AGENT BOUNDARY ONLY WHEN"}</span>
                <strong>{isZh ? "输入有界 · 写入互斥 · 产物可验收 · 隔离/并行/重放值得交接" : "input is bounded · writes are disjoint · output is checkable · isolation/parallelism/replay justify handoff"}</strong>
              </div>
            </div>
          </div>
        </section>

        <section className="figure-showcase section-shell" id="system-figures" aria-labelledby="figure-showcase-title">
          <div className="figure-showcase-head">
            <div>
              <span className="kicker">{isZh ? "两张图读懂 MURAL" : "MURAL IN TWO FIGURES"}</span>
              <h2 id="figure-showcase-title">{isZh ? "先看责任如何变化，再看生命周期如何闭合。" : "First see how ownership changes. Then see how the lifecycle closes."}</h2>
            </div>
            <p>{isZh
              ? "这两张图回答不同的问题：第一张解释为什么需要 slide groups，第二张解释共享状态、组内闭环、整册复审和后续修改如何串成一个系统。"
              : "The first figure explains why slide groups exist. The second connects shared state, group-local loops, whole-deck review, and later revision into one system."}</p>
          </div>

          <div className="figure-viewer">
            {figureShowcase.map((figure, index) => (
              <input
                className="figure-viewer-toggle"
                type="radio"
                name="mural-system-figure"
                id={`mural-system-figure-${index + 1}`}
                aria-label={`${isZh ? "显示" : "Show"} ${figure.label}`}
                defaultChecked={index === 0}
                key={figure.index}
              />
            ))}
            <div className="figure-viewer-bar">
              <div className="figure-viewer-status"><i aria-hidden="true" /><span>{isZh ? "MURAL 作品墙" : "MURAL GALLERY WALL"}</span></div>
              <div className="figure-viewer-tabs" aria-label={isZh ? "选择系统图" : "Choose a system figure"}>
                {figureShowcase.map((figure, index) => (
                  <label htmlFor={`mural-system-figure-${index + 1}`} key={figure.index}>
                    <span>{figure.index}</span>{figure.label}
                  </label>
                ))}
              </div>
              <div className="figure-viewer-count"><b aria-hidden="true" /> / 02</div>
            </div>
            <div className="figure-viewer-stage">
              {figureShowcase.map((figure, index) => (
                <figure className={`figure-viewer-panel figure-viewer-panel-${index + 1}`} key={figure.index}>
                  <div className="figure-viewer-canvas"><img src={figure.src} alt={figure.alt} /></div>
                  <figcaption>
                    <span>FIGURE {figure.index}</span>
                    <div><strong>{figure.title}</strong><p>{figure.description}</p></div>
                  </figcaption>
                </figure>
              ))}
            </div>
            <div className="figure-viewer-thumbs">
              {figureShowcase.map((figure, index) => (
                <label htmlFor={`mural-system-figure-${index + 1}`} className={`figure-viewer-thumb figure-viewer-thumb-${index + 1}`} key={figure.index}>
                  <img src={figure.src} alt="" />
                  <span><b>{figure.index}</b><strong>{figure.label}</strong></span>
                </label>
              ))}
            </div>
          </div>
          <p className="figure-showcase-note">{isZh
            ? "提示：点击上方标签或下方缩略图切换。交互由原生 HTML 控件完成，离线与无脚本环境同样可用。"
            : "Tip: switch with the tabs or thumbnails. Native HTML controls keep the walkthrough usable offline and without scripts."}</p>
        </section>

        <div className="article-layout section-shell">
          <aside className="article-toc">
            <strong>{t.contents}</strong>
            {sections.map((section, index) => (
              <a href={`#${section.id}`} key={section.id}>
                <span>{String(index + 1).padStart(2, "0")}</span>{section.title}
              </a>
            ))}
          </aside>

          <div className="article-body">
            {sections.map((section, index) => (
              <section id={section.id} key={section.id}>
                <div className="article-section-number">{String(index + 1).padStart(2, "0")}</div>
                <h2>{section.title}</h2>
                {section.paragraphs.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
              </section>
            ))}

            <section className="evidence-gate" aria-labelledby="evidence-gate-title">
              <div className="evidence-gate-head">
                <span>{isZh ? "RESULTS PENDING" : "RESULTS PENDING"}</span>
                <h2 id="evidence-gate-title">{isZh ? "让实验决定哪些边界真的有价值。" : "Let experiments decide which boundaries earn their cost."}</h2>
                <p>{isZh
                  ? "我们不把“用了多 Agent”直接写成收益。每项结论都必须在工具、输入、backbone 与预算匹配的比较中成立。"
                  : "We do not turn “uses multiple Agents” into an effectiveness claim. Every boundary must survive matched comparisons over tools, inputs, backbone, and budget."}</p>
              </div>
              <div className="evidence-gate-grid">
                {evaluationCards.map(([index, title, description]) => (
                  <article key={index}>
                    <span>{index}</span>
                    <h3>{title}</h3>
                    <p>{description}</p>
                  </article>
                ))}
              </div>
            </section>

            <blockquote className="article-closing">{t.closing}</blockquote>
            <div className="article-end-actions">
              <a className="button button-primary article-repo" href="https://github.com/G1nkgo7/MURAL-Presenter" target="_blank" rel="noreferrer">
                {t.repository}<span>↗</span>
              </a>
              <a className="button button-secondary" href={paperHref}>{isZh ? "阅读论文工作稿" : "Read the working paper"}<span>→</span></a>
            </div>
          </div>
        </div>
      </article>

      <footer className="section-shell">
        <a className="brand footer-brand" href={homeHref}>
          <BrandLockup />
        </a>
        <p>{t.eyebrow}</p>
        <a href="#top">↑ {isZh ? "返回顶部" : "Back to top"}</a>
      </footer>
    </main>
  );
}
