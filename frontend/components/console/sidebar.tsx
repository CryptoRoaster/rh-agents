import {
  LayoutDashboard,
  UsersRound,
  ArrowLeftRight,
  ChartNoAxesCombined,
  BriefcaseBusiness,
  Workflow,
  ScrollText,
  Settings2,
  ShieldCheck,
  Radar,
} from "lucide-react";
const links = [
  ["Dashboard", "dashboard", LayoutDashboard],
  ["Discovery", "scout", Radar],
  ["Agents", "agents", UsersRound],
  ["Trading", "trades", ArrowLeftRight],
  ["Analytics", "analytics", ChartNoAxesCombined],
  ["Positions", "positions", BriefcaseBusiness],
  ["Strategies", "controls", Workflow],
  ["Logs", "activity", ScrollText],
  ["Settings", "controls", Settings2],
] as const;
// `scout` is its own page; every other entry is a section of the dashboard.
export function Sidebar({
  current = "dashboard",
}: {
  current?: "dashboard" | "scout";
}) {
  return (
    <aside className="sidebar">
      <a
        href={current === "dashboard" ? "#dashboard" : "/"}
        className="brand-mark"
        aria-label="RH Agents dashboard"
      >
        rh<span>/</span>
      </a>
      <nav aria-label="Main navigation">
        {links.map(([name, id, Icon], i) => {
          const active =
            id === "scout"
              ? current === "scout"
              : current === "dashboard" && i === 0;
          const href =
            id === "scout"
              ? "/scout"
              : current === "dashboard"
                ? `#${id}`
                : `/#${id}`;
          return (
            <a
              key={name}
              href={href}
              className={`nav-link ${active ? "active" : ""}`}
              aria-current={active ? "page" : undefined}
            >
              <Icon size={20} />
              <span>{name}</span>
            </a>
          );
        })}
      </nav>
      <div className="sidebar-foot">
        <ShieldCheck size={20} />
        <strong>Paper only</strong>
        <span>Observe. Validate.</span>
      </div>
    </aside>
  );
}
