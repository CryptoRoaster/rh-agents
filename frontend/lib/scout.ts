// Early-discovery cockpit: API shapes and pure presentation helpers.
// Read-only. Nothing here ranks markets or recommends a trade: a watch is an
// observation, and PROMOTABLE is not a buy approval.

export type Availability = "AVAILABLE" | "UNKNOWN" | "UNAVAILABLE";
export type WatchStatus = "WATCHING" | "PROMOTABLE" | "DORMANT" | "RETIRED";
export type Tone =
  "blue" | "green" | "red" | "orange" | "purple" | "cyan" | "pink" | "slate";

export type SnapshotView = {
  snapshot_id: string;
  observed_at: string;
  base_symbol: string;
  quote_symbol: string;
  price_usd: string | null;
  price_status: Availability;
  liquidity_usd: string | null;
  liquidity_status: Availability;
  volume_usd: string | null;
  volume_status: Availability;
  volume_window_seconds: number;
};

export type AssessmentBrief = {
  assessed_at: string;
  checkpoint_seconds: number;
  status: "COMPLETED" | "FAILED";
  classification: string | null;
  strength: string | null;
};

export type WatchView = {
  watch_id: string;
  provider: string;
  chain: string;
  network: string;
  pair_id: string;
  venue: string;
  base_asset_id: string;
  quote_asset_id: string;
  is_fixture: boolean;
  first_seen_at: string;
  last_seen_at: string;
  age_seconds: number;
  status: WatchStatus;
  reason_code: string;
  next_orbit_review_at: string | null;
  orbit_checkpoint_index: number | null;
  next_history_review_at: string | null;
  latest_vector_sufficiency: string | null;
  vector_checked_at: string | null;
  last_promoted_trade_case_id: string | null;
  latest_snapshot: SnapshotView | null;
  latest_assessment: AssessmentBrief | null;
  assessments: number;
};

export type WatchPage = {
  items: WatchView[];
  total: number;
  limit: number;
  offset: number;
};

export type WatchAssessment = {
  id: string;
  watch_id: string;
  snapshot_id: string;
  assessed_at: string;
  checkpoint_index: number;
  checkpoint_seconds: number;
  status: "COMPLETED" | "FAILED";
  failure_reason: string | null;
  failure_reason_code: string | null;
  classification: string | null;
  strength: string | null;
  reason_codes: string[];
  data_gaps: string[];
  cited_observation_ids: string[];
  summary: string | null;
  input_digest: string;
  policy_version: string;
  prompt_version: string;
  prompt_hash: string;
  output_schema_version: number;
  reasoning_provider: string | null;
  reasoning_model: string | null;
  reasoning_effort: string | null;
  reported_effort: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
};

export type CheckpointState =
  "ASSESSED" | "FAILED" | "COALESCED" | "DUE" | "PENDING" | "NOT_SCHEDULED";

export type CheckpointView = {
  checkpoint_index: number;
  checkpoint_seconds: number;
  due_at: string;
  state: CheckpointState;
  assessment_id: string | null;
};

// A JEV shadow fast assessment. Shadow only: no trading or promotion effect.
export type FastAnswer =
  | { type: "noul"; noul: number }
  | {
      type: "choice";
      choice: string;
      probabilities: Record<string, number>;
      confidence: number;
    }
  | {
      type: "score";
      score: number;
      legend: Record<string, string>;
      probabilities: Record<string, number>;
      confidence: number;
    };

// Evidence about a discovered stream, not a watch: candidates the watch limit
// declined have these too. A watch shows the ones for its own stream.
export type FastAssessment = {
  id: string;
  market_provider: string;
  chain: string;
  network: string;
  pair_id: string;
  is_fixture: boolean;
  snapshot_id: string;
  reserved_at: string;
  assessed_at: string | null;
  status: "PENDING" | "COMPLETED" | "FAILED";
  provider: string;
  model: string;
  model_version: string | null;
  question_version: string;
  input_schema_version: number;
  input_digest: string;
  input_payload: Record<string, unknown>;
  answers: Record<string, FastAnswer> | null;
  latency_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  failure_category: string | null;
  failure_reason_code: string | null;
};

