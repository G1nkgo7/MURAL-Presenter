import { MuralSite } from "../MuralSite";
import { createPageMetadata } from "../metadata";

export const metadata = createPageMetadata(
  "MURAL Presenter — 面向长程演示文稿的完整生命周期创作",
  "一个技能驱动、修改感知的多智能体框架，用于可编辑 HTML 演示文稿的生成、复审与持续修改。",
  "zh_CN",
);

export default function ChineseHome() {
  return <MuralSite language="zh" />;
}
