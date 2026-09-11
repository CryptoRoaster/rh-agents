import { NextResponse } from "next/server";
import type { ChainFeed, MarketChain } from "@/lib/market-feed";
export const dynamic = "force-dynamic";
// Read-only server boundary. No credentials, mutation routes or provider HTTP clients.
async function readChain(chain: MarketChain): Promise<ChainFeed> {
  const unavailable: ChainFeed = {
    chain,
    status: "unavailable",
    count: null,
    capped: false,
    observedAt: null,
  };
  try {
    const base = new URL(
      process.env.MARKET_API_BASE_URL ?? "http://127.0.0.1:8000",
    );
    if (
      !["http:", "https:"].includes(base.protocol) ||
      base.username ||
      base.password
    )
      return unavailable;
    const url = new URL("/api/markets", base);
    url.search = new URLSearchParams({
      chain,
      network: "mainnet",
      provider: "geckoterminal",
      include_fixtures: "false",
      limit: "100",
    }).toString();
    const response = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
      redirect: "error",
    });
    if (!response.ok) return unavailable;
    const data: unknown = await response.json();
    if (!Array.isArray(data) || data.length > 100) return unavailable;
    const times: string[] = [];
    for (const row of data) {
      if (
        !row ||
        typeof row !== "object" ||
        row.chain !== chain ||
        row.network !== "mainnet" ||
        row.provider !== "geckoterminal" ||
        row.is_fixture !== false ||
        typeof row.observed_at !== "string" ||
        !Number.isFinite(Date.parse(row.observed_at)) ||
        Date.parse(row.observed_at) > Date.now()
      )
        return unavailable;
      times.push(row.observed_at);
    }
    times.sort((a, b) => Date.parse(b) - Date.parse(a));
    return {
      chain,
      status: "available",
      count: data.length,
      capped: data.length === 100,
      observedAt: times[0] ?? null,
    };
  } catch {
    return unavailable;
  }
}
export async function GET() {
  const chains = await Promise.all([readChain("robinhood"), readChain("bsc")]);
  return NextResponse.json(
    { provider: "geckoterminal", checkedAt: new Date().toISOString(), chains },
    { headers: { "Cache-Control": "no-store" } },
  );
}