export type WatchDetail = {
  watch: WatchView;
  checkpoints: CheckpointView[];
  assessments: WatchAssessment[];
  fast_assessments: FastAssessment[];
  trade_case: {
    trade_case_id: string;
    status: string;
    opened_at: string;
  } | null;
};

export type ScoutRun = {
  id: string;
  started_at: string;
  completed_at: string;
  status: "COMPLETED" | "STOPPED" | "FAILED";
  stop: string;
  errors: string[];
  policy_version: string;
  discovered: number;
  valid_markets: number;
  provider_identity_rejects: number;
  other_provider_rejects: number;
  watches_created: number;
  watches_updated: number;
  bootstrapped: number;
  refreshed: number;
  watches_due_orbit: number;
  orbit_reviews_started: number;
  orbit_reviews_completed: number;
  interesting: number;
  not_interesting: number;
  insufficient_data: number;
  watches_due_history: number;
  history_checks: number;
  vector_sufficient: number;
  promotable_new: number;
  dormant_new: number;
  retired_new: number;
  provider_failures: number;
  model_failures: number;
  provider_requests: number;
  orbit_backlog_before: number;
  orbit_backlog_after: number;
  oldest_orbit_due_age_seconds: number | null;
  new_watches_without_orbit_assessment: number;
  orbit_daily_budget: number;
  orbit_daily_used_before: number;
  orbit_daily_remaining_before: number;
  orbit_daily_used_after: number;
  orbit_daily_remaining_after: number;
  model_failure_reasons: ModelFailureCount[];
};

// One provider failing one way in one run: codes only, never a message.
export type ModelFailureCount = {
  provider: string;
  category: string;
  reason_code: string;
  count: number;
};

export type RunView = {
  run: ScoutRun;
  duration_seconds: number;
  identity_acceptance_rate: number | null;
  watch_creation_rate: number | null;
};

export type RunPage = {
  items: RunView[];
  total: number;
  limit: number;
  offset: number;
};

export type ScoutOverview = {
  as_of: string;
  policy_version: string;
  watches: number;
  by_status: Record<WatchStatus, number>;
  orbit_backlog: number;
  oldest_orbit_due_age_seconds: number | null;
  unreviewed_watches: number;
  latest_run: RunView | null;
  very_young_seconds: number;
  orbit_daily_budget: number;
  orbit_daily_used: number;
  orbit_daily_remaining: number;
  // EARLY_SCOUT_V2: review debt closed without a model call, kept apart from "due".
  orbit_first_reviews_skipped_stale?: number;
  orbit_follow_ups_deferred?: number;
  orbit_reviewed?: number;
  // VECTOR history fair queue.
  history_due?: number;
  history_eligible_now?: number;
  history_in_backoff?: number;
  oldest_history_due_age_seconds?: number | null;
};

export type PositionView = {
  position_id: string;
  asset_id: string;
  market_pair_id: string | null;
  market_chain: string | null;
  quantity: string;
  cost_basis_usd: string;
  realized_pnl_usd: string;
  open: boolean;
  created_at: string;
  updated_at: string;
};

export type FillView = {
  side: "BUY" | "SELL";
  trade_case_id: string;
  execution_id: string;
  quantity: string;
  execution_price_usd: string;
  notional_usd: string;
  fees_usd: string;
  realized_pnl_usd: string | null;
  filled_at: string;
  mode: "PAPER";
};

export type PnLView = {
  recorded_at: string;
  snapshot: {
    cash_usd: string;
    market_value_usd: string;
    equity_usd: string;
    realized_pnl_usd: string;
    unrealized_pnl_usd: string;
    total_pnl_usd: string;
    fees_paid_usd: string;
  };
};

export type PaperPortfolio = {
  account: {
    cash_usd: string;
    initial_cash_usd: string;
    fees_paid_usd: string;
    realized_loss_today_usd: string;
    paused: boolean;
  } | null;
  positions: PositionView[];
  fills: FillView[];
  pnl: PnLView[];
};

