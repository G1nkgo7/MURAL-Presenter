import type { Metadata } from "next";
import { BlogPage } from "../../BlogPage";

export const metadata: Metadata = {
  title: "演示文稿不是一摞页面 — MURAL",
  description: "介绍 MURAL：一种面向长程演示文稿完整生成与修改生命周期的方法。",
};

export default function ChineseBlog() {
  return <BlogPage language="zh" />;
}
