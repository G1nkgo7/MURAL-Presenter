type Language = "en" | "zh";

const copy = {
  en: {
    languageName: "中文",
    languageHref: "/zh",
    nav: [
      ["Why MURAL", "#why"],
      ["Lifecycle", "#lifecycle"],
      ["Revision", "#revision"],
      ["THREAD-Bench", "#benchmark"],
      ["Blog", "/blog"],
    ],
    badge: "MURAL Presenter · Research preview",
    heroTitleA: "A presentation is not",
    heroTitleB: "a stack of slides.",
    heroBody:
      "MURAL does more than split a workflow. It aligns agent ownership with deck dependencies, then carries shared decisions through research, grouped authoring, rendered review, and later human edits.",
    primaryCta: "Explore the lifecycle",
    secondaryCta: "Read the launch article",
    secondaryCtaHref: "/blog",
    markLabel: "Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations",
    artNote: "Three collaborators. One shared deck state.",
    horizonKicker: "The actual horizon",
    horizonTitle: "Decisions must survive in three directions.",
    horizonIntro:
      "Slide count is only the visible surface. The harder question is whether a decision remains valid when another role, a distant slide, or a later edit needs it.",
    horizonCards: [
      ["Across stages", "Audience, evidence, and art direction move from planning into production without being re-inferred."],
      ["Across slides", "Definitions, narrative setups, and visual encodings may close many pages after they are introduced."],
      ["Across revisions", "A later change should update every affected relation while preserving unrelated work."],
    ],
    topologyKicker: "The missing middle",
    topologyTitle: "Put responsibility where the relationship lives.",
    topologyBody:
      "A single trajectory retains one owner but keeps accumulating heterogeneous history. Per-slide workers are easy to parallelize, yet related pages live in separate contexts. MURAL groups narratively or visually dependent slides under one accountable Group Agent, then restores a deck-wide decision after assembly.",
    topologyCaption:
      "The difference is not whether pages are assembled. It is where related slides are jointly authored and where deck-level validation returns.",
    lifecycleKicker: "An executable authoring procedure",
    lifecycleTitle: "From a user brief to an editable deck — and back into revision.",
    lifecycleBody:
      "The MURAL Authoring Skill defines roles, persisted artifacts, quality gates, and continuation paths. Global choices become a shared deck blueprint; complete slide groups author and inspect related pages; whole-deck Review checks the relations no group can see alone.",
    lifecycleCaption:
      "One centralized image stage resolves reusable assets before parallel groups begin. Each group closes its own render–inspect–revise loop.",
    phases: [
      ["01", "Material & research", "Organize optional attachments and close only the evidence gaps that can change the deck."],
      ["02", "Plan & compile", "Fix audience, narrative, design language, page map, and complete non-overlapping production groups."],
      ["03", "Group authoring", "Jointly write and inspect related slides while independent groups run in parallel."],
      ["04", "Review & deliver", "Assemble the deck, restore whole-deck review, and export HTML, PPTX, PDF, and images."],
    ],
    revisionKicker: "Revision-aware by construction",
    revisionTitle: "Choose the smallest scope that can preserve the deck.",
    revisionBody:
      "Generation does not end the authoring lifecycle. Review findings and later human requests enter the same router. When impact is uncertain, MURAL escalates instead of pretending a local patch is safe.",
    revisionCards: [
      ["Page patch", "A precisely local request changes one page and rerenders the affected pixels."],
      ["Group replay", "A cross-slide relation or shared visual language reactivates the complete affected group."],
      ["Deck replan", "Sections, title structure, slide count, or shared systems trigger affected-structure replanning."],
    ],
    htmlKicker: "Structured source, rendered truth",
    htmlTitle: "HTML connects generation to editing.",
    htmlBody:
      "HTML, CSS, SVG, text, and media remain separately addressable, while browser pixels become the ground truth for inspection. PPTX, PDF, and images are delivery adapters whose fidelity is measured rather than assumed.",
    benchmarkKicker: "Evaluation",
    benchmarkTitle: "THREAD-Bench follows requirements end to end.",
    benchmarkBody:
      "The planned 50-case benchmark separates process evidence, knowledge checks, deck-level requirements, page-level quality, and case-specific long-range probes. Every probe records where a decision is established, which distant targets depend on it, and what observable predicate closes the relation.",
    benchmarkName: "Tracking Holistic Requirements and End-to-End Alignment in Decks",
    statusKicker: "Release boundary",
    statusTitle: "The mechanisms are implemented. The measured claims remain open.",
    statusBody:
      "This preview publishes the narrative, figures, brand system, bilingual documentation, and launch article. The code, Skill, training trajectories, canonical benchmark cases, checkpoints, licenses, and formal results follow after clean version freezes and reproducibility review.",
    available: "Available now",
    pending: "Pending release gates",
    availableItems: ["Brand system", "Method figures", "Bilingual documentation", "Launch article"],
    pendingItems: ["Frozen implementation", "THREAD-Bench cases", "Models and data", "Measured results"],
    closing: "A presentation is a designed argument that persists through evidence, planning, production, review, and change.",
    footer: "MURAL research preview · No public effectiveness claim yet",
  },
  zh: {
    languageName: "English",
    languageHref: "/",
    nav: [
      ["为什么是 MURAL", "#why"],
      ["完整生命周期", "#lifecycle"],
      ["修改路由", "#revision"],
      ["THREAD-Bench", "#benchmark"],
      ["宣传文章", "/zh/blog"],
    ],
    badge: "MURAL Presenter · Research preview",
    heroTitleA: "演示文稿不是",
    heroTitleB: "一摞页面。",
    heroBody:
      "MURAL 不只是把工作流拆成子任务，而是让 Agent 的责任边界匹配整册依赖结构，并让共享决策贯穿资料接地、分组制作、渲染复审和后续人工修改。",
    primaryCta: "查看完整生命周期",
    secondaryCta: "阅读宣传文章",
    secondaryCtaHref: "/zh/blog",
    markLabel: "Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations",
    artNote: "三个协作者，一份持续共享的整册状态。",
    horizonKicker: "真正的 long horizon",
    horizonTitle: "一项决策需要沿三个方向持续有效。",
    horizonIntro:
      "页数只是表面。更难的问题是，当另一个角色、远处页面或后续修改需要一项决策时，它是否仍然准确、可用。",
    horizonCards: [
      ["跨阶段", "受众、证据与 art direction 从规划进入制作，不应由下游重新猜测。"],
      ["跨页面", "定义、叙事伏笔和视觉编码，可能隔了许多页才真正完成闭合。"],
      ["跨修改", "后续请求需要更新所有受影响关系，同时尽量保持无关页面不变。"],
    ],
    topologyKicker: "整册与单页之间",
    topologyTitle: "让责任落在跨页关系真正发生的位置。",
    topologyBody:
      "单条轨迹只有一个负责者，却会持续累积异构执行历史；逐页 Agent 易于并行，但关联页面处于不同上下文。MURAL 把具有叙事或视觉依赖的页面交给同一个 Group Agent，并在页面汇合后恢复整册判断。",
    topologyCaption:
      "区别不在于是否需要 assembly，而在于关联页面在哪里被共同制作，以及整册验证在哪里重新出现。",
    lifecycleKicker: "可执行的创作程序",
    lifecycleTitle: "从用户需求到可编辑整册，再回到持续修改。",
    lifecycleBody:
      "MURAL Authoring Skill 定义角色、落盘状态、质量门与继续执行路径。全局决策成为 shared deck blueprint；完整页面组联合制作和检查关联页面；Whole-deck Review 负责单组无法独立判断的关系。",
    lifecycleCaption:
      "统一 Image 阶段会在并行组开工前解析全册素材；每个页面组都要关闭自己的 render–inspect–revise 循环。",
    phases: [
      ["01", "Material & research", "整理可选附件，只补齐真正会改变整册结论的证据缺口。"],
      ["02", "Plan & compile", "锁定受众、叙事、设计语言、页面地图和完整且不重叠的 production groups。"],
      ["03", "Group authoring", "同一个 Group Agent 联合制作和检查关联页面，不同页面组保持并行。"],
      ["04", "Review & deliver", "装配整册，恢复 whole-deck Review，并导出 HTML、PPTX、PDF 与图片。"],
    ],
    revisionKicker: "修改感知不是附加功能",
    revisionTitle: "选择能够可靠保持整册决策的最小范围。",
    revisionBody:
      "生成结束不代表 authoring 结束。Review 问题和后续人工请求进入同一个 revision router；影响边界不确定时，系统会向上升级，而不是假设局部 patch 一定安全。",
    revisionCards: [
      ["Page patch", "可以精确定位的局部请求只修改单页，并重新检查变化像素。"],
      ["Group replay", "跨页关系或共同视觉语言变化时，重新激活完整受影响页面组。"],
      ["Deck replan", "章节、标题结构、页数或共享系统改变时，才重规划受影响结构。"],
    ],
    htmlKicker: "结构化源码，渲染像素为准",
    htmlTitle: "HTML 把生成和修改连接起来。",
    htmlBody:
      "HTML、CSS、SVG、文字与媒体保持独立可寻址，浏览器像素则成为视觉检查的依据。PPTX、PDF 和图片是 delivery adapters，其保真度需要测量，而不是默认等价。",
    benchmarkKicker: "评测",
    benchmarkTitle: "THREAD-Bench 追踪一项要求如何贯穿整条链路。",
    benchmarkBody:
      "计划中的 50 个 case 会拆分过程证据、知识检查、整册要求、单页质量和 case-specific 长程探针。每个探针记录决策在哪里建立，哪些远处目标依赖它，以及用什么可观察条件判定关系闭合。",
    benchmarkName: "Tracking Holistic Requirements and End-to-End Alignment in Decks",
    statusKicker: "公开边界",
    statusTitle: "机制已经实现，效果结论仍等待正式实验。",
    statusBody:
      "当前 preview 公开项目叙事、系统图、品牌系统、中英文文档和宣传文章。代码、Skill、训练轨迹、canonical benchmark cases、模型权重、许可证与正式结果，会在版本冻结和复现审查后发布。",
    available: "现在已经提供",
    pending: "等待发布门槛",
    availableItems: ["品牌系统", "方法配图", "中英文文档", "宣传文章"],
    pendingItems: ["冻结实现", "THREAD-Bench cases", "模型与数据", "正式结果"],
    closing: "演示文稿是一段贯穿证据、规划、制作、复审与修改的设计论证。",
    footer: "MURAL research preview · 暂无公开效果结论",
  },
} as const;

