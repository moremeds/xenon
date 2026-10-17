import Link from "next/link";

export default function TickerNotFound() {
  return (
    <div style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      minHeight: "60vh",
      fontFamily: "var(--font-mono)",
      color: "var(--text-secondary)",
      gap: "16px",
    }}>
      <span style={{ fontSize: "11px", letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--fault)" }}>
        404 — Instrument Not Found
      </span>
      <span style={{ fontSize: "12px" }}>
        The requested ticker path is not a valid instrument identifier.
      </span>
      <Link
        href="/dashboard"
        style={{
          fontSize: "11px",
          color: "var(--signal-core)",
          textDecoration: "none",
          borderBottom: "1px solid var(--signal-core)",
          paddingBottom: "1px",
        }}
      >
        Return to Dashboard
      </Link>
    </div>
  );
}
