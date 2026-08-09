import { BlogPage } from "../../BlogPage";
import { createPageMetadata } from "../../metadata";

export const metadata = createPageMetadata(
  "演示文稿不是一摞页面 — MURAL",
  "介绍 MURAL：一种面向长程演示文稿完整生成与修改生命周期的方法。",
  "zh_CN",
);

export default function ChineseBlog() {
  return <BlogPage language="zh" />;
}
