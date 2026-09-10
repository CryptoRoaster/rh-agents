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
} from "lucide-react";
const links = [
  ["Dashboard", "dashboard", LayoutDashboard],
  ["Agents", "agents", UsersRound],
  ["Trading", "trades", ArrowLeftRight],
  ["Analytics", "analytics", ChartNoAxesCombined],
  ["Positions", "positions", BriefcaseBusiness],
  ["Strategies", "controls", Workflow],
  ["Logs", "activity", ScrollText],
  ["Settings", "controls", Settings2],
] as const;
export function Sidebar() {
  return (
    <aside className="sidebar">
      <a
        href="#dashboard"
        className="brand-mark"
        aria-label="RH Agents dashboard"
      >
        rh<span>/</span>
      </a>
      <nav aria-label="Main navigation">
        {links.map(([name, id, Icon], i) => (
          <a
            key={name}
            href={`#${id}`}
            className={`nav-link ${i === 0 ? "active" : ""}`}
            aria-current={i === 0 ? "page" : undefined}
          >
            <Icon size={20} />
            <span>{name}</span>
          </a>
        ))}
      </nav>
      <div className="sidebar-foot">
        <ShieldCheck size={20} />
        <strong>Paper only</strong>
        <span>Observe. Validate.</span>
      </div>
    </aside>
  );
}
