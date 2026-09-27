import { StatusBadge } from "@/components/console/ui";
import {
  CHECKPOINT_LABELS,
  checkpointTone,
  classificationTone,
  formatAge,
  formatRelative,
  formatUsd,
  pairLabel,
  statusTone,
  type WatchDetail,
} from "@/lib/scout";

export function WatchDetailView({
  detail,
  now,
}: {
  detail: WatchDetail;
  now: number;
}) {
  const { watch, checkpoints, assessments, trade_case } = detail;
  const snapshot = watch.latest_snapshot;
  const byId = new Map(assessments.map((item) => [item.id, item]));
  const snapshotAge = snapshot
    ? Math.round((now - Date.parse(snapshot.observed_at)) / 1000)
    : null;
  return (
    <div className="scout-detail">
      <header>
        <h3>{pairLabel(watch)}</h3>
        <StatusBadge tone={statusTone(watch.status)}>
          {watch.status}
        </StatusBadge>
      </header>
      <p className="scout-note">
        A watch is an observation, not a trade recommendation.
        {watch.status === "PROMOTABLE"
          ? " PROMOTABLE means VECTOR-sufficient history exists; it is not a buy approval."
          : ""}
      </p>
      <dl className="scout-facts">
        <dt>Chain / venue</dt>
        <dd>
          {watch.chain} · {watch.venue}
        </dd>
        <dt>Pair</dt>
        <dd className="scout-mono">{watch.pair_id}</dd>
        <dt>Base / quote</dt>
        <dd className="scout-mono">
          {watch.base_asset_id} / {watch.quote_asset_id}
        </dd>
        <dt>First / last seen</dt>
        <dd>
          {new Date(watch.first_seen_at).toLocaleString("en-GB")} ·{" "}
          {new Date(watch.last_seen_at).toLocaleString("en-GB")} (age{" "}
          {formatAge(watch.age_seconds)})
        </dd>
        <dt>Latest reading</dt>
        <dd>
          {snapshot ? (
            <>
              price {formatUsd(snapshot.price_usd, snapshot.price_status)} ·
              liquidity{" "}
              {formatUsd(snapshot.liquidity_usd, snapshot.liquidity_status)} ·
              volume {formatUsd(snapshot.volume_usd, snapshot.volume_status)} ·
              observed {formatAge(snapshotAge)} ago
            </>
          ) : (
            "no reading recorded"
          )}
        </dd>
        <dt>Lifecycle</dt>
        <dd>
          reason {watch.reason_code} · next ORBIT{" "}
          {formatRelative(watch.next_orbit_review_at, now)} · next history{" "}
          {formatRelative(watch.next_history_review_at, now)}
        </dd>
        <dt>VECTOR maturity</dt>
        <dd>
          {watch.latest_vector_sufficiency ?? "not checked yet"}
          {watch.vector_checked_at
            ? ` (checked ${new Date(watch.vector_checked_at).toLocaleString("en-GB")})`
            : ""}
        </dd>
        <dt>Trade case</dt>
        <dd>
          {trade_case ? (
            <a
              href={`/api/cockpit/trade-cases/${trade_case.trade_case_id}`}
              className="scout-link"
            >
              {trade_case.status} · opened{" "}
              {new Date(trade_case.opened_at).toLocaleString("en-GB")}
            </a>
          ) : (
            "none formed"
          )}
        </dd>
      </dl>
      <h4>ORBIT timeline</h4>
      <p className="scout-note">
        Scout assessments are discovery history. They are never TradeCase
        evidence.
      </p>
      <ol className="scout-timeline">
        {checkpoints.map((checkpoint) => {
          const assessment = checkpoint.assessment_id
            ? byId.get(checkpoint.assessment_id)
            : undefined;
          return (
            <li key={checkpoint.checkpoint_index}>
              <div className="scout-timeline-head">
                <strong>
                  {CHECKPOINT_LABELS[checkpoint.checkpoint_index]}
                </strong>
                <StatusBadge tone={checkpointTone(checkpoint.state)}>
                  {checkpoint.state}
                </StatusBadge>
                {assessment?.classification ? (
                  <StatusBadge
                    tone={classificationTone(assessment.classification)}
                  >
                    {assessment.classification} · {assessment.strength}
                  </StatusBadge>
                ) : null}
              </div>
              {assessment ? (
                <div className="scout-assessment">
                  <small>
                    {new Date(assessment.assessed_at).toLocaleString("en-GB")}
                  </small>
                  {assessment.status === "FAILED" ? (
                    <p>Review failed: {assessment.failure_reason}</p>
                  ) : (
                    <>
                      <p>{assessment.summary}</p>
                      <p className="scout-codes">
                        {assessment.reason_codes.join(" · ")}
                        {assessment.data_gaps.length
                          ? ` · gaps: ${assessment.data_gaps.join(", ")}`
                          : ""}
                      </p>
                      <small className="scout-muted">
                        {assessment.reasoning_provider}/
                        {assessment.reasoning_model} ·{" "}
                        {assessment.input_tokens ?? "—"} in /{" "}
                        {assessment.output_tokens ?? "—"} out ·{" "}
                        {assessment.latency_ms ?? "—"} ms ·{" "}
                        {assessment.prompt_version}
                      </small>
                    </>
                  )}
                </div>
              ) : (
                <small className="scout-muted">
                  {checkpoint.state === "COALESCED"
                    ? "Covered by a later review; missed checkpoints are not replayed."
                    : checkpoint.state === "NOT_SCHEDULED"
                      ? "No longer reviewed."
                      : `Due ${new Date(checkpoint.due_at).toLocaleString("en-GB")}; no assessment yet.`}
                </small>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
