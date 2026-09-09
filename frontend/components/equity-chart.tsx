"use client";

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

export default function EquityChart() {
  return (
    <div
      className="equity-chart"
      role="img"
      aria-label="Illustrative portfolio equity increases from 10,000 to 10,128.40 dollars over 24 hours. Demo data only."
    >
      <ResponsiveContainer
        width="100%"
        height="100%"
        minWidth={0}
        initialDimension={{ width: 700, height: 220 }}
      >
        <AreaChart
          data={equity}
          margin={{ top: 15, right: 12, bottom: 0, left: 0 }}
        >
          <defs>
            <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#257866" stopOpacity={0.16} />
              <stop offset="100%" stopColor="#257866" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid
            strokeDasharray="3 5"
            vertical={false}
            stroke="#e9eeeb"
          />
          <XAxis
            dataKey="time"
            axisLine={false}
            tickLine={false}
            minTickGap={40}
            tick={{ fill: "#7d8582", fontSize: 11 }}
            dy={8}
          />
          <YAxis
            domain={[9960, 10160]}
            ticks={[9960, 10060, 10160]}
            axisLine={false}
            tickLine={false}
            tickFormatter={(value: number) => `$${(value / 1000).toFixed(2)}k`}
            tick={{ fill: "#7d8582", fontSize: 11 }}
            width={64}
          />
          <Tooltip
            formatter={(value) => [
              `$${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2 })}`,
              "Demo equity",
            ]}
            contentStyle={{
              borderRadius: 10,
              border: "1px solid #e2e8e4",
              fontSize: 12,
            }}
          />
          <Area
            type="monotone"
            dataKey="value"
            stroke="#257866"
            strokeWidth={2.5}
            fill="url(#equity-fill)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
