import { test, expect } from "vitest";
import {
  titleCase,
  formatCurrency,
  parsePossibleJson,
  normalizeForCell,
  normalizeTextLines,
  valueToText,
  formatArrayAsTable,
  formatPortfolioPayload,
  formatJournalPayload,
  formatAssistantPayload,
  formatPiPayload,
} from "../lib/utils";

test("titleCase converts snake_case to Title Case", () => {
  expect(titleCase("hello_world")).toBe("Hello World");
  expect(titleCase("dark-pool-flow")).toBe("Dark Pool Flow");
  expect(titleCase("single")).toBe("Single");
  expect(titleCase("")).toBe("");
});

test("formatCurrency formats numbers as USD", () => {
  expect(formatCurrency(1000)).toBe("$1,000");
  expect(formatCurrency(981353)).toBe("$981,353");
  expect(formatCurrency(0)).toBe("$0");
  expect(formatCurrency("5000")).toBe("$5,000");
  expect(formatCurrency("not a number")).toBe("N/A");
  expect(formatCurrency(NaN)).toBe("N/A");
  expect(formatCurrency(Infinity)).toBe("N/A");
});

test("parsePossibleJson parses valid JSON objects and arrays", () => {
  expect(parsePossibleJson('{"a":1}')).toEqual({ a: 1 });
  expect(parsePossibleJson('[1,2,3]')).toEqual([1, 2, 3]);
  expect(parsePossibleJson("not json")).toBe(null);
  expect(parsePossibleJson("")).toBe(null);
  expect(parsePossibleJson("  ")).toBe(null);
  expect(parsePossibleJson("hello world")).toBe(null);
});

test("normalizeForCell converts values to display strings", () => {
  expect(normalizeForCell(null)).toBe("N/A");
  expect(normalizeForCell(undefined)).toBe("N/A");
  expect(normalizeForCell("hello")).toBe("hello");
  expect(normalizeForCell(42)).toBe("42");
  expect(normalizeForCell(true)).toBe("true");
  expect(normalizeForCell({ a: 1 })).toBe('{"a":1}');
});

test("normalizeTextLines trims whitespace and line endings", () => {
  expect(normalizeTextLines("hello  \nworld  \n")).toBe("hello\nworld");
  expect(normalizeTextLines("  hello  \n  world  ")).toBe("hello\n  world");
  expect(normalizeTextLines("")).toBe("");
  expect(normalizeTextLines("   ")).toBe("");
});

test("valueToText handles all value types", () => {
  expect(valueToText(null)).toBe("N/A");
  expect(valueToText(undefined)).toBe("N/A");
  expect(valueToText(true)).toBe("true");
  expect(valueToText(42)).toBe("42");
  expect(valueToText("hello")).toBe("hello");
  expect(valueToText({})).toBe("");
});

test("formatArrayAsTable produces markdown table from objects", () => {
  const data = [
    { ticker: "AAPL", score: 80 },
    { ticker: "MSFT", score: 75 },
  ];
  const result = formatArrayAsTable(data);
  expect(result).toBeTruthy();
  expect(result.includes("| Ticker | Score |")).toBeTruthy();
  expect(result.includes("| AAPL | 80 |")).toBeTruthy();
  expect(result.includes("| MSFT | 75 |")).toBeTruthy();
});

test("formatArrayAsTable returns message for empty array", () => {
  expect(formatArrayAsTable([])).toBe("No rows available.");
});

test("formatPortfolioPayload formats portfolio data", () => {
  const data = {
    bankroll: 100000,
    position_count: 3,
    defined_risk_count: 1,
    undefined_risk_count: 2,
    last_sync: "2026-01-01",
    positions: [],
  };
  const result = formatPortfolioPayload(data);
  expect(result.includes("Portfolio Snapshot")).toBeTruthy();
  expect(result.includes("$100,000")).toBeTruthy();
  expect(result.includes("Positions: 3")).toBeTruthy();
  expect(result.includes("No positions found.")).toBeTruthy();
});

test("formatJournalPayload formats trade journal", () => {
  const data = { trades: [] };
  const result = formatJournalPayload(data);
  expect(result.includes("Recent Journal")).toBeTruthy();
  expect(result.includes("No trades logged.")).toBeTruthy();
});

test("formatAssistantPayload passes through plain text", () => {
  expect(formatAssistantPayload("Hello world")).toBe("Hello world");
});

test("formatPiPayload routes portfolio command", () => {
  const json = JSON.stringify({ bankroll: 50000, positions: [] });
  const result = formatPiPayload("portfolio", json);
  expect(result.includes("Portfolio Snapshot")).toBeTruthy();
  expect(result.includes("$50,000")).toBeTruthy();
});

test("formatPiPayload routes journal command", () => {
  const json = JSON.stringify({ trades: [] });
  const result = formatPiPayload("journal", json);
  expect(result.includes("Recent Journal")).toBeTruthy();
});
