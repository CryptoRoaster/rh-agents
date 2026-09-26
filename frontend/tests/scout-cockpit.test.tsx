import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { ScoutCockpit } from "@/components/scout/cockpit";

const NOW = Date.parse("2026-09-26T20:00:00Z");
const WATCH_ID = "0f6b1e2a-3c4d-4e5f-8a9b-0c1d2e3f4a5b";

function watch(overrides: Record<string, unknown> = {}) {
  return {
    watch_id: WATCH_ID,
    provider: "geckoterminal",
    chain: "bsc",
    network: "mainnet",
    pair_id: "bsc:mainnet:contract_address:0x" + "ac".repeat(20),
    venue: "four-meme",
    base_asset_id: "bsc:mainnet:0x" + "ac".repeat(20),
    quote_asset_id: "bsc:mainnet:0x" + "0".repeat(40),
    is_fixture: false,
    first_seen_at: "2026-09-26T19:57:00Z",
    last_seen_at: "2026-09-26T19:57:00Z",
    age_seconds: 180,
    status: "WATCHING",
    reason_code: "WATCH_OPENED",
    next_orbit_review_at: "2026-09-26T20:57:00Z",
    orbit_checkpoint_index: 0,
    next_history_review_at: "2026-09-27T19:57:00Z",
    latest_vector_sufficiency: null,
    vector_checked_at: null,
    last_promoted_trade_case_id: null,
    latest_snapshot: {
      snapshot_id: "1",
      observed_at: "2026-09-26T19:57:00Z",
      base_symbol: "MEME",
      quote_symbol: "BNB",
      price_usd: "0.0000042",
      price_status: "AVAILABLE",
      liquidity_usd: null,
      liquidity_status: "UNKNOWN",
      volume_usd: "10",
      volume_status: "AVAILABLE",
      volume_window_seconds: 86400,
    },
    latest_assessment: {
      assessed_at: "2026-09-26T19:57:05Z",
      checkpoint_seconds: 0,
      status: "COMPLETED",
      classification: "NOT_INTERESTING",
      strength: "WEAK",
    },
    assessments: 1,
    ...overrides,
  };
}

const run = {
  run: {
    id: "run-1",
    started_at: "2026-09-26T19:57:00Z",
    completed_at: "2026-09-26T19:57:12Z",
    status: "COMPLETED",
    stop: "COMPLETED",
    errors: [],
    policy_version: "early-scout-v1",
    discovered: 10,
    valid_markets: 10,
    provider_identity_rejects: 0,
    other_provider_rejects: 0,
    watches_created: 1,
    watches_updated: 0,
    bootstrapped: 0,
    refreshed: 0,
    watches_due_orbit: 4,
    orbit_reviews_started: 3,
    orbit_reviews_completed: 3,
    interesting: 0,
    not_interesting: 3,
    insufficient_data: 0,
    watches_due_history: 0,
    history_checks: 0,
    vector_sufficient: 0,
    promotable_new: 0,
    dormant_new: 0,
    retired_new: 0,
    provider_failures: 0,
    model_failures: 0,
    provider_requests: 3,
    orbit_backlog_before: 4,
    orbit_backlog_after: 1,
    oldest_orbit_due_age_seconds: 5400,
    new_watches_without_orbit_assessment: 1,
  },
  duration_seconds: 12,
  identity_acceptance_rate: 1,
  watch_creation_rate: 0.1,
};

const overview = {
  as_of: "2026-09-26T20:00:00Z",
  policy_version: "early-scout-v1",
  watches: 2,
  by_status: { WATCHING: 1, PROMOTABLE: 1, DORMANT: 0, RETIRED: 0 },
  orbit_backlog: 1,
  oldest_orbit_due_age_seconds: 5400,
  unreviewed_watches: 1,
  latest_run: run,
  very_young_seconds: 21600,
};

