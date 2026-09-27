// Read-only cockpit boundary. GET only, a fixed allowlist of backend paths, an
// allowlist of query keys, and no credentials. Used by the cockpit proxy route.
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const ALLOWED = [
  /^scout\/overview$/,
  /^scout\/watches$/,
  new RegExp(`^scout/watches/${UUID}$`),
  new RegExp(`^scout/watches/${UUID}/assessments$`),
  /^scout\/runs$/,
  /^paper\/portfolio$/,
  // The existing read-only TradeCase views, for linking a promoted watch.
  new RegExp(`^trade-cases/${UUID}(/(timeline|evidence|tasks))?$`),
];
const QUERY = new Set([
  "status",
  "chain",
  "venue",
  "has_assessment",
  "promotable",
  "max_age_seconds",
  "limit",
  "offset",
  "fills",
  "pnl",
]);

export function cockpitTarget(
  path: string[],
  search: URLSearchParams,
  baseUrl: string = process.env.MARKET_API_BASE_URL ?? "http://127.0.0.1:8000",
): URL | null {
  const joined = path.join("/");
  if (!ALLOWED.some((rule) => rule.test(joined))) return null;
  const base = new URL(baseUrl);
  if (
    !["http:", "https:"].includes(base.protocol) ||
    base.username ||
    base.password
  )
    return null;
  const url = new URL(`/api/${joined}`, base);
  for (const [key, value] of search) {
    if (QUERY.has(key) && value.length <= 80)
      url.searchParams.append(key, value);
  }
  return url;
}
