import { PaperPage } from "../../PaperPage";
import { createPageMetadata } from "../../metadata";

export const metadata = createPageMetadata(
  "MURAL-Presenter — 论文工作稿",
  "阅读 MURAL-Presenter 当前的中文论文工作稿。",
  "zh_CN",
);

export default function ChinesePaper() {
  return <PaperPage language="zh" />;
}
