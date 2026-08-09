import { BrandLockup } from "./MuralSite";

type Language = "en" | "zh";

const copy = {
  en: {
    home: "Project home",
    blog: "Launch article",
    language: "中文",
    languageHref: "/zh/paper",
    eyebrow: "MURAL Presenter · Research manuscript",
    status: "Local draft · Results pending",
    titleLead: "MURAL-Presenter",
    titleRest: "Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations",
    localizedTitle: "",
    documentLabel: "Working manuscript",
    abstractLabel: "Abstract",
    abstract:
      "Automated presentation authoring is a long-horizon multimodal task: individual slides must be informative and well composed, while the complete deck must preserve narrative, factual, and visual decisions across production and later edits. MURAL-Presenter (MURAL) is a Skill-driven multi-agent framework that externalizes this lifecycle as a reusable authoring procedure. An Orchestrator maintains shared deck state, projects global decisions into briefs, and assigns narratively or visually dependent slides to accountable Group Agents for joint generation and inspection. Whole-deck review restores global closure after assembly, while a revision router resumes work at page, group, evidence, or deck scope according to impact. The accompanying THREAD-Bench is designed to evaluate whether long-range requirements remain observable and satisfied across the resulting deck.",
    note:
      "This is the current working manuscript. Wording, figures, experiments, and citations may change before public release.",
    open: "Open PDF",
    download: "Download draft",
    reader: "Manuscript reader",
    readerHint: "Working manuscript · 12 pages · ICLR format",
    previewTitle: "Read the current working manuscript.",
    previewBody:
      "This preview is rendered from the checked-in PDF, so it remains visible even when the browser has no PDF plug-in. The manuscript marks E1–E7 results as pending and makes no effectiveness claim yet.",
    previewMeta: ["Bilingual", "12 pages", "Results pending"],
    previewAlt: "First page of the English MURAL-Presenter working manuscript",
  },
  zh: {
    home: "项目主页",
    blog: "宣传文章",
    language: "English",
    languageHref: "/paper",
    eyebrow: "MURAL Presenter · 论文工作稿",
    status: "本地草稿 · 实验结果待补",
    titleLead: "MURAL-Presenter",
    titleRest: "Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations",
    localizedTitle: "面向长程演示文稿的多智能体统一、修改感知创作",
    documentLabel: "论文工作稿",
    abstractLabel: "摘要",
    abstract:
      "自动化演示文稿创作是一项长程多模态任务：单页需要内容准确充实、布局合理，整册还要让叙事、事实和视觉决策贯穿制作过程及后续修改。MURAL-Presenter（简称 MURAL）是一个技能驱动的多智能体框架，它将完整生命周期外置为可复用的创作程序。协调器维护共享的整册状态，将全局决策投影为结构化 brief，并把具有叙事或设计依赖的页面交给同一 Group Agent 联合生成与检查。页面汇合后，整册复审负责恢复全局闭合；生成后的修改则根据影响范围，从单页、页面组、证据阶段或整册规划处继续执行。配套的 THREAD-Bench 用于检验长程要求能否在最终演示文稿中保持可观察且得到满足。",
    note: "这是当前论文工作稿，正式公开前，表述、配图、实验与引用仍可能调整。",
    open: "打开 PDF",
    download: "下载工作稿",
    reader: "论文阅读器",
    readerHint: "论文工作稿 · 12 页 · ICLR 格式",
    previewTitle: "阅读当前论文工作稿。",
    previewBody:
      "这里直接展示仓库中 PDF 的真实首页，因此即使浏览器没有 PDF 插件也不会出现空白。文稿仍将 E1–E7 标记为待完成，本版本不作效果结论。",
    previewMeta: ["中英双版", "12 页", "结果待补"],
    previewAlt: "MURAL-Presenter 中文论文工作稿第一页",
  },
} as const;

export function PaperPage({ language }: { language: Language }) {
  const t = copy[language];
  const isZh = language === "zh";
  const homeHref = isZh ? "/zh" : "/";
  const blogHref = isZh ? "/zh/blog" : "/blog";
  const pdfHref = isZh ? "/mural-paper-zh.pdf" : "/mural-paper.pdf";
  const coverHref = isZh ? "/mural-paper-cover-zh.png" : "/mural-paper-cover-en.png";

  return (
    <main className="paper-page" lang={isZh ? "zh-CN" : "en"} id="top">
      <header className="nav-shell paper-nav">
        <a className="brand" href={homeHref} aria-label={t.home}>
          <BrandLockup />
        </a>
        <nav aria-label={isZh ? "论文导航" : "Paper navigation"}>
          <a href={homeHref}>{t.home}</a>
          <a href={blogHref}>{t.blog}</a>
          <a href="https://github.com/G1nkgo7/MURAL-Presenter" target="_blank" rel="noreferrer">GitHub ↗</a>
        </nav>
        <a className="language-link" href={t.languageHref}>{t.language}</a>
      </header>

      <section className="paper-masthead section-shell">
        <div className="paper-masthead-main">
          <span className="kicker">{t.eyebrow}</span>
          <h1>
            <span className="paper-title-mark">{t.titleLead}</span>
            <span className="paper-title-rest">{t.titleRest}</span>
          </h1>
          {t.localizedTitle && <p className="paper-localized-title">{t.localizedTitle}</p>}
          <div className="paper-byline">
            <strong>{t.documentLabel}</strong>
            <span>{t.status}</span>
          </div>
        </div>
        <aside className="paper-abstract">
          <span>{t.abstractLabel}</span>
          <p>{t.abstract}</p>
          <small>{t.note}</small>
        </aside>
        <div className="paper-masthead-actions">
          <a className="button button-primary" href={pdfHref} target="_blank" rel="noreferrer">{t.open}<span>↗</span></a>
          <a className="button button-secondary" href={pdfHref} download>{t.download}<span>↓</span></a>
        </div>
      </section>

      <section className="paper-reader-section section-shell" aria-labelledby="paper-reader-title">
        <div className="paper-reader-bar">
          <div>
            <span>MANUSCRIPT · PDF</span>
            <strong id="paper-reader-title">{t.reader}</strong>
          </div>
          <small>{t.readerHint}</small>
          <a href={pdfHref} target="_blank" rel="noreferrer">{t.open} ↗</a>
        </div>
        <div className="paper-reader-frame">
          <figure className="paper-preview-sheet">
            <img src={coverHref} alt={t.previewAlt} />
            <figcaption>{isZh ? "当前中文工作稿 · 第 1 页" : "Current English working draft · page 1"}</figcaption>
          </figure>
          <aside className="paper-preview-copy">
            <span>{isZh ? "本地、可追踪的工作稿" : "LOCAL, TRACEABLE DRAFT"}</span>
            <h2>{t.previewTitle}</h2>
            <p>{t.previewBody}</p>
            <div className="paper-preview-meta">
              {t.previewMeta.map((item) => <small key={item}>{item}</small>)}
            </div>
            <a className="button button-primary" href={pdfHref} target="_blank" rel="noreferrer">{t.open}<span>↗</span></a>
          </aside>
        </div>
      </section>

      <footer className="section-shell paper-footer">
        <a className="brand footer-brand" href={homeHref}><BrandLockup /></a>
        <p>{t.status}</p>
        <a href="#top">↑ {isZh ? "返回顶部" : "Back to top"}</a>
      </footer>
    </main>
  );
}
