import {
  ShieldCheck,
  SlidersHorizontal,
  LockKeyhole,
  Power,
  TrendingUp,
} from "lucide-react";
import { trades, positions } from "@/lib/console-data";
import { DemoLabel, PanelHeading, StatusBadge } from "./ui";
export function MarketAnalytics() {
  return (
    <section
      className="panel analytics-panel"
      id="analytics"
      data-source="demo"
    >
      <PanelHeading title="Market & Signal Analytics">
        <DemoLabel />
        <span className="subtle-label">24H</span>
      </PanelHeading>
      <div className="analytics-metrics">
        <div>
          <h3>Discovery Rate</h3>
          <div className="mini-bars" aria-hidden="true">
            {[22, 38, 30, 60, 44, 80, 51, 63, 46, 43].map((height, i) => (
              <i key={i} style={{ height: `${height}%` }} />
            ))}
          </div>
          <strong>
            3.4<span> / min</span>
          </strong>
          <small className="positive">▲ 28%</small>
        </div>
        <div>
          <h3>Liquidity Quality</h3>
          <svg viewBox="0 0 100 60" aria-hidden="true">
            <path d="M0 37L10 26L20 30L30 17L40 30L50 28L60 42L70 45L80 35L90 30L100 35" />
            <path
              className="secondary-line"
              d="M0 44L10 39L20 45L30 35L40 44L50 42L60 51L70 56L80 46L90 43L100 47"
            />
          </svg>
          <strong>
            87<span> / 100</span>
          </strong>
          <small className="positive">▲ 6%</small>
        </div>
        <div>
          <h3>Signal Quality</h3>
          <div className="signal-gauge">
            <span>72</span>
          </div>
          <strong>Balanced</strong>
          <small>Demo score</small>
        </div>
        <div>
          <h3>Avg. Obs. Slippage</h3>
          <svg
            className="slippage-line"
            viewBox="0 0 100 60"
            aria-hidden="true"
          >
            <path d="M0 13L20 23L40 29L60 36L80 44L100 49" />
            <circle cx="80" cy="44" r="4" />
          </svg>
          <strong>0.08%</strong>
          <small className="positive">▼ 35%</small>
        </div>
      </div>
    </section>
  );
}
export function RiskDistribution() {
  return (
    <section className="panel risk-panel" data-source="demo">
      <PanelHeading title="Risk Distribution">
        <DemoLabel />
      </PanelHeading>
      <div className="risk-content">
        <div
          className="risk-donut"
          role="img"
          aria-label="Demo exposure: RH Chain 14 percent, BSC 20 percent, cash 66 percent"
        >
          <div>
            <strong>34%</strong>
            <small>Paper exposure</small>
          </div>
        </div>
        <div className="risk-legend">
          <div>
            <i className="dot blue" />
            RH Chain<strong>14%</strong>
          </div>
          <div>
            <i className="dot orange" />
            BSC<strong>20%</strong>
          </div>
          <div>
            <i className="dot green" />
            Cash<strong>66%</strong>
          </div>
          <small>Illustrative allocation</small>
        </div>
      </div>
    </section>
  );
}
function Pnl({ value }: { value: string }) {
  return (
    <span className={value.startsWith("−") ? "negative" : "positive"}>
      {value}
    </span>
  );
}
export function RecentTrades() {
  return (
    <section className="panel table-panel" id="trades" data-source="demo">
      <PanelHeading title="Recent Trades">
        <DemoLabel />
      </PanelHeading>
      <div
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label="Demo recent trades"
      >
        <table>
          <thead>
            <tr>
              {["Time", "Chain", "Pair", "Side", "Size", "PnL"].map((x) => (
                <th key={x}>{x}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.pair}>
                <td>{t.time}</td>
                <td>{t.chain}</td>
                <td>
                  <strong>{t.pair}</strong>
                </td>
                <td>
                  <StatusBadge tone={t.side === "BUY" ? "green" : "red"}>
                    {t.side}
                  </StatusBadge>
                </td>
                <td>{t.size}</td>
                <td>
                  <Pnl value={t.pnl} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="table-caption">
        Simulated fills · illustrative instruments
      </div>
    </section>
  );
}
export function OpenPositions() {
  return (
    <section className="panel table-panel" id="positions" data-source="demo">
      <PanelHeading title="Open Positions">
        <DemoLabel />
      </PanelHeading>
      <div
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label="Demo open positions"
      >
        <table>
          <thead>
            <tr>
              {["Pair", "Chain", "Side", "Size", "PnL", "ROI"].map((x) => (
                <th key={x}>{x}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.pair}>
                <td>
                  <strong>{p.pair}</strong>
                </td>
                <td>{p.chain}</td>
                <td>
                  <StatusBadge tone="green">LONG</StatusBadge>
                </td>
                <td>{p.size}</td>
                <td>
                  <Pnl value={p.pnl} />
                </td>
                <td>
                  <Pnl value={p.roi} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="table-caption">
        Paper portfolio fixture · no actual holdings
      </div>
    </section>
  );
}
export function GlobalControls() {
  return (
    <section className="panel controls-panel">
      <PanelHeading title="Global Controls">
        <span className="subtle-label">Read-only</span>
        <SlidersHorizontal size={14} />
      </PanelHeading>
      <div className="control-rows">
        <div className="kill-row">
          <span>
            <Power size={13} />
            Kill Switch
          </span>
          <button
            disabled
            className="switch"
            aria-label="Kill switch unavailable: read-only policy preview"
          />
        </div>
        {[
          ["Max Exposure", "$10,000"],
          ["Max Position", "$2,500"],
          ["Max Slippage", "1.0%"],
          ["Daily Loss Limit", "$500"],
        ].map(([label, value]) => (
          <div key={label}>
            <span>
              <ShieldCheck size={12} />
              {label}
            </span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
      <div className="execution-boundary">
        <div>
          Live execution<strong>DISABLED</strong>
        </div>
        <div>
          <span>
            <LockKeyhole size={11} /> Wallet / Signer
          </span>
          <strong>NOT CONFIGURED</strong>
        </div>
      </div>
      <p className="control-caption">
        <TrendingUp size={11} /> Read-only paper policy preview
      </p>
    </section>
  );
}
