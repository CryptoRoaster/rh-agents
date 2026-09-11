"use client";
import { useEffect, useState } from "react";
import { Radio, RefreshCw } from "lucide-react";
import { emptyChains, type MarketFeed } from "@/lib/market-feed";
export function MarketFeedIndicator() {
  const [feed, setFeed] = useState<MarketFeed | null>(null);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(0);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    async function load() {
      setLoading(true);
      try {
        const result = await fetch("/api/market-feed", {
          signal: controller.signal,
        });
        if (!result.ok) throw new Error("unavailable");
        const data: MarketFeed = await result.json();
        if (!disposed) setFeed(data);
      } catch {
        if (!disposed) setFeed(null);
      } finally {
        if (!disposed) setLoading(false);
      }
    }
    void load();
    const poll = setInterval(load, 30000);
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      disposed = true;
      controller.abort();
      clearInterval(poll);
      clearInterval(clock);
    };
  }, [refresh]);
  return (
    <section className="market-feed-strip" aria-label="Real market data status">
      <span className="feed-source">
        <Radio size={14} />
        <strong>REAL MARKET FEED</strong>
        <span>GeckoTerminal</span>
      </span>
      {(feed?.chains ?? emptyChains).map((chain) => (
        <span className="chain-feed" key={chain.chain}>
          <i
            className={`dot ${chain.status === "available" ? "green" : "orange"}`}
          />
          <strong>
            {chain.chain === "robinhood" ? "Robinhood Chain" : "BSC"}
          </strong>
          <span>
            {chain.status === "available"
              ? `${chain.count}${chain.capped ? "+" : ""} visible markets`
              : loading
                ? "Connecting…"
                : "Unavailable"}
          </span>
          <small>
            {chain.observedAt
              ? `latest ${Math.max(0, Math.floor(((now || Date.parse(feed!.checkedAt)) - Date.parse(chain.observedAt)) / 1000))}s ago`
              : "freshness —"}
          </small>
        </span>
      ))}
      <button
        className="feed-refresh"
        onClick={() => setRefresh((x) => x + 1)}
        disabled={loading}
        aria-label="Refresh recorded market data"
      >
        <RefreshCw size={12} />
        30s refresh
      </button>
    </section>
  );
}
