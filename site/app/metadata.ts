import type { Metadata } from "next";

type PageLocale = "en_US" | "zh_CN";

export function createPageMetadata(
  title: string,
  description: string,
  locale: PageLocale,
): Metadata {
  return {
    title,
    description,
    openGraph: {
      title,
      description,
      type: "website",
      locale,
      alternateLocale: [locale === "en_US" ? "zh_CN" : "en_US"],
      images: [{
        url: "og.png",
        width: 1734,
        height: 907,
        alt: "MURAL Presenter — long-horizon, multi-agent presentation authoring",
      }],
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
      images: ["og.png"],
    },
  };
}
