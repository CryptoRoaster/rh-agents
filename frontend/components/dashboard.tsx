"use client";
import dynamic from "next/dynamic";
import { ShieldCheck, CircleHelp } from "lucide-react";
import { SortableGroup } from "./console/sortable";
import { Sidebar } from "./console/sidebar";
import { TopBar } from "./console/top-bar";
import { MarketFeedIndicator } from "./console/market-feed";
import { KpiRow } from "./console/kpi-card";
import { ActivityFeed } from "./console/activity-feed";
import { AgentNetwork } from "./console/agent-network";
import { AgentOverview } from "./console/agent-card";
import {
  MarketAnalytics,
  RiskDistribution,
  RecentTrades,
  OpenPositions,
} from "./console/lower-panels";
const EquityChart = dynamic(() => import("./equity-chart"), {
  ssr: false,
  loading: () => (
    <section className="panel chart-loading">
      Loading paper equity chart…
    </section>
  ),
});
export function Dashboard() {
  return (
    <div className="workspace">
      <a className="skip-link" href="#dashboard">
        Skip to dashboard
      </a>
      <Sidebar />
      <div className="main-wrap">
        <TopBar />
        <main id="dashboard">
          <div className="demo-banner">
            <span>
              <CircleHelp size={13} />
              <strong>Paper workspace</strong> · Portfolio, performance & agent
              activity are illustrative demo data.
            </span>
            <span>
              <ShieldCheck size={13} />
              SENTINEL: ACTIVE{" "}
              <span className="policy-note">
                paper policy · not runtime telemetry
              </span>
            </span>
          </div>
          <KpiRow />
          <div className="top-grid">
            <EquityChart />
            <ActivityFeed />
            <AgentNetwork />
          </div>
          <AgentOverview />
          <div className="lower-dashboard">
            <SortableGroup
              group="analytics"
              className="lower-row analytics-row"
              label="Reorder analytics panels"
              items={[
                {
                  id: "analytics",
                  label: "Market & Signal Analytics",
                  content: <MarketAnalytics />,
                },
                {
                  id: "risk",
                  label: "Risk Distribution",
                  content: <RiskDistribution />,
                },
              ]}
            />
            <SortableGroup
              group="trading"
              className="lower-row trading-row"
              label="Reorder trading panels"
              items={[
                {
                  id: "trades",
                  label: "Recent Trades",
                  content: <RecentTrades />,
                },
                {
                  id: "positions",
                  label: "Open Positions",
                  content: <OpenPositions />,
                },
              ]}
            />
          </div>
          <MarketFeedIndicator />
          <footer>
            <span>
              <ShieldCheck size={12} /> Real market data / simulated execution ·
              Live execution disabled
            </span>
            <span>
              rh/agents <span className="footer-divider">/</span> Evidence
              before execution.
            </span>
          </footer>
        </main>
      </div>
    </div>
  );
}