// A UI filter only. It changes what is shown, never what the scout does.
export const VERY_YOUNG_SECONDS = 6 * 3600;

export const CHECKPOINT_LABELS = [
  "T+0",
  "T+1h",
  "T+3h",
  "T+6h",
  "T+12h",
  "T+24h",
];

export type ListFilter =
  "ALL" | "VERY_YOUNG" | "WATCHING" | "PROMOTABLE" | "DORMANT" | "RETIRED";

export const LIST_FILTERS: ListFilter[] = [
  "ALL",
  "VERY_YOUNG",
  "WATCHING",
  "PROMOTABLE",
  "DORMANT",
  "RETIRED",
];

export function filterQuery(filter: ListFilter): Record<string, string> {
  if (filter === "ALL") return {};
  if (filter === "VERY_YOUNG")
    return { max_age_seconds: String(VERY_YOUNG_SECONDS) };
  return { status: filter };
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds))
    return "—";
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

export function secondsUntil(iso: string | null, now: number): number | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  return Number.isFinite(at) ? Math.round((at - now) / 1000) : null;
}

export function formatRelative(iso: string | null, now: number): string {
  const delta = secondsUntil(iso, now);
  if (delta === null) return "—";
  return delta <= 0 ? "due now" : `in ${formatAge(delta)}`;
}

export function formatRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate))
    return "N/A";
  return `${Math.round(rate * 1000) / 10}%`;
}

// Decimals arrive as exact strings. Displayed compactly, never recomputed.
export function formatUsd(
  value: string | null,
  status: Availability = "AVAILABLE",
): string {
  if (status !== "AVAILABLE" || value === null) return status.toLowerCase();
  const number = Number(value);
  if (!Number.isFinite(number)) return value;
  if (number === 0) return "$0";
  const abs = Math.abs(number);
  if (abs >= 1_000_000) return `$${(number / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `$${(number / 1_000).toFixed(1)}k`;
  if (abs >= 1) return `$${number.toFixed(2)}`;
  return `$${number.toPrecision(3)}`;
}

export function pairLabel(watch: WatchView): string {
  const snapshot = watch.latest_snapshot;
  if (!snapshot) return shortId(watch.pair_id);
  return `${snapshot.base_symbol} / ${snapshot.quote_symbol}`;
}

export function shortId(value: string): string {
  const tail = value.split(":").pop() ?? value;
  return tail.length > 14 ? `${tail.slice(0, 6)}…${tail.slice(-4)}` : tail;
}

export function statusTone(status: WatchStatus): Tone {
  return (
    {
      WATCHING: "blue",
      PROMOTABLE: "green",
      DORMANT: "slate",
      RETIRED: "red",
    } as const
  )[status];
}

export function classificationTone(value: string | null): Tone {
  if (value === "INTERESTING") return "purple";
  if (value === "INSUFFICIENT_DATA") return "orange";
  return "slate";
}

export function checkpointTone(state: CheckpointState): Tone {
  return (
    {
      ASSESSED: "green",
      FAILED: "red",
      COALESCED: "slate",
      DUE: "orange",
      PENDING: "blue",
      NOT_SCHEDULED: "slate",
    } as const
  )[state];
}

export function backlogLabel(
  backlog: number,
  oldestSeconds: number | null,
): string {
  if (backlog <= 0) return "ORBIT queue caught up";
  const noun = backlog === 1 ? "review" : "reviews";
  return `${backlog} ORBIT ${noun} pending · oldest due ${formatAge(oldestSeconds)}`;
}

// Calls and budget only. No dollar estimate is shown without a real price basis.
export function dailyBudgetLabel(used: number, cap: number): string {
  if (cap > 0 && used >= cap) return "Daily ORBIT budget reached";
  return `${Math.max(0, cap - used)} of ${cap} remaining today`;
}

export async function readJson<T>(path: string): Promise<T> {
  const response = await fetch(`/api/cockpit/${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}
