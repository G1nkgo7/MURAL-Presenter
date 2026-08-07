import type { Metadata } from "next";
import { BlogPage } from "../BlogPage";

export const metadata: Metadata = {
  title: "A presentation is not a stack of slides — MURAL",
  description:
    "Introducing MURAL, a Skill-driven multi-agent framework for long-horizon presentation authoring and revision.",
};

export default function EnglishBlog() {
  return <BlogPage language="en" />;
}
