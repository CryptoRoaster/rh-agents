"use client";
import { useEffect, useState } from "react";
import { Bell, Clock3, ChevronDown } from "lucide-react";
import { PolicyMenu } from "./policy-menu";
import { StatusBadge } from "./ui";
export function TopBar() {
  const [session, setSession] = useState({
    time: "—",
    date: "Local session",
    elapsed: "00H 00M",
  });
  const [notice, setNotice] = useState(false);
  useEffect(() => {
    const start = Date.now();
    const tick = () => {
      const now = new Date();
      const minutes = Math.floor((now.getTime() - start) / 60000);
      setSession({
        time: now.toLocaleTimeString("en-GB"),
        date: now.toLocaleDateString("en-GB", {
          day: "2-digit",
          month: "short",
          year: "numeric",
        }),
        elapsed: `${String(Math.floor(minutes / 60)).padStart(2, "0")}H ${String(minutes % 60).padStart(2, "0")}M`,
      });
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <header className="topbar">
      <div className="product-title">
        <h1>
          rh<span>/</span>agents
        </h1>
        <p>Multi-Agent On-Chain Trading Console</p>
      </div>
      <div className="topbar-ops">
        <div className="operation">
          <StatusBadge tone="green">● OPERATIONAL</StatusBadge>
          <small>Console · not agent telemetry</small>
        </div>
        <div className="operation runtime">
          <Clock3 size={19} />
          <div>
            <small>Session Runtime</small>
            <strong>{session.elapsed}</strong>
          </div>
        </div>
        <div className="operation">
          <small>Mode · RH + BSC</small>
          <StatusBadge tone="blue">PAPER · SIMULATED</StatusBadge>
        </div>
        <div className="operation local-time">
          <small>{session.date} · local</small>
          <strong>{session.time}</strong>
        </div>
        <PolicyMenu />
        <div className="notification-wrap">
          <button
            className="icon-button"
            aria-label="Notifications"
            aria-expanded={notice}
            onClick={() => setNotice(!notice)}
          >
            <Bell size={20} />
          </button>
          {notice && (
            <div className="notification-popover">
              <strong>No connected notification service</strong>
              <p>Activity below is a labeled workflow demo.</p>
            </div>
          )}
        </div>
        <span className="avatar" aria-label="Local user placeholder">
          RH
        </span>
        <ChevronDown size={13} />
      </div>
    </header>
  );
}
