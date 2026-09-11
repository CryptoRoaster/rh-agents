# RH Agents console

Compact paper workspace based on `../docs/design/dashboard-reference` (the supplied PNG has no extension). Primary target: 1440–1920px desktop. Native Node.js development: `npm ci`, then `npm run dev`.

## Real recorded markets

Set server-only `MARKET_API_BASE_URL` in the frontend process environment or untracked `frontend/.env.local`. Default: `http://127.0.0.1:8000`.

GET `/api/market-feed` reads existing FastAPI `/api/markets` concurrently for Robinhood and BSC, filtered to GeckoTerminal, mainnet and `include_fixtures=false`. Four-second timeout, no caching/redirects, maximum 100 records per chain. No provider, ingestion or trading endpoints are invoked; no database credentials reach the frontend.

The status strip refreshes every 30 seconds and shows visible market counts and age of the latest observation. `100+` indicates the read limit, not an exact total. Empty successful reads show 0; failures or incompatible provenance show Unavailable with unknown freshness. Failed refreshes clear old counts. MarketReader remains responsible for availability/freshness policy; observation age is not service-health telemetry.

## Fixtures and safety

Portfolio KPIs, equity, activity, agent metrics, analytics, exposure, trades, positions and policy values are labeled demo data. `lib/console-data.ts` declares fixture provenance and sections carry `data-source="demo"`. Existing `lib/demo.ts` supplies equity. Period buttons preview the same illustrative series on different time axes; they do not query historical performance.

PAPER / SIMULATED is the workspace presentation mode, not a backend mutation. OPERATIONAL describes the console, not backend/agent health. SENTINEL ACTIVE describes the paper policy boundary, not polled telemetry. Specialists remain planned. SENTINEL/LEDGER show demo health and validations instead of confidence. Live execution is disabled; signer is not configured. Policy controls are read-only, including a disabled kill switch. They live in the top-right Controls popover, not in the main dashboard grid. Escape, outside click or the close button dismisses the popover.

Network arrows show the planned workflow: specialist evidence through FUSE and COMMANDER to SENTINEL; only the PASS path reaches the paper executor and accounting. No signing, trade submission or backend architecture changes. Navigation scrolls to page sections. Notifications explicitly indicate their unconnected state.

## Structure and responsiveness

`components/console/`: Sidebar, TopBar, KpiCard, ActivityFeed, AgentNetwork, AgentCard, MarketAnalytics, RiskDistribution, RecentTrades, OpenPositions, GlobalControls, MarketFeedIndicator and shared badges/headings. Recharts loads on the client in `components/equity-chart.tsx`. `app/globals.css` centralizes colors, agent accents, spacing, radius and sidebar width.

Desktop: narrow sidebar, seven KPIs, three fixed top panels, and exactly two rows of five agent cards. Lower row A contains Market & Signal Analytics and Risk Distribution; row B contains Recent Trades and Open Positions. Below 1200px KPI cards wrap; the five-column, two-row agent grid scrolls locally. Tables have explicit horizontal scroll regions. Below 760px the sidebar contracts and main/lower panels stack.

## Local layout preferences

Lightweight Pointer Event drag handles and accessible earlier/later buttons reorder KPIs and all ten agent cards. The two lower panel pairs reorder only within their respective rows, preserving the analytics/trading grouping. The core top three panels remain fixed. Defaults preserve the documented agent order (ORBIT through SIGNAL, then ATLAS through COMMANDER).

Orders use versioned `rh-agents.layout.v1.*` localStorage keys, with in-session fallback if storage is blocked. Invalid, duplicate or obsolete IDs fall back to defaults; saved layout never omits a required tile. Changes in another tab synchronize through storage events. Controls → Reset layout restores all four groups. Preferences are purely local and never change backend settings or trading state.

## Checks

```sh
npm run typecheck
npm run lint
npm run format:check
npm run build
```

Browser verification covers desktop overflow, missing backend, agent filtering and chart period controls. HTTP contract simulation covers separate chain counts, empty responses, the 100-row cap, fixture rejection and provider-backend failure. Test responses do not establish live provider availability.
