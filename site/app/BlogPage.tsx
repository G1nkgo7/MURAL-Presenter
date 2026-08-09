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
    subtitle: "Introducing MURAL-Presenter: lifecycle authoring for long-horizon presentations",
    dek:
      "Presentation agents can already draft outlines, retrieve evidence, write slides, and respond to local edits. The harder problem is carrying one set of decisions through the complete deck—and through the edits that follow.",
    readTime: "Launch article · 9 min read",
    brandTagline: "One shared deck state, carried from the first brief through every later revision.",
    contents: "In this article",
    closing:
      "MURAL starts from a simple premise: a presentation is a designed argument that persists through evidence, planning, production, review, and change. The system should be organized around that lifecycle too.",
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
          "One Agent can carry a deck from beginning to end, retaining one owner and a continuous context. Its trajectory, however, accumulates research notes, planning decisions, tool observations, slide code, render feedback, and later edits. Per-slide Agents shorten individual trajectories and expose parallelism, but related pages now live in separate execution contexts.",
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
          "The MURAL Authoring Skill defines stages, roles, persisted artifacts, quality gates, and continuation paths. Optional attachments become a location-grounded material brief. Research is invoked only when an unresolved fact or term can change the deck’s conclusion.",
          "The Orchestrator then fixes the audience, communicative goal, narrative arc, terminology, visual direction, page map, asset strategy, and production groups. These decisions are externalized as a shared deck blueprint and compiled into group and page briefs. Downstream Agents receive the state relevant to their responsibility instead of reconstructing the talk from a growing dialogue.",
          "A centralized image stage resolves reusable assets before grouped authoring begins. Each Group Agent closes a render–inspect–revise loop on its own pages. Deterministic assembly then creates the authoritative deck and whole-deck contact sheet; Review checks numerical conventions, terminology, setup and response, visual semantics, section rhythm, special pages, and speaker-note alignment.",
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
          "The current release is a research preview. It publishes the project narrative, system figures, brand system, bilingual documentation, and this article. Formal results, canonical benchmark cases, training artifacts, and frozen implementation releases will follow reproducibility and redistribution review.",
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
    subtitle: "介绍 MURAL-Presenter：面向长程演示文稿完整生命周期的创作框架",
    dek:
      "演示文稿 Agent 已经能够列提纲、查资料、写单页和响应局部修改。更难的问题，是让同一组事实、叙事和设计决策贯穿整册，并在后续修改中继续有效。",
    readTime: "发布文章 · 约 9 分钟",
    brandTagline: "让一份共享的整册状态，从最初需求一直延续到后续每一轮修改。",
    contents: "本文内容",
    closing:
      "MURAL 的出发点并不复杂：演示文稿不是一摞分别合格的页面，而是一段贯穿证据、规划、制作、复审与修改的设计论证。系统本身也应该围绕这条生命周期组织。",
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
          "单 Agent 可以从头到尾负责整册，保留连续上下文，但它的轨迹会不断累积研究材料、规划决策、工具反馈、页面代码、渲染检查和后续修改。逐页 Agent 缩短了单条轨迹，也便于并行，却让关联页面落入彼此独立的执行上下文。",
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
          "MURAL Authoring Skill 定义阶段、角色、落盘产物、质量门和继续执行路径。可选附件先被整理成带位置依据的 material brief；只有未解决事实或术语会改变整册结论时，系统才启动 Research。",
          "Orchestrator 随后确定受众、沟通目标、叙事弧、术语、视觉方向、页面地图、素材策略和 production groups。这些决定被外置为 shared deck blueprint，再编译成 group/page briefs。下游 Agent 只接收与自身职责相关的状态，不必从持续增长的对话中重新猜整场演讲。",
          "统一 Image 阶段会在页面组开工前解析可复用素材。每个 Group Agent 关闭自己的 render–inspect–revise 循环；确定性 assembly 生成权威整册和 contact sheet；Whole-deck Review 再检查数值口径、术语、setup/response、视觉语义、章节节奏、特殊页面和讲稿配合。",
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
          "当前公开的是 research preview：项目叙事、系统配图、品牌系统、中英文文档和本文已经发布；正式结果、canonical benchmark cases、训练产物和冻结实现将在完成复现与再分发审查后陆续开放。",
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
        <header className="blog-hero section-shell">
          <div className="blog-hero-copy">
            <div className="blog-hero-title">
              <span className="kicker">{t.eyebrow}</span>
              <h1>{t.title}</h1>
            </div>
            <div className="blog-hero-intro">
              <p className="blog-subtitle">{t.subtitle}</p>
              <p className="blog-dek">{t.dek}</p>
              <div className="blog-meta">{t.readTime}</div>
              <div className="blog-project-actions" aria-label={isZh ? "项目资源" : "Project resources"}>
                <a href="https://github.com/G1nkgo7/MURAL-Presenter" target="_blank" rel="noreferrer">GitHub <span>↗</span></a>
                <a href={paperHref}>Paper <small>{isZh ? "本地预览" : "LOCAL PREVIEW"}</small></a>
                <a href={homeHref}>{isZh ? "项目主页" : "Project page"} <span>→</span></a>
              </div>
            </div>
          </div>
          <div className="blog-brand-hero">
            <div className="blog-brand-art">
              <picture>
                <source media="(max-width: 680px)" srcSet="/mural-mascot.png" />
                <img
                  src="/mural-blog-hero-source.png"
                  alt={isZh ? "三个 MURAL Agent 共同绘制 M 形壁画" : "Three MURAL agents jointly painting an M-shaped mural"}
                />
              </picture>
            </div>
            <div className="blog-brand-copy">
              <div className="blog-brand-label"><span>RESEARCH PREVIEW</span><span>01 → N</span></div>
              <div className="blog-brand-word"><strong>MURAL</strong><em>PRESENTER</em></div>
              <div className="blog-brand-expansion">
                <div className="blog-brand-expansion-label">
                  <span>{isZh ? "MURAL 的五个字母分别代表" : "What MURAL stands for"}</span>
                  <i aria-hidden="true" />
                </div>
                <div className="blog-brand-legend" aria-label="Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations">
                  {acronym.map(([initial, term, meaning], index) => (
                    <span className={initial === "R" ? "is-revision" : ""} key={initial}>
                      <i>0{index + 1}</i>
                      <b>{initial}</b>
                      <strong>{term}</strong>
                      <small>{meaning}</small>
                    </span>
                  ))}
                </div>
              </div>
              <p>{t.brandTagline}</p>
            </div>
          </div>
        </header>

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
                {section.figure && (
                  <figure className="article-figure">
                    <div className="figure-bar">
                      <span>{section.id === "groups" ? "FIGURE 01" : "FIGURE 02"}</span>
                      <strong>{section.id === "groups"
                        ? (isZh ? "责任拓扑" : "Responsibility topology")
                        : (isZh ? "完整生命周期" : "Full lifecycle")}</strong>
                    </div>
                    <img src={section.figure.src} alt={section.figure.alt} />
                    <figcaption>{section.figure.caption}</figcaption>
                  </figure>
                )}
              </section>
            ))}

            <blockquote className="article-closing">{t.closing}</blockquote>
            <a
              className="button button-primary article-repo"
              href="https://github.com/G1nkgo7/MURAL-Presenter"
            >
              {t.repository}<span>↗</span>
            </a>
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
