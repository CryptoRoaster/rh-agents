"use client";
import { SortableGroup } from "./sortable";
import { consoleAgents, type ConsoleAgent } from "@/lib/console-data";
import { AgentBadge, DemoLabel, PanelHeading } from "./ui";
export function AgentCard({ agent }: { agent: ConsoleAgent }) {
  const service = agent.kind === "service";
  return (
    <article className="agent-card">
      <div className="agent-title">
        <AgentBadge agent={agent} />
        <div>
          <h3>{agent.name}</h3>
          <p title={agent.role}>{agent.role}</p>
        </div>
      </div>
      <div className="agent-state">
        <i className={`dot ${service ? "green" : "slate"}`} />
        {service ? "Paper service" : "Planned specialist"}
      </div>
      <div className="agent-meter">
        <div className={`meter tone-${agent.tone}`}>
          <i style={{ width: `${agent.metric}%` }} />
        </div>
        <span>{agent.metric}%</span>
      </div>
      <div className="metric-caption">
        {service ? "Demo health" : "Demo confidence"}
      </div>
      <dl className="agent-metrics">
        <div>
          <dt>{service ? "Validations" : "Processed"}</dt>
          <dd>{agent.processed}</dd>
        </div>
        <div>
          <dt>
            Accepted <b>{agent.accepted}</b>
          </dt>
          <dd>
            Rejects <b>{agent.rejected}</b>
          </dd>
        </div>
        {service && (
          <div>
            <dt>Demo latency</dt>
            <dd>{agent.name === "SENTINEL" ? "4" : "2"} ms</dd>
          </div>
        )}
      </dl>
      <div className="last-action">
        <span>
          Last demo action <time>{agent.age} ago</time>
        </span>
        <strong title={agent.action}>{agent.action}</strong>
      </div>
    </article>
  );
}
export function AgentOverview() {
  return (
    <section className="panel agent-overview" id="agents" data-source="demo">
      <PanelHeading title="Agent Overview">
        <span className="agent-overview-subtitle">
          10 specialist & service roles
        </span>
        <DemoLabel />
        <span className="agents-notice">
          Illustrative metrics · no running LLM agents
        </span>
      </PanelHeading>
      <SortableGroup
        group="agents"
        className="agent-grid"
        label="Reorder agent cards"
        items={consoleAgents.map((a) => ({
          id: a.name,
          label: a.name,
          content: <AgentCard agent={a} />,
        }))}
      />
    </section>
  );
}
