"use client";

import "./globals.css";
import { useEffect } from "react";

type AppError = Error & { digest?: string };

/**
 * Root-level error UI when the root layout fails. Must define html/body (replaces root layout).
 */
export default function GlobalError({ error, reset }: { error: AppError; reset: () => void }) {
  useEffect(() => {
    console.error("[radon] global error boundary:", error);
  }, [error]);

  return (
    <html lang="en" data-theme="dark">
      <body className="app-root">
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            minHeight: "100vh",
            fontFamily: "var(--font-mono)",
            color: "var(--text-secondary)",
            gap: "16px",
            padding: "24px",
            background: "var(--bg-base)",
          }}
        >
          <span
            style={{
              fontSize: "11px",
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              color: "var(--fault, #E85D6C)",
            }}
          >
            Application Error
          </span>
          <span style={{ fontSize: "12px", textAlign: "center", maxWidth: "420px" }}>
            Radon Terminal could not render. Reload the page or return after a moment.
          </span>
          {error.digest ? (
            <span style={{ fontSize: "10px", color: "var(--text-muted)" }}>Digest: {error.digest}</span>
          ) : null}
          <button
            type="button"
            onClick={() => reset()}
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "11px",
              padding: "8px 14px",
              borderRadius: "4px",
              border: "1px solid var(--border-dim, #1e293b)",
              background: "var(--bg-panel-raised, #151c22)",
              color: "var(--text-primary)",
              cursor: "pointer",
            }}
          >
            Retry
          </button>
        </div>
      </body>
    </html>
  );
}
