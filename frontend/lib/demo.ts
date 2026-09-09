import type { AgentSummary, GlobalControls } from "./types";

// Presentation fixtures only. These never represent persisted portfolio state.
export const controls: GlobalControls = {
  killSwitch: false,
  maxExposureUsd: 10000,
  maxPositionSizeUsd: 2500,
  maxSlippageBps: 100,
  dailyLossLimitUsd: 500,
};

const roles = [
  ["ORBIT", "Opportunity discovery", "agent"],
  ["ATLAS", "On-chain intelligence", "agent"],
  ["SIGNAL", "Sentiment & demand", "agent"],
  ["VECTOR", "Trade setup & targets", "agent"],
  ["PULSE", "Entry & exit triggers", "agent"],
  ["ANCHOR", "Liquidity & routing", "agent"],
  ["SENTINEL", "Deterministic risk", "deterministic_service"],
  ["FUSE", "Trade proposals", "agent"],
  ["COMMANDER", "Position orchestration", "agent"],
  ["LEDGER", "Accounting & reconciliation", "deterministic_service"],
  ["EXECUTOR", "Paper execution service", "infrastructure"],
] as const;

export const agents: AgentSummary[] = roles.map(([name, role, kind]) => ({
  name,
  role,
  kind,
  status: kind === "agent" ? "PLANNED" : "PAPER_READY",
  health: null,
  confidence: null,
  currentActivity:
    kind === "agent"
      ? "Awaiting Phase 1 integration"
      : "Available for local paper tests",
  lastAction: null,
  signalsProcessed: 0,
  approved: 0,
  rejected: 0,
  latencyMs: null,
  pnlContributionUsd: null,
}));

export const equity = [
  { time: "00:00", value: 10000 },
  { time: "02:00", value: 10012 },
  { time: "04:00", value: 10006 },
  { time: "06:00", value: 10037 },
  { time: "08:00", value: 10024 },
  { time: "10:00", value: 10058 },
  { time: "12:00", value: 10043 },
  { time: "14:00", value: 10091 },
  { time: "16:00", value: 10084 },
  { time: "18:00", value: 10112 },
  { time: "20:00", value: 10104 },
  { time: "22:00", value: 10128.4 },
];

export const demoTrades = [
  {
    id: "demo-003",
    asset: "NOVA",
    side: "SELL",
    quantity: "10.00",
    price: "$10.42",
    fees: "$0.10",
    pnl: "+$4.20",
    time: "22:00:18",
  },
  {
    id: "demo-002",
    asset: "NOVA",
    side: "BUY",
    quantity: "10.00",
    price: "$10.00",
    fees: "$0.10",
    pnl: "—",
    time: "21:48:32",
  },
  {
    id: "demo-001",
    asset: "ORION",
    side: "BUY",
    quantity: "24.00",
    price: "$4.16",
    fees: "$0.10",
    pnl: "—",
    time: "21:30:04",
  },
];