const detail = {
  watch: watch(),
  checkpoints: [
    {
      checkpoint_index: 0,
      checkpoint_seconds: 0,
      due_at: "2026-09-26T19:57:00Z",
      state: "ASSESSED",
      assessment_id: "a0",
    },
    {
      checkpoint_index: 1,
      checkpoint_seconds: 3600,
      due_at: "2026-09-26T20:57:00Z",
      state: "FAILED",
      assessment_id: "a1",
    },
    {
      checkpoint_index: 2,
      checkpoint_seconds: 10800,
      due_at: "2026-09-26T22:57:00Z",
      state: "PENDING",
      assessment_id: null,
    },
  ],
  assessments: [
    {
      id: "a0",
      watch_id: WATCH_ID,
      snapshot_id: "1",
      assessed_at: "2026-09-26T19:57:05Z",
      checkpoint_index: 0,
      checkpoint_seconds: 0,
      status: "COMPLETED",
      failure_reason: null,
      classification: "NOT_INTERESTING",
      strength: "WEAK",
      reason_codes: ["PRICE_AVAILABLE", "LIQUIDITY_UNKNOWN"],
      data_gaps: ["LIQUIDITY_UNKNOWN"],
      cited_observation_ids: [],
      summary: "Price observed; liquidity unknown.",
      input_digest: "d",
      policy_version: "early-scout-v1",
      prompt_version: "orbit-v1",
      prompt_hash: "h",
      output_schema_version: 1,
      reasoning_provider: "anthropic",
      reasoning_model: "claude-opus-5",
      input_tokens: 2700,
      output_tokens: 400,
      latency_ms: 9000,
    },
    {
      id: "a1",
      watch_id: WATCH_ID,
      snapshot_id: "2",
      assessed_at: "2026-09-26T20:58:00Z",
      checkpoint_index: 1,
      checkpoint_seconds: 3600,
      status: "FAILED",
      failure_reason: "PROVIDER_TIMEOUT",
      classification: null,
      strength: null,
      reason_codes: [],
      data_gaps: [],
      cited_observation_ids: [],
      summary: null,
      input_digest: "d",
      policy_version: "early-scout-v1",
      prompt_version: "orbit-v1",
      prompt_hash: "h",
      output_schema_version: 1,
      reasoning_provider: null,
      reasoning_model: null,
      input_tokens: null,
      output_tokens: null,
      latency_ms: null,
    },
  ],
  trade_case: null,
};

const emptyPortfolio = {
  account: {
    cash_usd: "10000",
    initial_cash_usd: "10000",
    fees_paid_usd: "0",
    realized_loss_today_usd: "0",
    paused: false,
  },
  positions: [],
  fills: [],
  pnl: [],
};

function respond(routes: Record<string, unknown>, failing: string[] = []) {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  return vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    const key = Object.keys(routes).find((prefix) => url.includes(prefix));
    if (failing.some((prefix) => url.includes(prefix)) || key === undefined)
      return new Response("{}", { status: 503 });
    return new Response(JSON.stringify(routes[key]), { status: 200 });
  });
}

