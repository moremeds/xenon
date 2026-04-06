import { test, expect } from "../../web/node_modules/@playwright/test";

test.describe("Radon site branding", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
  });

  test("should have the correct favicons and icons in head", async ({ page }) => {
    // Check 32x32 favicon
    const favicon32 = page.locator('link[rel="icon"][sizes="32x32"]');
    await expect(favicon32).toHaveAttribute("href", "/favicon-32x32.png");
    await expect(favicon32).toHaveAttribute("type", "image/png");

    // Check 16x16 favicon
    const favicon16 = page.locator('link[rel="icon"][sizes="16x16"]');
    await expect(favicon16).toHaveAttribute("href", "/favicon-16x16.png");
    await expect(favicon16).toHaveAttribute("type", "image/png");

    // Check SVG icon
    const faviconSvg = page.locator('link[rel="icon"][type="image/svg+xml"]');
    await expect(faviconSvg).toHaveAttribute("href", "/brand/radon-app-icon.svg");

    // Check Apple Touch Icon
    const appleIcon = page.locator('link[rel="apple-touch-icon"]');
    await expect(appleIcon).toHaveAttribute("href", "/apple-touch-icon.png");
    await expect(appleIcon).toHaveAttribute("sizes", "180x180");
  });

  test("should have correct Open Graph metadata", async ({ page }) => {
    // OG Title and Description (inherited from SEO constants)
    await expect(page).toHaveTitle(/Radon Terminal/);
    
    const ogImage = page.locator('meta[property="og:image"]');
    await expect(ogImage).toHaveAttribute("content", /og-image\.png$/);

    const twitterImage = page.locator('meta[name="twitter:image"]');
    await expect(twitterImage).toHaveAttribute("content", /og-image\.png$/);

    const ogImageAlt = page.locator('meta[property="og:image:alt"]');
    await expect(ogImageAlt).toHaveAttribute("content", /Radon Terminal marketing card/);
  });

  test("should have a manifest with correct icons", async ({ page }) => {
    const manifestLink = page.locator('link[rel="manifest"]');
    await expect(manifestLink).toHaveAttribute("href", "/manifest.webmanifest");

    // Fetch and verify manifest content
    const response = await page.request.get("/manifest.webmanifest");
    expect(response.ok()).toBeTruthy();
    const manifest = await response.json();

    expect(manifest.name).toBe("Radon Terminal");
    
    const icons = manifest.icons as Array<{ sizes: string }>;
    const sizes = icons.map((icon) => icon.sizes);
    
    expect(sizes).toContain("32x32");
    expect(sizes).toContain("16x16");
    expect(sizes).toContain("64x64");
    expect(sizes).toContain("128x128");
    expect(sizes).toContain("256x256");
    expect(sizes).toContain("512x512");
    expect(sizes).toContain("any");
  });
});
