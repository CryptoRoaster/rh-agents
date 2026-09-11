import type { ReactNode } from "react";
import { Bot, Crown, ShieldCheck, Database } from "lucide-react";
import type { ConsoleAgent, Tone } from "@/lib/console-data";
export function StatusBadge({
  children,
  tone = "blue",
}: {
  children: ReactNode;
  tone?: Tone;
}) {
  return <span className={`status-badge tone-${tone}`}>{children}</span>;
}
export function PanelHeading({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="panel-heading">
      <h2>{title}</h2>
      <div>{children}</div>
    </header>
  );
}
export function DemoLabel() {
  return <span className="demo-label">DEMO</span>;
}
export function AgentBadge({ agent }: { agent: ConsoleAgent }) {
  const Icon =
    agent.name === "SENTINEL"
      ? ShieldCheck
      : agent.name === "LEDGER"
        ? Database
        : agent.kind === "control"
          ? Crown
          : Bot;
  return (
    <span
      className={`agent-badge tone-${agent.tone} ${agent.kind === "service" ? "service-badge" : ""}`}
    >
      <Icon size={18} />
    </span>
  );
}
