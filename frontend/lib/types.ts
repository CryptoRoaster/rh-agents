export type TradingMode = "OBSERVE" | "PAPER" | "LIVE_AUTONOMOUS";
export type AgentStatus = "PLANNED" | "PAPER_READY" | "OFFLINE";

export interface AgentSummary {
  name: string;
  role: string;
  kind: "agent" | "deterministic_service" | "infrastructure";
  status: AgentStatus;
  health: number | null;
  confidence: number | null;
  currentActivity: string;
  lastAction: string | null;
  signalsProcessed: number;
  approved: number;
  rejected: number;
  latencyMs: number | null;
  pnlContributionUsd: number | null;
}

export interface GlobalControls {
  killSwitch: boolean;
  maxExposureUsd: number;
  maxPositionSizeUsd: number;
  maxSlippageBps: number;
  dailyLossLimitUsd: number;
}

export interface ExecutionObservability {
  correlationId: string;
  detectedAt: string;
  decisionAt: string | null;
  riskApprovedAt: string | null;
  executionRequestedAt: string | null;
  txSignedAt: string | null;
  txSentAt: string | null;
  txConfirmedAt: string | null;
  signalPrice: string;
  quotePrice: string | null;
  executionPrice: string | null;
  estimatedSlippageBps: string | null;
  realizedSlippageBps: string | null;
  feesUsd: string | null;
  gasUsd: string | null;
  executionLatencyMs: number | null;
}
