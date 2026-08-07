import type { Metadata } from "next";
import { MuralSite } from "../MuralSite";

export const metadata: Metadata = {
  title: "MURAL — 面向长程演示文稿的完整生命周期创作",
  description:
    "一个技能驱动、修改感知的多智能体框架，用于可编辑 HTML 演示文稿的生成、复审与持续修改。",
};

export default function ChineseHome() {
  return <MuralSite language="zh" />;
}

