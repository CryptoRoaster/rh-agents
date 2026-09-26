import { StatusBadge } from "@/components/console/ui";
import { formatRate, type RunPage } from "@/lib/scout";

export function RunHistoryTable({ page }: { page: RunPage }) {
  return (
    <div className="table-scroll scout-table">
      <table>
        <caption className="sr-only">Scout runs, newest first</caption>
        <thead>
          <tr>
            <th>Started</th>
            <th>Duration</th>
            <th>Status</th>
            <th>Discovered</th>
            <th>Valid</th>
            <th>Identity rejects</th>
            <th>Acceptance</th>
            <th>Watches created</th>
            <th>ORBIT done</th>
            <th>Backlog before → after</th>
            <th>History checks</th>
            <th>Promotable</th>
            <th>Provider failures</th>
            <th>Model failures</th>
          </tr>
        </thead>
        <tbody>
          {page.items.map(
            ({ run, duration_seconds, identity_acceptance_rate }) => (
              <tr key={run.id}>
                <td>{new Date(run.started_at).toLocaleString("en-GB")}</td>
                <td>{duration_seconds.toFixed(1)}s</td>
                <td>
                  <StatusBadge
                    tone={
                      run.status === "COMPLETED"
                        ? "green"
                        : run.status === "STOPPED"
                          ? "orange"
                          : "red"
                    }
                  >
                    {run.status}
                  </StatusBadge>
                </td>
                <td>{run.discovered}</td>
                <td>{run.valid_markets}</td>
                <td>{run.provider_identity_rejects}</td>
                <td>{formatRate(identity_acceptance_rate)}</td>
                <td>{run.watches_created}</td>
                <td>
                  {run.orbit_reviews_completed}/{run.orbit_reviews_started}
                </td>
                <td>
                  {run.orbit_backlog_before} → {run.orbit_backlog_after}
                </td>
                <td>{run.history_checks}</td>
                <td>{run.promotable_new}</td>
                <td>{run.provider_failures}</td>
                <td>{run.model_failures}</td>
              </tr>
            ),
          )}
        </tbody>
      </table>
    </div>
  );
}
