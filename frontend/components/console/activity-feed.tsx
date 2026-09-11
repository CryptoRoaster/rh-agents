"use client";
import { useState } from "react";
import { activity, consoleAgents } from "@/lib/console-data";
import { DemoLabel, PanelHeading, StatusBadge } from "./ui";
export function ActivityFeed() {
  const [filter, setFilter] = useState("all");
  return (
    <section className="panel activity-panel" id="activity" data-source="demo">
      <PanelHeading title="Activity Feed">
        <DemoLabel />
        <select
          aria-label="Filter activity by agent"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        >
          <option value="all">All agents</option>
          {consoleAgents.map((a) => (
            <option key={a.name}>{a.name}</option>
          ))}
        </select>
      </PanelHeading>
      <div className="activity-list">
        {activity
          .filter((a) => filter === "all" || a.agent === filter)
          .map((a) => (
            <div className="activity-item" key={a.agent}>
              <time>{a.time}</time>
              <span
                className={`agent-tag tone-${consoleAgents.find((x) => x.name === a.agent)!.tone}`}
              >
                {a.agent}
              </span>
              <span className="activity-message" title={a.message}>
                {a.message}
              </span>
              <StatusBadge
                tone={
                  a.status === "REJECT"
                    ? "red"
                    : a.status === "WARNING"
                      ? "orange"
                      : a.status === "SUCCESS"
                        ? "green"
                        : "blue"
                }
              >
                {a.status}
              </StatusBadge>
            </div>
          ))}
      </div>
    </section>
  );
}
