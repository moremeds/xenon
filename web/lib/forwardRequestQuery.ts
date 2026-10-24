/** Forward the browser/proxy query string verbatim (`?a=b` or ``). Server-safe (no React). */
export function forwardRequestQuery(req?: Request): string {
  if (!req?.url) return "";
  return new URL(req.url).search;
}
