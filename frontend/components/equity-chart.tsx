"use client";
import { useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { equity } from "@/lib/demo";
import { PanelHeading, DemoLabel } from "./console/ui";
const ranges = ["1H", "24H", "7D", "30D", "ALL"] as const;
export default function EquityChart() {
  const [range, setRange] = useState<(typeof ranges)[number]>("24H");
  // Extra fixture samples provide the reference's fine-grained chart texture.
  const samples = equity.flatMap((point, index) => {
    const next = equity[index + 1];
    if (!next) return [point];
    return [0, 1, 2, 3].map((step) => ({
      time: point.time,
      value:
        point.value +
        ((next.value - point.value) * step) / 4 +
        [0, -2, 3, -1][step],
    }));
  });
  const data = samples.map((point, i) => ({
    ...point,
    time:
      range === "24H"
        ? `${String(Math.floor((i * 24) / (samples.length - 1))).padStart(2, "0")}:00`
        : range === "1H"
          ? `${String(Math.floor((i * 60) / (samples.length - 1))).padStart(2, "0")}m`
          : range === "7D"
            ? `D${Math.floor((i * 7) / samples.length) + 1}`
            : `D${Math.floor((i * 30) / samples.length) + 1}`,
  }));
  return (
    <section className="panel equity-panel" data-source="demo">
      <PanelHeading title="Portfolio Equity">
        <div className="range-tabs" aria-label="Demo chart period">
          {ranges.map((r) => (
            <button
              key={r}
              aria-pressed={r === range}
              onClick={() => setRange(r)}
            >
              {r}
            </button>
          ))}
        </div>
      </PanelHeading>
      <div className="equity-summary">
        <strong>$10,128.40</strong>
        <span className="positive">▲ +1.28%</span>
        <span>({range.toLowerCase()})</span>
        <DemoLabel />
      </div>
      <div
        className="equity-chart"
        role="img"
        aria-label="Illustrative paper equity chart, not actual portfolio performance"
      >
        <ResponsiveContainer
          width="100%"
          height="100%"
          minWidth={0}
          initialDimension={{ width: 700, height: 205 }}
        >
          <AreaChart
            data={data}
            margin={{ top: 16, right: 17, bottom: 4, left: 0 }}
          >
            <defs>
              <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
                <stop
                  offset="0%"
                  stopColor="var(--success)"
                  stopOpacity={0.2}
                />
                <stop
                  offset="100%"
                  stopColor="var(--success)"
                  stopOpacity={0.015}
                />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} stroke="var(--border)" />
            <XAxis
              dataKey="time"
              tickLine={false}
              axisLine={false}
              minTickGap={32}
              tick={{ fill: "var(--text-secondary)", fontSize: 10 }}
            />
            <YAxis
              domain={[9950, 10150]}
              ticks={[9950, 10000, 10050, 10100, 10150]}
              tickFormatter={(v: number) => `${(v / 1000).toFixed(2)}k`}
              width={48}
              tickLine={false}
              axisLine={false}
              tick={{ fill: "var(--text-secondary)", fontSize: 10 }}
            />
            <Tooltip
              formatter={(v) => [`$${Number(v).toFixed(2)}`, "Demo equity"]}
              contentStyle={{
                fontSize: 11,
                border: "1px solid var(--border)",
                borderRadius: 5,
              }}
            />
            <Area
              type="linear"
              dataKey="value"
              stroke="var(--success)"
              strokeWidth={2}
              fill="url(#equity-fill)"
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <div className="chart-caption">
        <span>
          <i className="dot green" /> Paper portfolio
        </span>
        <span>Illustrative series · period preview</span>
      </div>
    </section>
  );
}
