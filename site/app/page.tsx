import { BlogPage } from "./BlogPage";
import { createPageMetadata } from "./metadata";

export const metadata = createPageMetadata(
  "MURAL Presenter — Long-horizon presentation authoring",
  "A Skill-driven, revision-aware multi-agent framework for the full lifecycle of editable HTML presentations.",
  "en_US",
);

export default function Home() {
  return <BlogPage language="en" />;
}