const routes = {
  "scout/overview": overview,
  "scout/watches?": {
    items: [
      watch(),
      watch({
        watch_id: "b",
        status: "PROMOTABLE",
        age_seconds: 30 * 3600,
        latest_assessment: null,
      }),
    ],
    total: 2,
    limit: 100,
    offset: 0,
  },
  [`scout/watches/${WATCH_ID}`]: detail,
  "scout/runs": { items: [run], total: 1, limit: 20, offset: 0 },
  "paper/portfolio": emptyPortfolio,
};

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("early discovery cockpit", () => {
  it("renders the watch list with young ages and promotable state", async () => {
    vi.stubGlobal("fetch", respond(routes));
    render(<ScoutCockpit />);
    const table = await screen.findByRole("table", {
      name: /Discovery watches/,
    });
    expect(within(table).getByText("3m")).toBeTruthy();
    expect(within(table).getAllByText("MEME / BNB").length).toBe(2);
    expect(within(table).getByText("PROMOTABLE")).toBeTruthy();
    expect(within(table).getAllByText("unknown").length).toBe(2);
    expect(within(table).getByText("not yet")).toBeTruthy();
  });

  it("shows the backlog and run coverage", async () => {
    vi.stubGlobal("fetch", respond(routes));
    render(<ScoutCockpit />);
    expect(
      await screen.findByText(/1 ORBIT review pending · oldest due 1h 30m/),
    ).toBeTruthy();
    const runs = await screen.findByRole("table", { name: /Scout runs/ });
    expect(within(runs).getByText("100%")).toBeTruthy();
    expect(within(runs).getByText("4 → 1")).toBeTruthy();
  });

  it("opens the ORBIT timeline with completed, failed and pending checkpoints", async () => {
    vi.stubGlobal("fetch", respond(routes));
    render(<ScoutCockpit />);
    const table = await screen.findByRole("table", {
      name: /Discovery watches/,
    });
    fireEvent.click(within(table).getAllByRole("button")[0]);
    expect(
      await screen.findByText("Price observed; liquidity unknown."),
    ).toBeTruthy();
    expect(screen.getByText("Review failed: PROVIDER_TIMEOUT")).toBeTruthy();
    expect(screen.getByText(/no assessment yet/)).toBeTruthy();
    expect(screen.getByText(/never TradeCase evidence/)).toBeTruthy();
  });

  it("shows an empty paper ledger as empty", async () => {
    vi.stubGlobal("fetch", respond(routes));
    render(<ScoutCockpit />);
    expect(await screen.findByText("No paper positions yet.")).toBeTruthy();
    expect(screen.getByText("No P&L snapshot recorded.")).toBeTruthy();
  });

  it("shows a booked paper position", async () => {
    vi.stubGlobal(
      "fetch",
      respond({
        ...routes,
        "paper/portfolio": {
          ...emptyPortfolio,
          positions: [
            {
              position_id: "p1",
              asset_id: "bsc:mainnet:0x" + "ab".repeat(20),
              market_pair_id: null,
              market_chain: "bsc",
              quantity: "12.5",
              cost_basis_usd: "25",
              realized_pnl_usd: "0",
              open: true,
              created_at: "2026-09-26T19:00:00Z",
              updated_at: "2026-09-26T19:00:00Z",
            },
          ],
        },
      }),
    );
    render(<ScoutCockpit />);
    expect(await screen.findByText("12.5")).toBeTruthy();
  });

  it("reports an unavailable API instead of zeros", async () => {
    vi.stubGlobal("fetch", respond(routes, ["scout/overview", "scout/runs"]));
    render(<ScoutCockpit />);
    expect(await screen.findByText(/Scout status unavailable/)).toBeTruthy();
    expect(await screen.findByText(/Run history unavailable/)).toBeTruthy();
  });

  it("offers no trading or mutation control", async () => {
    const fetch = respond(routes);
    vi.stubGlobal("fetch", fetch);
    const { container } = render(<ScoutCockpit />);
    await screen.findByRole("table", { name: /Discovery watches/ });
    const labels = Array.from(
      container.querySelectorAll("button, a, input, select, form"),
    )
      .map(
        (node) =>
          (node.textContent ?? "") +
          " " +
          (node.getAttribute("aria-label") ?? ""),
      )
      .join(" ")
      .toLowerCase();
    for (const word of [
      "buy",
      "sell",
      "execute",
      "approve",
      "sign",
      "broadcast",
      "override",
      "trade now",
    ]) {
      expect(labels).not.toContain(word);
    }
    expect(container.querySelector("form")).toBeNull();
    for (const call of fetch.mock.calls) {
      const init = call[1] as RequestInit | undefined;
      expect(init?.method ?? "GET").toBe("GET");
    }
  });
});
