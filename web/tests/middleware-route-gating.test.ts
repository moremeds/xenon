import { describe, it, expect } from "vitest";
import { NextRequest } from "next/server";
import { createRouteMatcher } from "@clerk/nextjs/server";
import { PUBLIC_ROUTES } from "../middleware";

// Asserts the default-private + explicit-public-allowlist policy. A new route
// added without touching middleware.ts must inherit auth — failing closed is
// the whole point of this design (project's universal-auth-gating policy).

const isPublic = createRouteMatcher(PUBLIC_ROUTES);

const matches = (path: string): boolean =>
  isPublic(new NextRequest(new URL(`http://localhost${path}`)));

describe("middleware route gating", () => {
  it.each([
    "/portfolio",
    "/portfolio/123",
    "/orders",
    "/orders/quote",
    "/journal",
    "/internals",
    "/dashboard",
    "/performance",
    "/", // workspace shell
    "/AAPL", // dynamic [ticker] route
  ])("private: %s requires auth", (path) => {
    expect(matches(path)).toBe(false);
  });

  it.each([
    "/sign-in",
    "/sign-in/factor-one",
    "/sign-up",
    "/api/portfolio",
    "/scanner",
    "/scanner/uw",
    "/discover",
    "/regime",
    "/flow-analysis",
    "/uw-analyze",
    "/uw-analyze/AAPL",
    "/cta",
    "/kit",
  ])("public: %s allowed without auth", (path) => {
    expect(matches(path)).toBe(true);
  });

  it("PUBLIC_ROUTES never matches an unlisted top-level segment", () => {
    // Sanity: a freshly-introduced route like /admin must default to private
    // until someone explicitly adds it to PUBLIC_ROUTES.
    expect(matches("/admin")).toBe(false);
    expect(matches("/billing")).toBe(false);
  });
});
