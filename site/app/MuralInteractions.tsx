type Language = "en" | "zh";

const topologyCopy = {
  zh: [
    { id: "sequential", tab: "单 Agent", title: "一条轨迹保留连续上下文", body: "全局决定天然连贯，但研究、规划、页面代码、渲染反馈与修改历史会进入同一条不断增长的轨迹。", facts: [["上下文", "持续增长"], ["跨页责任", "隐式"], ["局部重做", "代价较高"]] },
    { id: "parallel", tab: "逐页 Agent", title: "页面容易并行，关系无人共同负责", body: "单页生产被隔离，却把术语、叙事伏笔和视觉编码等跨页决定留给多个上下文之间的交接与事后检查。", facts: [["上下文", "短而独立"], ["跨页责任", "分散"], ["并行", "页面级"]] },
    { id: "mural", tab: "MURAL Groups", title: "关联页面共享生产者与检查单元", body: "共享蓝图投影为页面组 brief；同一 Group Agent 联合生成和检查关联页面，装配后再由 whole-deck Review 闭合跨组关系。", facts: [["上下文", "按责任有界"], ["跨页责任", "显式"], ["重做", "按组恢复"]] },
  ],
  en: [
    { id: "sequential", tab: "Single Agent", title: "One trace preserves continuous context", body: "Global decisions remain naturally connected, while research, planning, slide code, render feedback, and revision history accumulate in one growing trajectory.", facts: [["Context", "Growing"], ["Cross-slide owner", "Implicit"], ["Local replay", "Expensive"]] },
    { id: "parallel", tab: "Per-slide Agents", title: "Slides parallelize; relations lose a joint owner", body: "Page production is isolated, but terminology, narrative setups, and visual encodings depend on handoffs and post-hoc checks across separate contexts.", facts: [["Context", "Short, separate"], ["Cross-slide owner", "Distributed"], ["Parallelism", "Per page"]] },
    { id: "mural", tab: "MURAL Groups", title: "Related slides share a producer and inspection unit", body: "The shared blueprint is projected into group briefs. One Group Agent jointly authors and checks related pages before whole-deck Review closes relations across groups.", facts: [["Context", "Bounded by role"], ["Cross-slide owner", "Explicit"], ["Replay", "By group"]] },
  ],
} as const;

const lifecycleCopy = {
  zh: [
    { id: "ground", tab: "01 接地", title: "Material & Research", body: "解析用户附件、整理已有素材，并只对关键证据缺口开展网络调研。", owner: "Orchestrator · Material · Research", input: "用户需求 · Attachments", output: "material.md · knowledge-brief.md" },
    { id: "plan", tab: "02 规划", title: "Plan & Compile", body: "固定受众、叙事弧、设计语言、页面地图、素材策略和互不重叠的 production groups。", owner: "Orchestrator", input: "知识简报 · 设计目标", output: "deck.md · base.css · group briefs" },
    { id: "author", tab: "03 制作", title: "Group Authoring", body: "Group Agents 并行生产关联页面，并在组内关闭 write–render–inspect–revise 循环。", owner: "Image Agents · Group Agents", input: "Group brief · Assets", output: "slide_NN.html · group contact sheet" },
    { id: "review", tab: "04 闭合", title: "Review & Deliver", body: "装配权威整册，检查跨组叙事与设计关系，再按影响范围修改并导出交付格式。", owner: "Whole-deck Review · Router", input: "Rendered deck · User edit", output: "HTML · PPTX · PDF · Images" },
  ],
  en: [
    { id: "ground", tab: "01 Ground", title: "Material & Research", body: "Parse supplied material, organize existing assets, and research only consequential evidence gaps.", owner: "Orchestrator · Material · Research", input: "User brief · Attachments", output: "material.md · knowledge-brief.md" },
    { id: "plan", tab: "02 Plan", title: "Plan & Compile", body: "Fix audience, narrative arc, design language, page map, asset strategy, and disjoint production groups.", owner: "Orchestrator", input: "Knowledge brief · Design intent", output: "deck.md · base.css · group briefs" },
    { id: "author", tab: "03 Author", title: "Group Authoring", body: "Group Agents produce related pages in parallel and close write–render–inspect–revise loops inside each group.", owner: "Image Agents · Group Agents", input: "Group brief · Assets", output: "slide_NN.html · group contact sheet" },
    { id: "review", tab: "04 Close", title: "Review & Deliver", body: "Assemble the authoritative deck, inspect cross-group relations, route revisions by impact, and export delivery formats.", owner: "Whole-deck Review · Router", input: "Rendered deck · User edit", output: "HTML · PPTX · PDF · Images" },
  ],
} as const;

