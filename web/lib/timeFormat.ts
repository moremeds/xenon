// Blotter timestamps are pinned to exchange time (ET), not browser-local
// time — a 15:17 ET fill must not render as 03:17 for a UTC+8 operator.
const ET_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour12: false,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

export function formatEtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${ET_TIME.format(d)} ET`;
}