export function AcronymExpansion({ label, language }: { label: string; language: Language }) {
  const terms = language === "zh"
    ? [
        ["M", "Multi-Agent", "多角色并行", ""],
        ["U", "Unified", "共享整册状态", ""],
        ["R", "Revision-Aware", "按影响范围续作", "acronym-revision"],
        ["A", "Authoring", "贯穿创作生命周期", ""],
        ["L", "Long-Horizon Presentations", "跨阶段、页面与修改", ""],
      ] as const
    : [
        ["M", "Multi-Agent", "parallel specialists", ""],
        ["U", "Unified", "one shared deck state", ""],
        ["R", "Revision-Aware", "resume by impact", "acronym-revision"],
        ["A", "Authoring", "an executable lifecycle", ""],
        ["L", "Long-Horizon Presentations", "decisions persist", ""],
      ] as const;

  return (
    <div className="acronym-expansion" aria-label={label}>
      <div className="acronym-intro">
        <span>THE NAME IS<br />THE METHOD</span>
        <small>{language === "zh" ? "五个字母 · 一套方法" : "05 LETTERS · 01 SYSTEM"}</small>
      </div>
      <div className="acronym-row">
        {terms.map(([initial, term, meaning, modifier], index) => (
          <span className={`acronym-unit ${modifier}`} key={initial}>
            <span className="acronym-index">0{index + 1}</span>
            <span className="acronym-term">
              <b>{initial}</b><strong>{term}</strong>
              <small>{meaning}</small>
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}

export function BrandLockup() {
  return (
    <span className="brand-lockup" aria-label="MURAL Presenter">
      <span className="brand-name">MURAL</span>
      <span className="brand-divider" aria-hidden="true" />
      <span className="brand-suffix">PRESENTER</span>
    </span>
  );
}

export function MuralSite({ language }: { language: Language }) {
  const t = copy[language];
  const isZh = language === "zh";
  const paperHref = isZh ? "/zh/paper" : "/paper";

  return (
    <main lang={isZh ? "zh-CN" : "en"}>
      <header className="nav-shell">
        <a className="brand" href="#top" aria-label="MURAL home">
          <BrandLockup />
        </a>
        <nav aria-label={isZh ? "页面导航" : "Page navigation"}>
          {t.nav.map(([label, href]) => (
            <a href={href} key={href}>{label}</a>
          ))}
        </nav>
        <a className="language-link" href={t.languageHref}>{t.languageName}</a>
      </header>

      <section className="hero section-shell" id="top">
        <AcronymExpansion label={t.markLabel} language={language} />
        <div className="hero-copy">
          <div className="status-badge"><span />{t.badge}</div>
          <div className="hero-project-word" aria-label="MURAL Presenter">
            <strong>MURAL</strong><span>PRESENTER</span>
          </div>
          <h1>{t.heroTitleA}<br /><em>{t.heroTitleB}</em></h1>
          <p>{t.heroBody}</p>
          <div className="hero-actions">
            <a className="button button-primary" href="https://github.com/G1nkgo7/MURAL-Presenter" target="_blank" rel="noreferrer">GitHub<span>↗</span></a>
            <a className="button button-secondary" href={t.secondaryCtaHref}>{t.secondaryCta}<span>→</span></a>
          </div>
        </div>
        <div className="hero-art" aria-label={isZh ? "MURAL 多智能体壁画师团队" : "MURAL multi-agent muralist team"}>
          <div className="hero-stage-label"><span>MURAL CREW</span><b>03 AGENTS · 01 DECK</b></div>
          <img src="/mural-mascot.png" alt={isZh ? "三个机器人共同为 M 形页面墙涂色" : "Three robots jointly painting an M-shaped wall of presentation pages"} />
          <div className="hero-stage-rail" aria-hidden="true">
            <span>PLAN</span><i>→</i><span>GROUP</span><i>→</i><span>REVIEW</span><i>→</i><span>REVISE</span>
          </div>
          <div className="art-caption"><span>01 → N</span>{t.artNote}</div>
        </div>
        <div className="resource-shelf" aria-label={isZh ? "项目资源" : "Project resources"}>
          <a className="resource-link resource-github" href="https://github.com/G1nkgo7/MURAL-Presenter" target="_blank" rel="noreferrer">
            <span className="resource-index">01</span><strong>GitHub</strong><small>{isZh ? "代码、更新与发布" : "Code, updates & releases"}</small><i>↗</i>
          </a>
          <a className="resource-link resource-paper" href={paperHref}>
            <span className="resource-index">02</span><strong>Paper</strong><small>{isZh ? "本地论文预览" : "Local manuscript preview"}</small><i>→</i>
          </a>
          <a className="resource-link" href={t.secondaryCtaHref}>
            <span className="resource-index">03</span><strong>{isZh ? "项目文章" : "Project story"}</strong><small>{isZh ? "完整动机与设计思路" : "Motivation & design rationale"}</small><i>→</i>
          </a>
          <a className="resource-link resource-benchmark" href="#benchmark">
            <span className="resource-index">04</span><strong>THREAD-Bench</strong><small>{isZh ? "长程一致性评测预览" : "Long-horizon evaluation preview"}</small><i>↓</i>
          </a>
        </div>
      </section>

      <section className="horizon section-shell" id="why">
        <div className="section-heading">
          <span className="kicker">{t.horizonKicker}</span>
          <h2>{t.horizonTitle}</h2>
          <p>{t.horizonIntro}</p>
        </div>
        <div className="horizon-grid">
          {t.horizonCards.map(([title, body], index) => (
            <article className="horizon-card" key={title}>
              <span className="index">0{index + 1}</span>
              <h3>{title}</h3>
              <p>{body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="figure-section section-shell">
        <div className="split-heading">
          <div><span className="kicker">{t.topologyKicker}</span><h2>{t.topologyTitle}</h2></div>
          <p>{t.topologyBody}</p>
        </div>
        <figure className="paper-figure">
          <div className="figure-bar"><span>FIGURE 01</span><strong>{isZh ? "责任拓扑" : "Responsibility topology"}</strong></div>
          <img src="/execution-topologies.png" alt={isZh ? "顺序式、完整上下文并行和 MURAL 页面组拓扑" : "Sequential, full-context parallel, and MURAL slide-group topologies"} />
          <figcaption>{t.topologyCaption}</figcaption>
        </figure>
      </section>

      <section className="lifecycle section-shell" id="lifecycle">
        <div className="split-heading">
          <div><span className="kicker">{t.lifecycleKicker}</span><h2>{t.lifecycleTitle}</h2></div>
          <p>{t.lifecycleBody}</p>
        </div>
        <div className="phase-grid">
          {t.phases.map(([number, title, body]) => (
            <article className="phase-card" key={number}>
              <span>{number}</span><h3>{title}</h3><p>{body}</p>
            </article>
          ))}
        </div>
        <figure className="paper-figure lifecycle-figure">
          <div className="figure-bar"><span>FIGURE 02</span><strong>{isZh ? "完整创作与修改生命周期" : "Full authoring and revision lifecycle"}</strong></div>
          <img src="/authoring-lifecycle.png" alt={isZh ? "MURAL 完整创作与修改生命周期" : "MURAL full authoring and revision lifecycle"} />
          <figcaption>{t.lifecycleCaption}</figcaption>
        </figure>
      </section>

      <section className="revision section-shell" id="revision">
        <div className="revision-intro">
          <span className="kicker kicker-light">{t.revisionKicker}</span>
          <h2>{t.revisionTitle}</h2>
          <p>{t.revisionBody}</p>
        </div>
        <div className="revision-grid">
          {t.revisionCards.map(([title, body], index) => (
            <article key={title}><span>0{index + 1}</span><h3>{title}</h3><p>{body}</p></article>
          ))}
        </div>
      </section>

      <section className="html-section section-shell">
        <div className="html-copy">
          <span className="kicker">{t.htmlKicker}</span>
          <h2>{t.htmlTitle}</h2>
          <p>{t.htmlBody}</p>
        </div>
        <div className="code-window" aria-hidden="true">
          <div className="window-bar"><i /><i /><i /><span>present.html</span></div>
          <div className="code-line"><b>01</b><span className="code-tag">&lt;section</span> class=<span className="code-string">&quot;slide&quot;</span><span className="code-tag">&gt;</span></div>
          <div className="code-line"><b>02</b><span className="code-muted">  &lt;!-- addressable source --&gt;</span></div>
          <div className="code-line"><b>03</b><span className="code-tag">  &lt;h1&gt;</span>One decision, many pages.<span className="code-tag">&lt;/h1&gt;</span></div>
          <div className="code-line"><b>04</b><span className="code-tag">&lt;/section&gt;</span></div>
          <div className="render-chip"><span /> render → inspect → revise</div>
        </div>
      </section>

      <section className="benchmark section-shell" id="benchmark">
        <div className="thread-mark" aria-hidden="true"><span /><span /><span /><span /><span /></div>
        <div className="benchmark-copy">
          <span className="kicker">{t.benchmarkKicker}</span>
          <h2>{t.benchmarkTitle}</h2>
          <p>{t.benchmarkBody}</p>
          <div className="benchmark-name"><strong>THREAD-Bench</strong><span>{t.benchmarkName}</span></div>
        </div>
      </section>

      <section className="release section-shell" id="release">
        <div className="release-copy">
          <span className="kicker kicker-light">{t.statusKicker}</span>
          <h2>{t.statusTitle}</h2>
          <p>{t.statusBody}</p>
        </div>
        <div className="release-columns">
          <div><h3>{t.available}</h3>{t.availableItems.map(item => <span key={item}><i>✓</i>{item}</span>)}</div>
          <div><h3>{t.pending}</h3>{t.pendingItems.map(item => <span key={item}><i>○</i>{item}</span>)}</div>
        </div>
      </section>

      <section className="closing section-shell">
        <div className="closing-brand"><BrandLockup /></div>
        <blockquote>{t.closing}</blockquote>
      </section>

      <footer className="section-shell">
        <div className="brand footer-brand"><BrandLockup /></div>
        <p>{t.footer}</p>
        <a href="https://github.com/G1nkgo7/MURAL-Presenter" target="_blank" rel="noreferrer">GitHub ↗</a>
        <a href={isZh ? "/zh/blog" : "/blog"}>{isZh ? "宣传文章" : "Launch article"}</a>
        <a href="#top">↑ {isZh ? "返回顶部" : "Back to top"}</a>
      </footer>
    </main>
  );
}
