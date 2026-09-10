"use client";
import { Children } from "react";
import { SortableGroup } from "./sortable";
import {
  Wallet,
  ChartNoAxesCombined,
  Crosshair,
  Layers,
  Radar,
  ToggleRight,
  ShieldHalf,
  type LucideIcon,
} from "lucide-react";
import { DemoLabel } from "./ui";
function Sparkline({ variant = 0 }: { variant?: number }) {
  return (
    <svg className="sparkline" viewBox="0 0 85 35" aria-hidden="true">
      <path
        d={
          variant === 1
            ? "M0 27L8 22L16 26L23 11L30 23L40 19L49 26L58 8L66 22L74 17L85 8"
            : "M0 32L7 24L15 27L23 20L30 21L38 14L47 16L53 9L62 11L69 6L77 7L85 2"
        }
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
      />
    </svg>
  );
}
export function KpiCard({
  title,
  value,
  detail,
  icon: Icon,
  positive = false,
  chart = false,
}: {
  title: string;
  value: string;
  detail: string;
  icon: LucideIcon;
  positive?: boolean;
  chart?: boolean;
}) {
  return (
    <article className="kpi-card" data-kpi={title}>
      <div className="kpi-label">
        <Icon size={17} />
        <span>{title}</span>
      </div>
      <strong className={positive ? "positive" : ""}>{value}</strong>
      <div className="kpi-detail">
        <span className={positive ? "positive" : ""}>{detail}</span>
        {chart && <Sparkline variant={title === "Win Rate" ? 1 : 0} />}
      </div>
    </article>
  );
}
export function KpiRow() {
  const cards = (
    <>
      <KpiCard
        title="Paper Balance"
        value="$10,128.40"
        detail="▲ 1.28% (24h)"
        icon={Wallet}
        chart
      />
      <KpiCard
        title="Total PnL"
        value="+$128.40"
        detail="▲ $42.18 today"
        icon={ChartNoAxesCombined}
        positive
        chart
      />
      <KpiCard
        title="Win Rate"
        value="68.3%"
        detail="28 / 41 closed trades"
        icon={Crosshair}
        chart
      />
      <KpiCard
        title="Active Positions"
        value="4"
        detail="2 RH / 2 BSC · paper"
        icon={Layers}
      />
      <KpiCard
        title="Market Candidates"
        value="27"
        detail="32 reviewed · demo"
        icon={Radar}
      />
      <article className="kpi-card" data-kpi="Trading Mode">
        <div className="kpi-label">
          <ToggleRight size={17} />
          <span>Trading Mode</span>
        </div>
        <strong className="paper-mode">PAPER</strong>
        <div className="kpi-detail">Simulated execution</div>
      </article>
      <article className="kpi-card" data-kpi="Daily Risk Used">
        <div className="kpi-label">
          <ShieldHalf size={17} />
          <span>Daily Risk Used</span>
        </div>
        <div className="risk-kpi">
          <strong>12.8%</strong>
          <div>
            <div className="meter">
              <i style={{ width: "12.8%" }} />
            </div>
            <small>$64 / $500</small>
          </div>
        </div>
        <DemoLabel />
      </article>
    </>
  );
  const labels = [
    "Paper Balance",
    "Total PnL",
    "Win Rate",
    "Active Positions",
    "Market Candidates",
    "Trading Mode",
    "Daily Risk Used",
  ];
  const items = Children.toArray(cards.props.children).map(
    (content, index) => ({
      id: labels[index],
      label: labels[index],
      content,
    }),
  );
  return (
    <section aria-label="Illustrative paper metrics" data-source="demo">
      <SortableGroup
        group="kpis"
        className="kpi-row"
        label="Reorder KPI cards"
        items={items}
      />
    </section>
  );
}
