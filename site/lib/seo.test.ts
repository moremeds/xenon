import { describe, expect, it } from "vitest";
import manifest from "../app/manifest";
import robots from "../app/robots";
import sitemap from "../app/sitemap";
import {
  DEFAULT_SITE_URL,
  SITE_DESCRIPTION,
  SITE_NAME,
  SITE_TITLE,
  siteMetadata,
  siteStructuredData,
  siteUrl,
  siteViewport,
} from "./seo";

describe("site SEO contract", () => {
  it("publishes canonical and social metadata", () => {
    expect(siteUrl).toBe(DEFAULT_SITE_URL);
    expect(siteMetadata.title).toBe(SITE_TITLE);
    expect(siteMetadata.description).toBe(SITE_DESCRIPTION);
    expect(siteMetadata.alternates?.canonical).toBe("/");
    expect(siteMetadata.openGraph).toMatchObject({
      type: "website",
      url: "/",
      title: SITE_TITLE,
      description: SITE_DESCRIPTION,
      siteName: SITE_NAME,
    });
    expect(siteMetadata.twitter).toMatchObject({
      card: "summary_large_image",
      title: SITE_TITLE,
      description: SITE_DESCRIPTION,
    });
    expect(siteViewport.themeColor).toBe("#0a0f14");
  });

  it("publishes structured data for website, organization, and software", () => {
    const types = siteStructuredData.map((item) => item["@type"]);
    expect(types).toEqual(["WebSite", "Organization", "SoftwareApplication"]);
    expect(siteStructuredData[0]).toMatchObject({
      "@context": "https://schema.org",
      "@type": "WebSite",
      url: siteUrl,
    });
  });

  it("publishes crawl routes and manifest metadata", () => {
    expect(robots()).toEqual({
      rules: {
        userAgent: "*",
        allow: "/",
      },
      sitemap: `${siteUrl}/sitemap.xml`,
      host: siteUrl,
    });

    expect(sitemap()).toEqual([
      {
        url: siteUrl,
        lastModified: new Date("2026-03-11T00:00:00.000Z"),
        changeFrequency: "weekly",
        priority: 1,
      },
    ]);

    expect(manifest()).toMatchObject({
      name: SITE_NAME,
      short_name: "Radon",
      description: SITE_DESCRIPTION,
      start_url: "/",
      display: "standalone",
    });
  });
});
