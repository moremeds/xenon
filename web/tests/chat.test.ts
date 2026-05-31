import { test, expect } from "vitest";
import {
  isPiCommandInput,
  normalizeCommandInput,
  routeToPiPrompt,
  fallbackReply,
  resolveSectionFromPath,
} from "../lib/chat";

test("isPiCommandInput identifies valid PI commands", () => {
  expect(isPiCommandInput("portfolio")).toBe(true);
  expect(isPiCommandInput("/portfolio")).toBe(true);
  expect(isPiCommandInput("journal --limit 5")).toBe(true);
  expect(isPiCommandInput("sync")).toBe(true);
  expect(isPiCommandInput("help")).toBe(true);
});

test("isPiCommandInput rejects non-commands", () => {
  expect(isPiCommandInput("hello world")).toBe(false);
  expect(isPiCommandInput("")).toBe(false);
  expect(isPiCommandInput("   ")).toBe(false);
});

test("normalizeCommandInput adds leading slash", () => {
  expect(normalizeCommandInput("portfolio")).toBe("/portfolio");
  expect(normalizeCommandInput("/portfolio")).toBe("/portfolio");
  expect(normalizeCommandInput("  journal --limit 5  ")).toBe(
    "/journal --limit 5",
  );
});

test("routeToPiPrompt routes direct commands", () => {
  expect(routeToPiPrompt("portfolio")).toBe("/portfolio");
  expect(routeToPiPrompt("journal --limit 10")).toBe("/journal --limit 10");
});

test("routeToPiPrompt routes aliases", () => {
  expect(routeToPiPrompt("action items")).toBe("/journal --limit 25");
});

test("routeToPiPrompt routes keyword matches", () => {
  expect(routeToPiPrompt("show me the portfolio")).toBe("/portfolio");
  expect(routeToPiPrompt("check positions")).toBe("/portfolio");
  expect(routeToPiPrompt("open journal")).toBe("/journal");
});

test("routeToPiPrompt returns null for unrecognized input", () => {
  expect(routeToPiPrompt("hello world")).toBe(null);
  expect(routeToPiPrompt("what is the weather")).toBe(null);
  expect(routeToPiPrompt("")).toBe(null);
  expect(routeToPiPrompt("   ")).toBe(null);
});

test("fallbackReply returns non-empty replies", () => {
  expect(fallbackReply("").length > 0).toBeTruthy();
  expect(fallbackReply("help").length > 0).toBeTruthy();
});

test("resolveSectionFromPath maps URL paths to sections", () => {
  expect(resolveSectionFromPath("/", "dashboard")).toBe("dashboard");
  expect(resolveSectionFromPath("/dashboard", "dashboard")).toBe("dashboard");
  expect(resolveSectionFromPath("/portfolio", "dashboard")).toBe("portfolio");
  expect(resolveSectionFromPath("/performance", "dashboard")).toBe(
    "performance",
  );
  expect(resolveSectionFromPath("/journal", "dashboard")).toBe("journal");
  expect(resolveSectionFromPath("/AAPL", "dashboard")).toBe("ticker-detail");
  expect(resolveSectionFromPath("/unknown", "dashboard")).toBe("dashboard");
  expect(resolveSectionFromPath(null, "dashboard")).toBe("dashboard");
});
