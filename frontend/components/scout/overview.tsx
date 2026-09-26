import { StatusBadge } from "@/components/console/ui";
import {
  backlogLabel,
  formatAge,
  formatRate,
  type ScoutOverview,
} from "@/lib/scout";

function Kpi({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: string;
}) {
  return (
    <div className="scout-kpi">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
}

export function OverviewStrip({ overview }: { overview: ScoutOverview }) {
  const latest = overview.latest_run;
  const active = overview.by_status.WATCHING + overview.by_status.PROMOTABLE;
  return (
    <section className="panel scout-overview" aria-label="Scout status">
      <div className="scout-kpis">
        <Kpi
          label="Active watches"
          value={String(active)}
          detail={`${overview.watches} total`}
        />
        <Kpi label="Watching" value={String(overview.by_status.WATCHING)} />
        <Kpi label="Promotable" value={String(overview.by_status.PROMOTABLE)} />
        <Kpi label="Dormant" value={String(overview.by_status.DORMANT)} />
        <Kpi label="Retired" value={String(overview.by_status.RETIRED)} />
        <Kpi
          label="ORBIT backlog"
          value={String(overview.orbit_backlog)}
          detail={`${overview.unreviewed_watches} never reviewed`}
        />
        <Kpi
          label="Oldest due"
          value={formatAge(overview.oldest_orbit_due_age_seconds)}
        />
        <Kpi
          label="Latest run"
          value={
            latest
              ? `${latest.run.valid_markets}/${latest.run.discovered} valid`
              : "no runs yet"
          }
          detail={
            latest
              ? `${latest.run.provider_identity_rejects} identity rejects · acceptance ${formatRate(latest.identity_acceptance_rate)}`
              : "run history starts with the next scout run"
          }
        />
      </div>
      <p className="scout-backlog" role="status">
        <StatusBadge tone={overview.orbit_backlog > 0 ? "orange" : "green"}>
          ORBIT
        </StatusBadge>{" "}
        {backlogLabel(
          overview.orbit_backlog,
          overview.oldest_orbit_due_age_seconds,
        )}
      </p>
    </section>
  );
}
