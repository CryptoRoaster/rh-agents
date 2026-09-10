import { consoleAgents } from "@/lib/console-data";
import { AgentBadge, PanelHeading } from "./ui";
const coordinates = [
  ["COMMANDER", 50, 13],
  ["ORBIT", 10, 45],
  ["SENTINEL", 30, 45],
  ["VECTOR", 50, 45],
  ["PULSE", 70, 45],
  ["SIGNAL", 90, 45],
  ["ATLAS", 20, 77],
  ["ANCHOR", 40, 77],
  ["FUSE", 60, 77],
  ["LEDGER", 82, 77],
] as const;
export function AgentNetwork() {
  return (
    <section className="panel network-panel">
      <PanelHeading title="Agent Network">
        <span className="subtle-label">Architecture · planned roles</span>
      </PanelHeading>
      <div
        className="network-diagram"
        role="img"
        aria-label="Specialists send evidence to FUSE; FUSE sends the package to COMMANDER; COMMANDER requests SENTINEL validation. Only approved intents reach the paper executor and then LEDGER."
      >
        <svg
          viewBox="0 0 400 210"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <defs>
            <marker
              id="arrow-evidence"
              markerWidth="5"
              markerHeight="5"
              refX="4"
              refY="2.5"
              orient="auto"
            >
              <path d="M0 0L5 2.5L0 5" fill="var(--info)" />
            </marker>
            <marker
              id="arrow-data"
              markerWidth="5"
              markerHeight="5"
              refX="4"
              refY="2.5"
              orient="auto"
            >
              <path d="M0 0L5 2.5L0 5" fill="var(--success)" />
            </marker>
            <marker
              id="arrow-execution"
              markerWidth="5"
              markerHeight="5"
              refX="4"
              refY="2.5"
              orient="auto"
            >
              <path d="M0 0L5 2.5L0 5" fill="var(--danger)" />
            </marker>
          </defs>
          <g className="flow-data" markerEnd="url(#arrow-data)">
            <path d="M40 113L75 147" />
            <path d="M200 110L272 96" />
          </g>
          <g className="flow-evidence" markerEnd="url(#arrow-evidence)">
            <path d="M84 162L221 162" />
            <path d="M174 164L223 164" />
            <path d="M284 109L244 148" />
            <path d="M358 111L254 156" />
            <path d="M235 146L213 46" />
            <path d="M188 37L128 79" />
          </g>
          <g className="flow-execution" markerEnd="url(#arrow-execution)">
            <path d="M120 113L120 198L328 198L328 180" />
          </g>
          <text x="200" y="195" className="network-route-label">
            PASS → PAPER EXECUTOR
          </text>
        </svg>
        {coordinates.map(([name, x, y]) => {
          const agent = consoleAgents.find((a) => a.name === name)!;
          return (
            <div
              key={name}
              className="network-node"
              style={{ left: `${x}%`, top: `${y}%` }}
            >
              <AgentBadge agent={agent} />
              <strong>{name}</strong>
            </div>
          );
        })}
      </div>
      <div className="network-legend">
        <span>
          <i className="dot green" />
          Data flow
        </span>
        <span>
          <i className="dot blue" />
          Evidence flow
        </span>
        <span>
          <i className="dot red" />
          Execution flow
        </span>
      </div>
    </section>
  );
}
