import { BrandLockup } from "./MuralSite";
import { LifecycleExplorer, RevisionRouter, TopologyExplorer } from "./MuralInteractions";
import { ProjectNav } from "./ProjectNav";
import { HeroExplorer } from "./HeroExplorer";
import { MuralBrush } from "./MuralBrush";

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
    languageHref: "/zh",
    eyebrow: "MURAL Presenter · Long-horizon authoring system",
    title: "A presentation is not a stack of slides.",
    subtitle: "Introducing MURAL-Presenter: dependency-aligned lifecycle authoring for long-horizon presentations",
    dek:
      "Drafting slides is no longer the only hard part. The systems problem is deciding where long-lived state should live, who owns cross-page relations, and how to revise one region without destabilizing the deck.",
    readTime: "Project overview · 10 min read",
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
    languageHref: "/",
    eyebrow: "MURAL Presenter · 长程创作系统",
    title: "演示文稿不是一摞页面。",
    subtitle: "介绍 MURAL-Presenter：面向长程演示文稿的依赖对齐生命周期创作框架",
    dek:
      "写出单页已经不是唯一难点。真正的系统问题，是长期状态应该放在哪里、跨页关系由谁共同负责，以及如何修改一个区域而不破坏整册。",
    readTime: "项目全览 · 约 10 分钟",
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
  const homeHref = "#top";
  const paperHref = isZh ? "/zh/paper" : "/paper";
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
  const horizonCards = isZh
    ? [
        ["01", "跨阶段", "受众、证据与设计方向从研究进入规划和制作，不应由下游重新猜测。"],
        ["02", "跨页面", "定义、叙事承诺和视觉编码由关联页面共同保持，即使它们相距很远。"],
        ["03", "跨修改", "后续请求更新所有受影响关系，同时尽量保持无关页面不变。"],
      ] as const
    : [
        ["01", "Across stages", "Audience, evidence, and art direction move from research into planning and production without being re-inferred."],
        ["02", "Across slides", "Definitions, narrative promises, and visual encodings remain shared even when related pages are far apart."],
        ["03", "Across revisions", "Later requests update every affected relation while preserving work outside the impact boundary."],
      ] as const;
  const evaluationCards = isZh
    ? [
        ["01", "长程关系是否闭合", "THREAD-Bench 记录一项要求在哪里建立、被哪些远处页面消费，以及最终是否可观察地闭合。"],
        ["02", "单页与整册是否成立", "PresentBench 与 SlidesGen-Bench 补充内容、布局、视觉质量和通用生成能力。"],
        ["03", "修改之后是否仍然成立", "DECKBench 同时检查修改成功、未涉及页面保持、跨页回归错误与重执行范围。"],
      ] as const
    : [
        ["01", "Do long-range relations close?", "THREAD-Bench records where a requirement is established, which distant pages consume it, and whether closure is observable."],
        ["02", "Do pages and decks work?", "PresentBench and SlidesGen-Bench complement the study with content, layout, visual quality, and general generation measures."],
        ["03", "Does the deck survive revision?", "DECKBench measures edit success, preservation of untouched pages, cross-slide regressions, and replay scope together."],
      ] as const;

  return (
    <main className="blog-page" lang={isZh ? "zh-CN" : "en"} id="top">
      <ProjectNav
        language={language}
        homeHref={homeHref}
        homeLabel={t.homeLabel}
        languageHref={t.languageHref}
        languageLabel={t.languageLabel}
        paperHref={paperHref}
      />

      <article className="mural-editorial">
        <header className="mural-wall-hero">
          <div className="mural-wall-stage">
            <img
              className="mural-wall-image"
              src="/mural-wall-hero-v2.webp"
              alt={isZh
                ? "多位 MURAL Agent 在一面连续壁画上共同完成研究、规划、分组创作、复审与修改"
                : "MURAL agents collaboratively painting research, planning, grouped authoring, review, and revision across one continuous wall"}
            />
            <MuralBrush language={language} />
            <div className="mural-wall-heading section-shell">
              <div className="mural-wall-overline">
                <span>{t.eyebrow}</span>
                <b>ONE WALL · MANY HANDS</b>
              </div>
              <div className="mural-wall-name" aria-label="MURAL Presenter">
                <strong>MURAL</strong>
                <span>PRESENTER</span>
              </div>
              <h1 aria-label={t.title}>
                <span>{isZh ? "演示文稿不是" : "A presentation is not"}</span>
                <em>{isZh ? "一摞页面。" : "a stack of slides."}</em>
              </h1>
              <p className="mural-wall-subtitle">{t.subtitle}</p>
              <p className="mural-wall-dek">{t.dek}</p>
              <div className="mural-wall-actions" aria-label={isZh ? "项目资源" : "Project resources"}>
                <a className="is-primary" href="https://github.com/G1nkgo7/MURAL-Presenter" target="_blank" rel="noreferrer">GitHub <span>↗</span></a>
                <a href={paperHref}>Paper <span>→</span></a>
                <a href="#summary-title">{isZh ? "了解方法" : "Explore the method"} <span>↓</span></a>
              </div>
            </div>
            <HeroExplorer language={language} />
            <div className="mural-wall-signature" aria-hidden="true">
              <span>MATERIAL</span><i>→</i><span>PLAN</span><i>→</i><span>GROUP</span><i>→</i><span>REVIEW</span><i>→</i><span>REVISE</span>
            </div>
          </div>
          <div className="mural-wall-key" aria-label="Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations">
            <div className="mural-wall-key-inner section-shell">
              <div className="mural-wall-key-intro">
                <span>{isZh ? "名字即方法" : "THE NAME IS THE METHOD"}</span>
                <strong>{isZh ? "五个字母，一套完整创作系统" : "Five letters. One authoring system."}</strong>
              </div>
              {acronym.map(([initial, term, meaning]) => (
                <span
                  className={initial === "R" ? "is-revision" : ""}
                  aria-label={`${initial}, ${term}: ${meaning}`}
                  key={initial}
                >
                  <b>{initial}</b>
                  <span><strong>{term}</strong><small>{meaning}</small></span>
                </span>
              ))}
            </div>
          </div>
        </header>

        <section className="editorial-intro section-shell" id="overview" aria-labelledby="summary-title">
          <div className="editorial-intro-main">
            <span className="section-label">01 · {isZh ? "长程问题" : "THE LONG-HORIZON PROBLEM"}</span>
            <h2 id="summary-title">{isZh ? "长程，不只是页数。" : "Long-horizon is more than slide count."}</h2>
          </div>
          <p className="editorial-intro-lede">{isZh
            ? "一项决定需要跨过研究、规划、远距离页面和后续修改，仍然保持有效。MURAL 关注的不只是把每张页面单独做好，而是让整册的事实口径、叙事承诺和设计语言持续成立。"
            : "Audience, factual framing, narrative setups, visual encodings, and user requirements must remain valid across research, planning, distant slides, and later edits. MURAL is about keeping deck-level decisions intact—not merely polishing pages in isolation."}</p>
          <div className="decision-ribbon">
            {horizonCards.map(([index, title, description]) => (
              <article key={index}><span>{index}</span><h3>{title}</h3><p>{description}</p></article>
            ))}
          </div>
        </section>

        <section className="ownership-exhibit" id="task" aria-labelledby="ownership-title">
          <div className="section-shell ownership-exhibit-copy">
            <span className="section-label is-light">02 · {isZh ? "执行粒度" : "EXECUTION GRANULARITY"}</span>
            <h2 id="ownership-title">{isZh ? "整册和单页之间，需要一个共同负责者。" : "A deck needs an accountable unit between the deck and the slide."}</h2>
            <p>{isZh
              ? "MURAL 不是把一个 workflow 随意拆成更多 Agent。只有一组页面共享叙事职责、设计语言或显式依赖，并且需要被联合制作、检查与重做时，slide group 才成为新的执行边界。"
              : "MURAL does not split a workflow into Agents by default. A slide group becomes an execution boundary only when related pages share narrative responsibility, design language, or explicit dependencies that should be produced, inspected, and replayed together."}</p>
          </div>
          <div className="section-shell" id="showcase">
            <TopologyExplorer language={language} />
          </div>
        </section>

        <section className="method-exhibit section-shell" id="method" aria-labelledby="method-title">
          <div className="method-exhibit-head">
            <div><span className="section-label">03 · {isZh ? "创作生命周期" : "AUTHORING LIFECYCLE"}</span><h2 id="method-title">{isZh ? "把完整创作过程，变成可继续执行的 Skill。" : "Turn the authoring lifecycle into a resumable Skill."}</h2></div>
            <p>{isZh
              ? "Skill 定义角色触发、共享状态、落盘产物、质量门与继续执行路径。确定性动作保留为工具；只有边界明确、产物可验收、隔离或重放有价值的创作责任，才获得新的 Agent 上下文。"
              : "The Skill defines role triggers, shared state, persisted artifacts, quality gates, and continuation paths. Deterministic operations remain tools; a fresh Agent context is reserved for bounded creative ownership with checkable output and meaningful isolation or replay."}</p>
          </div>
          <LifecycleExplorer language={language} />
        </section>

        <section className="revision-exhibit" id="revision" aria-labelledby="revision-title">
          <div className="section-shell revision-exhibit-head">
            <div className="revision-exhibit-copy">
              <span className="section-label is-light">04 · {isZh ? "影响范围路由" : "IMPACT-SCOPED REVISION"}</span>
              <h2 id="revision-title">{isZh ? "修改，也要保持整册决定。" : "Revision must preserve deck-level decisions."}</h2>
              <p>{isZh
                ? "一次请求可能只改变一个元素，也可能改写一组页面的共同关系，甚至影响整册结构。MURAL 选择能够可靠保持全局决策的最小重执行范围；当边界不确定时，router 会向上升级。"
                : "A request may affect one element, a relation shared by several slides, or the structure of the deck. MURAL selects the smallest replay scope that can reliably preserve global decisions—and escalates when the impact boundary is uncertain."}</p>
            </div>
          </div>
          <div className="section-shell"><RevisionRouter language={language} /></div>
        </section>

        <section className="evaluation-exhibit section-shell" id="benchmark" aria-labelledby="benchmark-title">
          <div className="evaluation-exhibit-head">
            <span className="section-label">05 · {isZh ? "评测与发布" : "EVALUATION & RELEASE"}</span>
            <h2 id="benchmark-title">{isZh ? "单页好看，不代表整册成立。" : "A beautiful page does not prove the deck works."}</h2>
            <p>{isZh
              ? "正式实验将在相同工具、输入、backbone 与预算下比较不同执行拓扑，并验证每条 Agent 边界是否真的带来状态隔离、共同责任或可靠重放。"
              : "Formal experiments will compare execution topologies under matched tools, inputs, backbone, and budget, then test whether each Agent boundary truly earns its cost through state isolation, shared ownership, or reliable replay."}</p>
          </div>
          <div className="evaluation-ledger">
            {evaluationCards.map(([index, title, description]) => (
              <article key={index}><span>{index}</span><h3>{title}</h3><p>{description}</p></article>
            ))}
          </div>
          <div className="research-note"><span>RESEARCH PREVIEW</span><p>{isZh ? "评测协议与实验矩阵已经规划；正式分数、canonical cases 与统计结论仍待冻结，当前页面不作效果声明。" : "The protocol and experiment matrix are planned. Formal scores, canonical cases, and statistical conclusions remain pending; this preview makes no effectiveness claim."}</p></div>
          <div className="release-inline" id="release">
            <div><span>OPEN RELEASE</span><p>{isZh ? "当前开放项目页、方法图、论文工作稿与仓库骨架；训练、推理、QC、THREAD-Bench 与正式结果将在冻结后逐步发布。" : "The project page, method figures, working manuscript, and repository scaffold are open now. Training, inference, QC, THREAD-Bench, and formal results will follow their freezes."}</p></div>
            <nav aria-label={isZh ? "项目资源" : "Project resources"}><a href="https://github.com/G1nkgo7/MURAL-Presenter" target="_blank" rel="noreferrer">GitHub ↗</a><a href={paperHref}>{isZh ? "论文工作稿" : "Working paper"} →</a></nav>
          </div>
        </section>
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
