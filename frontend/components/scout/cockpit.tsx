"use client";
import { useEffect, useMemo, useState } from "react";
import { RefreshCw, ShieldCheck } from "lucide-react";
import { Sidebar } from "@/components/console/sidebar";
import { PanelHeading } from "@/components/console/ui";
import {
  filterQuery,
  type ListFilter,
  type PaperPortfolio,
  type RunPage,
  type ScoutOverview,
  type WatchDetail,
  type WatchPage,
} from "@/lib/scout";
import { OverviewStrip } from "./overview";
import { PaperSection } from "./paper";
import { RunHistoryTable } from "./run-history";
import { Empty, Gate } from "./states";
import { useResource } from "./use-resource";
import { WatchDetailView } from "./watch-detail";
import { WatchFilters, WatchTable } from "./watch-list";

// The early-discovery cockpit. Read-only by construction: every request is a GET
// through the cockpit proxy, and nothing here can trade, approve, sign or change
// a setting. It shows what the scout recorded; it does not decide anything.
export function ScoutCockpit() {
  const [version, setVersion] = useState(0);
  const [filter, setFilter] = useState<ListFilter>("ALL");
  const [selected, setSelected] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);
  const watchesPath = useMemo(() => {
    const query = new URLSearchParams({ limit: "100", ...filterQuery(filter) });
    return `scout/watches?${query}`;
  }, [filter]);
  const overview = useResource<ScoutOverview>("scout/overview", version);
  const watches = useResource<WatchPage>(watchesPath, version);
  const detail = useResource<WatchDetail>(
    selected ? `scout/watches/${selected}` : null,
    version,
  );
  const runs = useResource<RunPage>("scout/runs?limit=20", version);
  const paper = useResource<PaperPortfolio>("paper/portfolio", version);
  return (
    <div className="workspace">
      <Sidebar current="scout" />
      <div className="main-wrap">
        <main id="scout" className="scout">
          <header className="scout-heading">
            <div>
              <h1>Early Discovery</h1>
              <p>
                <ShieldCheck size={13} /> Read-only scout cockpit ·
                observations, not trade recommendations · PROMOTABLE is not a
                buy approval
              </p>
            </div>
            <button
              type="button"
              className="scout-refresh"
              onClick={() => {
                setNow(Date.now());
                setVersion((value) => value + 1);
              }}
            >
              <RefreshCw size={13} /> Reload data
            </button>
          </header>
          <Gate resource={overview} what="Scout status">
            {(data) => <OverviewStrip overview={data} />}
          </Gate>
          <div className="scout-grid">
            <section className="panel" aria-label="Watch list">
              <PanelHeading title="Watches">
                <WatchFilters current={filter} onChange={setFilter} />
              </PanelHeading>
              <Gate resource={watches} what="Watch list">
                {(data) =>
                  data.items.length === 0 ? (
                    <Empty>
                      No watches for this filter. The scout creates them from
                      newly discovered pools.
                    </Empty>
                  ) : (
                    <WatchTable
                      page={data}
                      now={now}
                      selected={selected}
                      onSelect={setSelected}
                    />
                  )
                }
              </Gate>
            </section>
            <section className="panel" aria-label="Watch detail">
              <PanelHeading title="Watch detail" />
              {selected === null ? (
                <Empty>Select a watch to see its ORBIT timeline.</Empty>
              ) : (
                <Gate resource={detail} what="Watch detail">
                  {(data) => <WatchDetailView detail={data} now={now} />}
                </Gate>
              )}
            </section>
          </div>
          <section className="panel" aria-label="Scout run history">
            <PanelHeading title="Scout runs" />
            <Gate resource={runs} what="Run history">
              {(data) =>
                data.items.length === 0 ? (
                  <Empty>No scout runs recorded yet.</Empty>
                ) : (
                  <RunHistoryTable page={data} />
                )
              }
            </Gate>
          </section>
          <section className="panel" aria-label="Paper portfolio">
            <PanelHeading title="Paper portfolio" />
            <Gate resource={paper} what="Paper portfolio">
              {(data) => <PaperSection portfolio={data} />}
            </Gate>
          </section>
        </main>
      </div>
    </div>
  );
}
