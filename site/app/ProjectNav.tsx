import { BrandLockup } from "./MuralSite";

type ProjectNavProps = {
  language: "en" | "zh";
  homeHref: string;
  homeLabel: string;
  languageHref: string;
  languageLabel: string;
  paperHref: string;
};

export function ProjectNav({ language, homeHref, homeLabel, languageHref, languageLabel, paperHref }: ProjectNavProps) {
  const isZh = language === "zh";
  return (
    <header className="nav-shell blog-nav">
      <a className="brand" href={homeHref} aria-label={homeLabel}>
        <BrandLockup />
        <span className="nav-descriptor">· {isZh ? "长程演示创作" : "long-horizon presentation authoring"}</span>
      </a>
      <nav aria-label={isZh ? "项目导航" : "Project navigation"}>
        <a href="#overview">{isZh ? "概览" : "Overview"}</a>
        <a href="#task">{isZh ? "任务" : "The Task"}</a>
        <a href="#method">{isZh ? "方法" : "Method"}</a>
        <a href="#benchmark">{isZh ? "评测" : "Benchmark"}</a>
      </nav>
      <a className="nav-paper-link" href={paperHref}>Paper</a>
      <a className="language-link" href={languageHref}>{languageLabel}</a>
    </header>
  );
}
