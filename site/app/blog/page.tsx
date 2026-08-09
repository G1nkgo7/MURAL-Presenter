import { BlogPage } from "../BlogPage";
import { createPageMetadata } from "../metadata";

export const metadata = createPageMetadata(
  "A presentation is not a stack of slides — MURAL",
  "Introducing MURAL, a Skill-driven multi-agent framework for long-horizon presentation authoring and revision.",
  "en_US",
);

export default function EnglishBlog() {
  return <BlogPage language="en" />;
}
