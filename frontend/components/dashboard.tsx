"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import {
  Activity,
  ArrowDownLeft,
  ArrowRight,
  ArrowUpRight,
  ChartNoAxesCombined,
  ChevronRight,
  CircleHelp,
  Command,
  Cpu,
  FlaskConical,
  Gauge,
  GitBranch,
  LayoutDashboard,
  LockKeyhole,
  Network,
  Radio,
  ShieldCheck,
  SlidersHorizontal,
  Wallet,
  Zap,
} from "lucide-react";
import { agents, controls, demoTrades } from "@/lib/demo";
import type { AgentSummary, TradingMode } from "@/lib/types";

const EquityChart = dynamic(() => import("./equity-chart"), {
  ssr: false,
  loading: () => (
    <div className="equity-chart chart-loading">
      Loading illustrative equity…
    </div>
  ),
});
const currency = (value: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);

function AgentCard({ agent }: { agent: AgentSummary }) {
  const ready = agent.status === "PAPER_READY";
  return (
    <article className={`agent-card ${ready ? "ready" : ""}`}>
      <div className="agent-card-head">
        <span className="agent-symbol">
          <Cpu size={16} />
        </span>
        <span className={`status-dot ${ready ? "green" : ""}`} />
        <span className="agent-status">
          {ready ? "Paper ready" : "Planned"}
        </span>
      </div>
      <h3>{agent.name}</h3>
      <p className="agent-role">{agent.role}</p>
      <p className="agent-activity">{agent.currentActivity}</p>
      <dl className="agent-metrics">
        <div>
          <dt>Health / confidence</dt>
          <dd>— / —</dd>
        </div>
        <div>
          <dt>Processed</dt>
          <dd>{agent.signalsProcessed}</dd>
        </div>
        <div>
          <dt>Approved / rejected</dt>
          <dd>
            {agent.approved} / {agent.rejected}
          </dd>
        </div>
        <div>
          <dt>Latency</dt>
          <dd>{agent.latencyMs ?? "—"}</dd>
        </div>
        <div>
          <dt>PnL contribution</dt>
          <dd>{agent.pnlContributionUsd ?? "—"}</dd>
        </div>
      </dl>
      <div className="last-action">
        Last action <span>{agent.lastAction ?? "No activity yet"}</span>
      </div>
    </article>
  );
}

