import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MURAL",
  description:
    "Multi-Agent Unified Revision-Aware Authoring for long-horizon presentations.",
  icons: {
    icon: "/favicon.png",
    shortcut: "/favicon.png",
  },
  openGraph: {
    title: "MURAL — A presentation is not a stack of slides",
    description:
      "A full-lifecycle, revision-aware approach to long-horizon presentation authoring.",
    type: "website",
    images: ["/og.png"],
  },
  twitter: {
    card: "summary_large_image",
    title: "MURAL — A presentation is not a stack of slides",
    description:
      "Full-lifecycle authoring for editable, long-horizon presentations.",
    images: ["/og.png"],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

