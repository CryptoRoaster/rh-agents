import { StatusBadge } from "@/components/console/ui";
import type { FastAnswer, FastAssessment } from "@/lib/scout";

const percent = (value: number) => `${Math.round(value * 100)}%`;

function answerText(answer: FastAnswer): string {
  if (answer.type === "noul") return `yes ${percent(answer.noul)}`;
  if (answer.type === "choice")
    return `${answer.choice} (${percent(answer.probabilities[answer.choice] ?? 0)}, confidence ${percent(answer.confidence)})`;
  const levels = Object.keys(answer.legend).length;
  return `${answer.score.toFixed(2)} of ${levels - 1} (confidence ${percent(answer.confidence)})`;
}

// JEV fast assessments are shadow evidence. They are shown, never acted on:
// no control here changes a watch, a review, a promotion or a trade.
export function FastShadowSection({
  assessments,
}: {
  assessments: FastAssessment[];
}) {
  return (
    <section className="scout-shadow" aria-label="Fast shadow assessment">
      <h4>Fast shadow assessment</h4>
      <p className="scout-shadow-notice">SHADOW — NO TRADING EFFECT</p>
      {assessments.length === 0 ? (
        <p className="scout-muted">
          No shadow assessment for this watch. Only watches opened while JEV is
          enabled are assessed; older watches are not backfilled.
        </p>
      ) : (
        assessments.map((item) => (
          <div key={item.id} className="scout-assessment">
            <div className="scout-timeline-head">
              <strong>
                {item.provider} · {item.model_version ?? item.model}
              </strong>
              <StatusBadge
                tone={
                  item.status === "COMPLETED"
                    ? "green"
                    : item.status === "FAILED"
                      ? "red"
                      : "orange"
                }
              >
                {item.status}
              </StatusBadge>
            </div>
            <small className="scout-muted">
              {new Date(item.assessed_at ?? item.reserved_at).toLocaleString(
                "en-GB",
              )}{" "}
              · questions {item.question_version} · {item.latency_ms ?? "—"} ms
            </small>
            {item.status === "FAILED" ? (
              <p>
                Assessment failed: {item.failure_category}
                <span className="scout-codes">
                  {" "}
                  · {item.failure_reason_code}
                </span>
              </p>
            ) : item.answers ? (
              <dl className="scout-facts">
                {Object.entries(item.answers).map(([name, answer]) => (
                  <div key={name} className="scout-shadow-signal">
                    <dt>{name.replaceAll("_", " ")}</dt>
                    <dd>{answerText(answer)}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </div>
        ))
      )}
    </section>
  );
}
