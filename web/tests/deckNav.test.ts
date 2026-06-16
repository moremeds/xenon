// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import {
  isUrlDeck,
  legacyTabToDeck,
  URL_DECKS,
  type DeckKey,
  type UrlDeckKey,
} from "@/lib/deckNav";

describe("deckNav", () => {
  it("URL_DECKS holds the 6 URL-addressable decks (not the local-only : and o)", () => {
    expect([...URL_DECKS].sort()).toEqual(["c", "i", "n", "p", "r", "s"]);
  });
  it("isUrlDeck validates URL-addressable keys only", () => {
    expect(isUrlDeck("c")).toBe(true);
    expect(isUrlDeck("o")).toBe(false); // order deck is local-only, not URL
    expect(isUrlDeck(":")).toBe(false); // command palette is local-only
    expect(isUrlDeck(null)).toBe(false);
  });
  it("legacyTabToDeck maps old ?tab= values", () => {
    expect(legacyTabToDeck("chain")).toBe("c");
    expect(legacyTabToDeck("position")).toBe("p");
    expect(legacyTabToDeck("news")).toBe("n");
    expect(legacyTabToDeck("ratings")).toBe("r");
    expect(legacyTabToDeck("seasonality")).toBe("s");
    expect(legacyTabToDeck("company")).toBe("i");
    expect(legacyTabToDeck("book")).toBe(null); // book is the default surface, no deck
    expect(legacyTabToDeck("order")).toBe(null); // order ticket always-visible (desktop)
    expect(legacyTabToDeck(null)).toBe(null);
  });
});

// Reference the type-only imports so tsc's noUnusedLocals stays quiet.
type _Decks = [DeckKey, UrlDeckKey];
