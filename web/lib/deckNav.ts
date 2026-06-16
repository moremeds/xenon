// Full deck universe (matches radon AssetCockpit's DeckKey): 6 URL-addressable
// decks + the local-only command palette (":") and mobile order ticket ("o").
export type DeckKey = "c" | "p" | "n" | "r" | "s" | "i" | ":" | "o";

// The subset that can live in the URL (?deck=). ":" and "o" are local-only and
// never serialize to the URL — they're held in component state (Task 2.13).
export type UrlDeckKey = "c" | "p" | "n" | "r" | "s" | "i";

export const URL_DECKS: ReadonlySet<UrlDeckKey> = new Set<UrlDeckKey>([
  "c",
  "p",
  "n",
  "r",
  "s",
  "i",
]);

export function isUrlDeck(
  value: string | null | undefined,
): value is UrlDeckKey {
  return value != null && URL_DECKS.has(value as UrlDeckKey);
}

export function legacyTabToDeck(tab: string | null): UrlDeckKey | null {
  switch (tab) {
    case "chain":
      return "c";
    case "position":
      return "p";
    case "news":
      return "n";
    case "ratings":
      return "r";
    case "seasonality":
      return "s";
    case "company":
      return "i";
    default:
      return null; // "book" / "order" / unknown → no overlay (book-first)
  }
}
