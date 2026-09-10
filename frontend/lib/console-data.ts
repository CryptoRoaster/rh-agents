// Every value in this module is an illustrative fixture, never ledger/provider state.
export const fixtureMetadata = {
  source: "demo",
  execution: "simulated",
  persisted: false,
} as const;
export type Tone =
  "blue" | "green" | "red" | "orange" | "purple" | "cyan" | "pink" | "slate";
export type AgentName =
  | "ORBIT"
  | "SENTINEL"
  | "VECTOR"
  | "PULSE"
  | "SIGNAL"
  | "ATLAS"
  | "ANCHOR"
  | "FUSE"
  | "LEDGER"
  | "COMMANDER";
export interface ConsoleAgent {
  name: AgentName;
  role: string;
  tone: Tone;
  kind: "specialist" | "service" | "control";
  metric: number;
  processed: number;
  accepted: number;
  rejected: number;
  action: string;
  age: string;
}
export const consoleAgents: ConsoleAgent[] = [
  {
    name: "ORBIT",
    role: "Scout",
    tone: "blue",
    kind: "specialist",
    metric: 92,
    processed: 142,
    accepted: 89,
    rejected: 53,
    action: "Scanned BSC pools",
    age: "2m",
  },
  {
    name: "SENTINEL",
    role: "Risk Firewall",
    tone: "red",
    kind: "service",
    metric: 100,
    processed: 98,
    accepted: 76,
    rejected: 22,
    action: "Liquidity rejected",
    age: "5m",
  },
  {
    name: "VECTOR",
    role: "Trade Setup",
    tone: "orange",
    kind: "specialist",
    metric: 94,
    processed: 64,
    accepted: 47,
    rejected: 17,
    action: "Updated entry rules",
    age: "7m",
  },
  {
    name: "PULSE",
    role: "Trigger Monitor",
    tone: "green",
    kind: "specialist",
    metric: 90,
    processed: 71,
    accepted: 57,
    rejected: 14,
    action: "Entry level reached",
    age: "4m",
  },
  {
    name: "SIGNAL",
    role: "Sentiment",
    tone: "purple",
    kind: "specialist",
    metric: 86,
    processed: 120,
    accepted: 52,
    rejected: 68,
    action: "Flagged divergence",
    age: "6m",
  },
  {
    name: "ATLAS",
    role: "On-chain Intelligence",
    tone: "cyan",
    kind: "specialist",
    metric: 91,
    processed: 66,
    accepted: 49,
    rejected: 17,
    action: "Revalidation requested",
    age: "3m",
  },
  {
    name: "ANCHOR",
    role: "Liquidity / Execution",
    tone: "orange",
    kind: "specialist",
    metric: 93,
    processed: 58,
    accepted: 46,
    rejected: 12,
    action: "Sized paper position",
    age: "5m",
  },
  {
    name: "FUSE",
    role: "Evidence Fusion",
    tone: "pink",
    kind: "specialist",
    metric: 89,
    processed: 41,
    accepted: 37,
    rejected: 4,
    action: "Evidence complete",
    age: "8m",
  },
  {
    name: "LEDGER",
    role: "Accounting",
    tone: "slate",
    kind: "service",
    metric: 100,
    processed: 36,
    accepted: 36,
    rejected: 0,
    action: "Paper fill reconciled",
    age: "10m",
  },
  {
    name: "COMMANDER",
    role: "Orchestration",
    tone: "purple",
    kind: "control",
    metric: 95,
    processed: 28,
    accepted: 25,
    rejected: 3,
    action: "Trade case → READY",
    age: "1m",
  },
];
export type ActivityStatus = "INFO" | "SUCCESS" | "WARNING" | "REJECT";
export const activity: {
  time: string;
  agent: AgentName;
  message: string;
  status: ActivityStatus;
}[] = [
  {
    time: "14:48",
    agent: "ORBIT",
    message: "Candidate discovered on BSC",
    status: "INFO",
  },
  {
    time: "14:46",
    agent: "ATLAS",
    message: "Holder evidence stale — revalidate",
    status: "WARNING",
  },
  {
    time: "14:44",
    agent: "SIGNAL",
    message: "Social hype diverges from on-chain flow",
    status: "WARNING",
  },
  {
    time: "14:41",
    agent: "VECTOR",
    message: "Entry conditions updated",
    status: "INFO",
  },
  {
    time: "14:38",
    agent: "PULSE",
    message: "Entry level reached",
    status: "INFO",
  },
  {
    time: "14:35",
    agent: "ANCHOR",
    message: "Safe position size calculated",
    status: "SUCCESS",
  },
  {
    time: "14:32",
    agent: "SENTINEL",
    message: "Insufficient exit liquidity",
    status: "REJECT",
  },
  {
    time: "14:29",
    agent: "FUSE",
    message: "Evidence package complete",
    status: "SUCCESS",
  },
  {
    time: "14:27",
    agent: "COMMANDER",
    message: "Trade case moved to READY",
    status: "INFO",
  },
  {
    time: "14:24",
    agent: "LEDGER",
    message: "Paper position reconciled",
    status: "SUCCESS",
  },
];
export const positions = [
  {
    pair: "WBNB / USDT",
    chain: "BSC",
    size: "$1,250",
    pnl: "+$142.12",
    roi: "+11.37%",
  },
  {
    pair: "WRH / USDC",
    chain: "RH",
    size: "$980",
    pnl: "−$27.21",
    roi: "−2.78%",
  },
  {
    pair: "CAKE / USDT",
    chain: "BSC",
    size: "$750",
    pnl: "+$68.33",
    roi: "+9.11%",
  },
  {
    pair: "DEMO / USDC",
    chain: "RH",
    size: "$420",
    pnl: "+$31.16",
    roi: "+7.42%",
  },
];
export const trades = positions.map((p, i) => ({
  ...p,
  time: ["14:24", "14:18", "14:02", "13:56"][i],
  side: i === 1 ? "SELL" : "BUY",
}));
