type Language = "en" | "zh";

const heroStages = {
  zh: [
    { id: "material", label: "Material", number: "01", title: "接住真实输入", body: "整理附件与已有材料；只在关键证据缺口出现时启动 Research。", state: "material.md · knowledge-brief.md" },
    { id: "plan", label: "Plan", number: "02", title: "固定整册决定", body: "把受众、叙事弧、设计语言、页面地图与素材策略写入共享蓝图。", state: "deck.md · base.css · group briefs" },
    { id: "group", label: "Group", number: "03", title: "让关联页面共同生产", body: "Group Agents 并行制作页面组，在组内完成 render–inspect–revise。", state: "slide groups · contact sheets" },
    { id: "review", label: "Review", number: "04", title: "重新看见整册", body: "装配权威 deck，以 whole-deck Review 检查跨组叙事和设计关系。", state: "rendered deck · review report" },
    { id: "revise", label: "Revise", number: "05", title: "从最小可靠范围继续", body: "把修改路由至单页、相关页面组或整册规划，而不是从头再来。", state: "page · group · deck" },
  ],
  en: [
    { id: "material", label: "Material", number: "01", title: "Ground the real input", body: "Organize attachments and supplied material; invoke Research only for consequential evidence gaps.", state: "material.md · knowledge-brief.md" },
    { id: "plan", label: "Plan", number: "02", title: "Fix deck-level decisions", body: "Persist the audience, narrative arc, design language, page map, and asset strategy in one blueprint.", state: "deck.md · base.css · group briefs" },
    { id: "group", label: "Group", number: "03", title: "Let related slides share production", body: "Group Agents author slide groups in parallel and close render–inspect–revise inside each group.", state: "slide groups · contact sheets" },
    { id: "review", label: "Review", number: "04", title: "See the deck again", body: "Assemble the authoritative deck and inspect narrative and design relations across groups.", state: "rendered deck · review report" },
    { id: "revise", label: "Revise", number: "05", title: "Resume at the smallest reliable scope", body: "Route an edit to a page, its responsible group, or the deck plan instead of starting over.", state: "page · group · deck" },
  ],
} as const;

export function HeroExplorer({ language }: { language: Language }) {
  const stages = heroStages[language];
  const prefix = `hero-stage-${language}`;
  const isZh = language === "zh";
  return (
    <div className="hero-explorer" aria-label={isZh ? "探索 MURAL 创作生命周期" : "Explore the MURAL authoring lifecycle"}>
      {stages.map((stage, index) => (
        <input
          className={`hero-stage-control is-${stage.id}`}
          type="radio"
          name={prefix}
          id={`${prefix}-${stage.id}`}
          defaultChecked={index === 2}
          aria-label={stage.label}
          key={stage.id}
        />
      ))}
      <div className="hero-stage-hotspots" aria-label={isZh ? "选择壁画阶段" : "Choose a mural stage"}>
        {stages.map((stage) => (
          <label className={`hero-hotspot is-${stage.id}`} htmlFor={`${prefix}-${stage.id}`} data-mode={stage.id} key={stage.id}>
            <i aria-hidden="true" />
            <span><small>{stage.number}</small><strong>{stage.label}</strong></span>
          </label>
        ))}
      </div>
      <div className="hero-stage-stories">
        {stages.map((stage) => (
          <article data-mode={stage.id} key={stage.id}>
            <span>{stage.number} · {stage.label}</span>
            <h2>{stage.title}</h2>
            <p>{stage.body}</p>
            <small>{isZh ? "持久状态" : "PERSISTED STATE"}</small>
            <strong>{stage.state}</strong>
          </article>
        ))}
      </div>
      <div className="hero-explorer-hint"><span>{isZh ? "选择一个阶段" : "CHOOSE A STAGE"}</span><i>↗</i></div>
    </div>
  );
}