export function Dashboard() {
  const [mode, setMode] = useState<TradingMode>("PAPER");
  const [filter, setFilter] = useState<"all" | "agent" | "service">("all");
  const filteredAgents = agents.filter(
    (agent) =>
      filter === "all" ||
      (filter === "agent" ? agent.kind === "agent" : agent.kind !== "agent"),
  );

  return (
    <div className="workspace">
      <a href="#main" className="skip-link">
        Skip to dashboard
      </a>
      <aside className="sidebar">
        <a className="brand" href="#overview">
          <span className="brand-mark">
            <GitBranch size={24} strokeWidth={2.4} />
          </span>
          <span>
            rh<span className="brand-light">/</span>agents
            <small>CRYPTO ROASTER</small>
          </span>
        </a>
        <div className="workspace-label">
          WORKSPACE <span>PHASE 0</span>
        </div>
        <nav aria-label="Main navigation">
          <a className="nav-link active" href="#overview">
            <LayoutDashboard size={17} />
            Overview
          </a>
          <a className="nav-link" href="#positions">
            <Wallet size={17} />
            Portfolio
          </a>
          <a className="nav-link" href="#agents">
            <Network size={17} />
            Agent network<span className="nav-count">11</span>
          </a>
          <a className="nav-link" href="#trades">
            <ChartNoAxesCombined size={17} />
            Trade history
          </a>
          <a className="nav-link" href="#risk">
            <ShieldCheck size={17} />
            Risk center
          </a>
          <a className="nav-link" href="#controls">
            <SlidersHorizontal size={17} />
            Global controls
          </a>
        </nav>
        <div className="sidebar-bottom">
          <div className="sandbox-card">
            <FlaskConical size={19} />
            <strong>Built for autonomy.</strong>
            <p>Starting with a safe place to test.</p>
            <span>
              Paper environment <ArrowUpRight size={13} />
            </span>
          </div>
          <a className="help-link" href="#foundation-note">
            <CircleHelp size={16} />
            About this foundation
          </a>
          <div className="profile">
            <span>CR</span>
            <div>
              CryptoRoaster<small>Local workspace</small>
            </div>
            <Command size={15} />
          </div>
        </div>
      </aside>

      <div className="main-wrap">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace <ChevronRight size={13} />
            <span>Overview</span>
          </div>
          <div className="topbar-right">
            <span className="environment-dot" />
            Local development
            <span className="topbar-divider" />
            <span className="phase-chip">Phase 0</span>
          </div>
        </header>
        <main id="main">
          <section id="overview" className="page-heading">
            <div>
              <div className="eyebrow">YOUR AUTONOMOUS TRADING WORKSPACE</div>
              <h1>
                Trading overview<span>.</span>
              </h1>
              <p>A clear view of your portfolio, agents, and risk.</p>
            </div>
            <div className="heading-status">
              <FlaskConical size={16} />
              Paper sandbox
            </div>
          </section>
          <div className="demo-banner">
            <span>
              <FlaskConical size={15} />
              <strong>Dashboard preview</strong> · All portfolio values are
              illustrative demo data.
            </span>
            <span>No live funds. No connected feeds.</span>
          </div>

          <section className="stats-grid" aria-label="Portfolio summary">
            <article className="stat">
              <div className="stat-label">
                Total Balance <Wallet size={16} />
              </div>
              <strong>
                $10,128<span>.40</span>
              </strong>
              <div className="stat-foot">
                <span className="positive">↗ 1.28%</span> from demo starting
                balance
              </div>
            </article>
            <article className="stat">
              <div className="stat-label">
                Total PnL <ChartNoAxesCombined size={16} />
              </div>
              <strong className="positive">
                +$128<span>.40</span>
              </strong>
              <div className="stat-foot">
                Realized + unrealized <span className="mini-badge">DEMO</span>
              </div>
            </article>
            <article className="stat">
              <div className="stat-label">
                Win Rate <Gauge size={16} />
              </div>
              <strong>
                66.7<span>%</span>
              </strong>
              <div className="stat-foot">
                8 wins <span className="dot-separator">·</span> 12 illustrative
                closes
              </div>
            </article>
            <article className="stat">
              <div className="stat-label">
                Active Positions <Activity size={16} />
              </div>
              <strong>02</strong>
              <div className="stat-foot">
                <span className="positive">$200.00</span> illustrative exposure
              </div>
            </article>
          </section>
          <div className="status-strip">
            <div>
              <span className="status-icon">
                <Radio size={15} />
              </span>
              Approved Signals
              <strong>
                03 <small>demo</small>
              </strong>
            </div>
            <div>
              <span className="status-icon">
                <FlaskConical size={15} />
              </span>
              Trading Mode
              <strong>
                {mode === "PAPER" ? "Paper" : "Observe"} <small>preview</small>
              </strong>
            </div>
            <div>
              <span className="status-icon">
                <ShieldCheck size={15} />
              </span>
              Daily Risk Used<strong>8.4%</strong>
              <span className="inline-meter">
                <i />
              </span>
              <small>$42 / $500 · demo</small>
            </div>
          </div>

          <div className="chart-row">
            <section className="panel portfolio-panel">
              <div className="panel-heading">
                <div>
                  <h2>Portfolio Equity</h2>
                  <p>Illustrative performance · USD</p>
                </div>
                <span className="period-chip">24 hours</span>
              </div>
              <div className="equity-summary">
                <strong>$10,128.40</strong>
                <span className="positive">↗ +$128.40 (1.28%)</span>
                <span className="chart-legend">
                  <i />
                  Paper portfolio
                </span>
              </div>
              <EquityChart />
            </section>
            <section className="panel activity-panel">
              <div className="panel-heading">
                <h2>Activity Feed</h2>
                <span className="mini-badge">DEMO</span>
              </div>
              <div className="feed-item">
                <span className="feed-icon mint">
                  <ShieldCheck size={16} />
                </span>
                <div>
                  <strong>Risk check passed</strong>
                  <p>SENTINEL · Within paper limits</p>
                  <small>Illustrative event · 22:00</small>
                </div>
              </div>
              <div className="feed-item">
                <span className="feed-icon blue">
                  <ArrowDownLeft size={16} />
                </span>
                <div>
                  <strong>Paper order filled</strong>
                  <p>EXECUTOR · NOVA / USD</p>
                  <small>Illustrative event · 21:48</small>
                </div>
              </div>
              <div className="feed-item">
                <span className="feed-icon cream">
                  <Zap size={16} />
                </span>
                <div>
                  <strong>Opportunity detected</strong>
                  <p>ORBIT · Example market signal</p>
                  <small>Illustrative event · 21:47</small>
                </div>
              </div>
              <div className="feed-note">
                <span className="status-dot" />
                Live activity will appear in Phase 1
              </div>
            </section>
          </div>

          <section id="agents" className="panel network-panel">
            <div className="panel-heading">
              <div>
                <h2>Agent Network</h2>
                <p>Autonomous decisions. Deterministic safeguards.</p>
              </div>
              <span className="quiet-label">8 agents · 3 services</span>
            </div>
            <div className="pipeline" aria-label="Trading pipeline">
              <span className="pipeline-feed">
                <Radio size={16} />
                Data feeds
              </span>
              <ArrowRight className="pipeline-arrow" size={15} />
              <span>ORBIT</span>
              <ArrowRight className="pipeline-arrow" size={15} />
              <span className="pipeline-stack">
                ATLAS · SIGNAL
                <br />
                VECTOR
              </span>
              <ArrowRight className="pipeline-arrow" size={15} />
              <span>PULSE</span>
              <ArrowRight className="pipeline-arrow" size={15} />
              <span>ANCHOR</span>
              <ArrowRight className="pipeline-arrow" size={15} />
              <span className="pipeline-risk">
                <ShieldCheck size={13} />
                SENTINEL
              </span>
              <ArrowRight className="pipeline-arrow" size={15} />
              <span>FUSE</span>
              <ArrowRight className="pipeline-arrow" size={15} />
              <span>COMMANDER</span>
              <ArrowRight className="pipeline-arrow" size={15} />
              <span className="pipeline-risk">EXECUTOR</span>
              <ArrowRight className="pipeline-arrow" size={15} />
              <span>LEDGER</span>
            </div>
            <div className="network-note">
              <LockKeyhole size={13} />
              Every final intent is rechecked by SENTINEL before execution.
              Agents cannot override risk.
            </div>
          </section>

          <section className="agent-section">
            <div className="section-heading">
              <div>
                <h2>Agent Overview</h2>
                <p>Foundation status · no agents are running yet</p>
              </div>
              <div className="segmented" aria-label="Filter components">
                {(
                  [
                    ["all", "All components"],
                    ["agent", "Agents"],
                    ["service", "Services"],
                  ] as const
                ).map(([value, label]) => (
                  <button
                    key={value}
                    aria-pressed={filter === value}
                    onClick={() => setFilter(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <div className="agent-grid">
              {filteredAgents.map((agent) => (
                <AgentCard key={agent.name} agent={agent} />
              ))}
            </div>
          </section>

          <div className="lower-row">
            <section id="risk" className="panel risk-panel">
              <div className="panel-heading">
                <h2>Risk Distribution</h2>
                <span className="mini-badge">DEMO</span>
              </div>
              <div className="risk-content">
                <div
                  className="risk-donut"
                  role="img"
                  aria-label="Demo allocation: 98 percent cash, 1.2 percent ORION, 0.8 percent NOVA"
                >
                  <div>
                    <strong>
                      2.0<span>%</span>
                    </strong>
                    <small>deployed</small>
                  </div>
                </div>
                <div className="risk-legend">
                  <div>
                    <i className="cash-dot" />
                    Cash reserve<strong>98.0%</strong>
                  </div>
                  <div>
                    <i className="orion-dot" />
                    ORION<strong>1.2%</strong>
                  </div>
                  <div>
                    <i className="nova-dot" />
                    NOVA<strong>0.8%</strong>
                  </div>
                  <p>Illustrative allocation only</p>
                </div>
              </div>
            </section>
            <section id="controls" className="panel controls-panel">
              <div className="panel-heading">
                <div>
                  <h2>Global Controls</h2>
                  <p>Policy preview · controls are read-only</p>
                </div>
                <LockKeyhole size={16} />
              </div>
              <div className="control-mode">
                <span>Trading mode</span>
                <div className="segmented">
                  <button
                    aria-pressed={mode === "OBSERVE"}
                    onClick={() => setMode("OBSERVE")}
                  >
                    OBSERVE
                  </button>
                  <button
                    aria-pressed={mode === "PAPER"}
                    onClick={() => setMode("PAPER")}
                  >
                    PAPER
                  </button>
                  <button
                    disabled
                    title="Live trading is unavailable in Phase 0"
                  >
                    <LockKeyhole size={10} />
                    LIVE AUTONOMOUS
                  </button>
                </div>
              </div>
              <div className="control-values">
                <div>
                  <span>Max Exposure</span>
                  <strong>{currency(controls.maxExposureUsd)}</strong>
                </div>
                <div>
                  <span>Max Position Size</span>
                  <strong>{currency(controls.maxPositionSizeUsd)}</strong>
                </div>
                <div>
                  <span>Max Slippage</span>
                  <strong>{controls.maxSlippageBps / 100}%</strong>
                </div>
                <div>
                  <span>Daily Loss Limit</span>
                  <strong>{currency(controls.dailyLossLimitUsd)}</strong>
                </div>
              </div>
              <div className="kill-control">
                <span>
                  <strong>Kill Switch</strong>
                  <small>
                    Backend pause control will be connected in Phase 1.
                  </small>
                </span>
                <button disabled>Pause system</button>
              </div>
              <p className="control-notice" role="status">
                {mode} preview selected. This does not change backend mode or
                risk policy.
              </p>
            </section>
          </div>

          <section id="trades" className="panel table-panel">
            <div className="panel-heading">
              <div>
                <h2>Recent Trades</h2>
                <p>Simulated examples · not ledger records</p>
              </div>
              <span className="mini-badge">DEMO</span>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Asset / pair</th>
                    <th>Side</th>
                    <th>Quantity</th>
                    <th>Fill price</th>
                    <th>Fees</th>
                    <th>Realized PnL</th>
                    <th>Status</th>
                    <th>Time · UTC</th>
                  </tr>
                </thead>
                <tbody>
                  {demoTrades.map((trade) => (
                    <tr key={trade.id}>
                      <td>
                        <span
                          className={`asset-avatar ${trade.asset === "NOVA" ? "mint" : "blue"}`}
                        >
                          {trade.asset[0]}
                        </span>
                        <strong>{trade.asset}</strong>
                        <span className="pair"> / USD</span>
                      </td>
                      <td>
                        <span
                          className={`side-tag ${trade.side.toLowerCase()}`}
                        >
                          {trade.side}
                        </span>
                      </td>
                      <td>{trade.quantity}</td>
                      <td>{trade.price}</td>
                      <td>{trade.fees}</td>
                      <td
                        className={trade.pnl.startsWith("+") ? "positive" : ""}
                      >
                        {trade.pnl}
                      </td>
                      <td>
                        <span className="filled-status">
                          <i />
                          Paper fill
                        </span>
                      </td>
                      <td className="muted">{trade.time}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section id="positions" className="panel table-panel">
            <div className="panel-heading">
              <div>
                <h2>
                  Open Positions <span className="count-chip">2</span>
                </h2>
                <p>Illustrative holdings · not connected to the database</p>
              </div>
              <span className="mini-badge">DEMO</span>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Asset</th>
                    <th>Quantity</th>
                    <th>Average entry</th>
                    <th>Mark price</th>
                    <th>Market value</th>
                    <th>Unrealized PnL</th>
                    <th>Mode</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>
                      <span className="asset-avatar blue">O</span>
                      <strong>ORION</strong>
                    </td>
                    <td>24.00</td>
                    <td>$4.16</td>
                    <td>$5.00</td>
                    <td>$120.00</td>
                    <td className="positive">+$20.16</td>
                    <td>Paper demo</td>
                  </tr>
                  <tr>
                    <td>
                      <span className="asset-avatar mint">N</span>
                      <strong>NOVA</strong>
                    </td>
                    <td>8.00</td>
                    <td>$9.60</td>
                    <td>$10.00</td>
                    <td>$80.00</td>
                    <td className="positive">+$3.20</td>
                    <td>Paper demo</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
          <footer id="foundation-note">
            <span>
              <ShieldCheck size={14} />
              Phase 0 foundation · Paper execution only
            </span>
            <span>
              No signing infrastructure. All safety checks fail closed.
            </span>
          </footer>
        </main>
      </div>
    </div>
  );
}
