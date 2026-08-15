import type { Metadata } from "next";
import "./globals.css";

// Deployments should set NEXT_PUBLIC_SITE_URL to their public canonical base.
// The localhost fallback keeps public source free of environment-specific hosts.
const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000/";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: "MURAL Presenter",
  description:
    "Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations.",
  icons: {
    icon: "/favicon-v2.png",
    shortcut: "/favicon-v2.png",
  },
  openGraph: {
    title: "MURAL Presenter — A presentation is not a stack of slides",
    description:
      "A full-lifecycle, revision-aware approach to long-horizon presentation authoring.",
    type: "website",
    images: [{
      url: "og.png",
      width: 1734,
      height: 907,
      alt: "MURAL Presenter — long-horizon, multi-agent presentation authoring",
    }],
  },
  twitter: {
    card: "summary_large_image",
    title: "MURAL Presenter — A presentation is not a stack of slides",
    description:
      "Full-lifecycle authoring for editable, long-horizon presentations.",
    images: ["og.png"],
  },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
