"use client";

import { useCallback, useMemo, useState } from "react";

/**
 * Generic table filter hook. Filters rows by substring match across
 * specified fields. Designed to compose with useSort:
 *
 *   const { sorted } = useSort(data, extractValue);
 *   const { filtered, query, setQuery } = useTableFilter(sorted, extractSearchText);
 *   // render filtered.map(...)
 */
export type SearchTextExtractor<T> = (item: T) => string;

export function useTableFilter<T>(
  data: readonly T[] | T[],
  extractSearchText: SearchTextExtractor<T>,
) {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return data as T[];
    return (data as T[]).filter((item) =>
      extractSearchText(item).toLowerCase().includes(q),
    );
  }, [data, query, extractSearchText]);

  const clear = useCallback(() => setQuery(""), []);

  return { filtered, query, setQuery, clear };
}