const revisionCopy = {
  zh: [
    { id: "page", example: "把第 7 页标题改短，并把图片向左移动。", scope: "PAGE", title: "局部 Patch", body: "目标元素与影响边界都明确，只修改并重新渲染受影响页面。", restart: "返回 slide_07.html" },
    { id: "group", example: "统一市场章节的术语和蓝色编码。", scope: "GROUP", title: "重新激活页面组", body: "共享术语或视觉语言发生变化，让原 Group Agent 连同关联页面一起恢复工作。", restart: "返回 Group brief 与 slide stack" },
    { id: "deck", example: "加入竞争格局章节，并调整结论顺序。", scope: "DECK", title: "重规划受影响结构", body: "章节、页数或核心叙事改变，回到共享蓝图，再选择性重启下游阶段。", restart: "返回 shared deck blueprint" },
  ],
  en: [
    { id: "page", example: "Shorten slide 7's title and move its image left.", scope: "PAGE", title: "Local patch", body: "The target and impact boundary are precise, so only the affected page is edited and rerendered.", restart: "Return to slide_07.html" },
    { id: "group", example: "Unify terminology and blue encoding across the market section.", scope: "GROUP", title: "Reactivate the group", body: "Shared terminology or visual language changes, so the original Group Agent resumes with its related pages.", restart: "Return to the group brief and slide stack" },
    { id: "deck", example: "Add a competitive landscape section and reorder the conclusion.", scope: "DECK", title: "Replan affected structure", body: "Sections, page count, or the narrative changes, so work returns to the shared blueprint before selective downstream replay.", restart: "Return to the shared deck blueprint" },
  ],
} as const;

function FigureLightbox({ id, src, alt, closeHref, closeLabel }: { id: string; src: string; alt: string; closeHref: string; closeLabel: string }) {
  return (
    <div className="figure-lightbox" id={id} role="dialog" aria-modal="true" aria-label={alt}>
      <a className="figure-lightbox-backdrop" href={closeHref} aria-label={closeLabel} />
      <div className="figure-lightbox-panel">
        <a className="figure-lightbox-close" href={closeHref} aria-label={closeLabel}>×</a>
        <div className="figure-lightbox-scroll"><img src={src} alt={alt} /></div>
        <small>{closeLabel}</small>
      </div>
    </div>
  );
}

export function TopologyExplorer({ language }: { language: Language }) {
  const items = topologyCopy[language];
  const prefix = `topology-${language}`;
  return (
    <div className="topology-explorer">
      {items.map((item, index) => <input className={`explorer-control topology-control is-${item.id}`} type="radio" name={prefix} id={`${prefix}-${item.id}`} defaultChecked={index === 2} aria-label={item.tab} key={item.id} />)}
      <div className="explorer-tabs" aria-label={language === "zh" ? "选择执行拓扑" : "Choose an execution topology"}>
        {items.map((item, index) => <label data-mode={item.id} htmlFor={`${prefix}-${item.id}`} key={item.id}><span>0{index + 1}</span>{item.tab}</label>)}
      </div>
      <div className="topology-explorer-body">
        <a className="interactive-figure" href="#topology-figure" aria-label={language === "zh" ? "放大责任拓扑图" : "Enlarge responsibility topology"}>
          <img src="/execution-topologies.png" alt={language === "zh" ? "单 Agent、逐页 Agent 与 MURAL 页面组拓扑" : "Single-agent, per-slide-agent, and MURAL group topologies"} />
          <span>{language === "zh" ? "点击放大" : "Click to enlarge"} ↗</span>
        </a>
        <div className="explorer-details">
          {items.map((item) => <div className="explorer-detail" data-mode={item.id} key={item.id}><small>{item.tab}</small><h3>{item.title}</h3><p>{item.body}</p><dl>{item.facts.map(([term, value]) => <div key={term}><dt>{term}</dt><dd>{value}</dd></div>)}</dl></div>)}
        </div>
      </div>
      <FigureLightbox id="topology-figure" src="/execution-topologies.png" alt={language === "zh" ? "执行拓扑高清图" : "Full execution topology"} closeHref="#showcase" closeLabel={language === "zh" ? "关闭高清图" : "Close full figure"} />
    </div>
  );
}

