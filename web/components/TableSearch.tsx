"use client";

import { Search, X } from "lucide-react";

export default function TableSearch({
  query,
  setQuery,
  placeholder = "Filter...",
  resultCount,
  totalCount,
}: {
  query: string;
  setQuery: (q: string) => void;
  placeholder?: string;
  resultCount?: number;
  totalCount?: number;
}) {
  return (
    <div className="table-search">
      <Search size={12} className="table-search-icon" />
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={placeholder}
        className="table-search-input"
      />
      {query && (
        <button
          type="button"
          onClick={() => setQuery("")}
          className="table-search-clear"
          aria-label="Clear filter"
        >
          <X size={12} />
        </button>
      )}
      {query && resultCount != null && totalCount != null && (
        <span className="table-search-count">
          {resultCount}/{totalCount}
        </span>
      )}
    </div>
  );
}
