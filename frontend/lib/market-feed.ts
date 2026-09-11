export type MarketChain = "robinhood" | "bsc";
export interface ChainFeed {
  chain: MarketChain;
  status: "available" | "unavailable";
  count: number | null;
  capped: boolean;
  observedAt: string | null;
}
export interface MarketFeed {
  provider: "geckoterminal";
  checkedAt: string;
  chains: ChainFeed[];
}
export const emptyChains: ChainFeed[] = ["robinhood", "bsc"].map((chain) => ({
  chain: chain as MarketChain,
  status: "unavailable",
  count: null,
  capped: false,
  observedAt: null,
}));