export function LifecycleExplorer({ language }: { language: Language }) {
  const items = lifecycleCopy[language];
  const prefix = `lifecycle-${language}`;
  return (
    <div className="lifecycle-explorer">
      {items.map((item, index) => <input className={`explorer-control lifecycle-control is-${item.id}`} type="radio" name={prefix} id={`${prefix}-${item.id}`} defaultChecked={index === 0} aria-label={item.tab} key={item.id} />)}
      <div className="lifecycle-tabs" aria-label={language === "zh" ? "选择生命周期阶段" : "Choose a lifecycle stage"}>
        {items.map((item) => <label data-mode={item.id} htmlFor={`${prefix}-${item.id}`} key={item.id}>{item.tab}</label>)}
      </div>
      <div className="lifecycle-canvas">
        <a className="interactive-figure" href="#lifecycle-figure" aria-label={language === "zh" ? "放大生命周期图" : "Enlarge lifecycle figure"}>
          <div className="lifecycle-viewport"><img src="/authoring-lifecycle.png" alt={language === "zh" ? "MURAL 完整创作生命周期" : "The complete MURAL authoring lifecycle"} /></div>
          <span>{language === "zh" ? "点击放大" : "Click to enlarge"} ↗</span>
        </a>
        <div className="lifecycle-details">
          {items.map((item) => <div className="lifecycle-detail" data-mode={item.id} key={item.id}><small>{item.tab}</small><h3>{item.title}</h3><p>{item.body}</p><dl><div><dt>{language === "zh" ? "责任角色" : "Owners"}</dt><dd>{item.owner}</dd></div><div><dt>{language === "zh" ? "输入" : "Input"}</dt><dd>{item.input}</dd></div><div><dt>{language === "zh" ? "落盘产物" : "Persisted output"}</dt><dd>{item.output}</dd></div></dl></div>)}
        </div>
      </div>
      <FigureLightbox id="lifecycle-figure" src="/authoring-lifecycle.png" alt={language === "zh" ? "创作生命周期高清图" : "Full authoring lifecycle"} closeHref="#method" closeLabel={language === "zh" ? "关闭高清图" : "Close full figure"} />
    </div>
  );
}

export function RevisionRouter({ language }: { language: Language }) {
  const items = revisionCopy[language];
  const prefix = `revision-${language}`;
  return (
    <div className="revision-router-demo">
      {items.map((item, index) => <input className={`explorer-control revision-control is-${item.id}`} type="radio" name={prefix} id={`${prefix}-${item.id}`} defaultChecked={index === 0} aria-label={item.example} key={item.id} />)}
      <div className="revision-prompts">
        <span>{language === "zh" ? "试一个修改请求" : "TRY A REVISION REQUEST"}</span>
        {items.map((item, index) => <label data-mode={item.id} htmlFor={`${prefix}-${item.id}`} key={item.id}><i>0{index + 1}</i>{item.example}</label>)}
      </div>
      <div className="revision-route-results">
        {items.map((item) => <div className={`revision-route-result scope-${item.id}`} data-mode={item.id} key={item.id}><div className="route-rail"><span>{language === "zh" ? "用户修改" : "USER EDIT"}</span><i>→</i><span>ROUTER</span><i>→</i><strong>{item.scope}</strong></div><small>{item.scope}</small><h3>{item.title}</h3><p>{item.body}</p><div><span>{language === "zh" ? "恢复位置" : "RESUME AT"}</span><strong>{item.restart}</strong></div></div>)}
      </div>
    </div>
  );
}
