import type { Metadata } from "next";
import { MuralSite } from "./MuralSite";

export const metadata: Metadata = {
  title: "MURAL — Long-horizon presentation authoring",
  description:
    "A Skill-driven, revision-aware multi-agent framework for the full lifecycle of editable HTML presentations.",
};

export default function Home() {
  return <MuralSite language="en" />;
}

