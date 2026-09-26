import { StatusBadge } from "@/components/console/ui";
import {
  LIST_FILTERS,
  classificationTone,
  formatAge,
  formatRelative,
  formatUsd,
  pairLabel,
  shortId,
  statusTone,
  type ListFilter,
  type WatchPage,
  type WatchView,
} from "@/lib/scout";

const FILTER_LABELS: Record<ListFilter, string> = {
  ALL: "All",
  VERY_YOUNG: "Very young (<6h)",
  WATCHING: "Watching",
  PROMOTABLE: "Promotable",
  DORMANT: "Dormant",
  RETIRED: "Retired",
};

export function WatchFilters({
  current,
  onChange,
}: {
  current: ListFilter;
  onChange: (filter: ListFilter) => void;
}) {
  return (
    <div className="scout-filters" role="tablist" aria-label="Watch filter">
      {LIST_FILTERS.map((filter) => (
        <button
          key={filter}
          type="button"
          role="tab"
          aria-selected={filter === current}
          className={filter === current ? "active" : ""}
          onClick={() => onChange(filter)}
        >
          {FILTER_LABELS[filter]}
        </button>
      ))}
    </div>
  );
}

function Row({
  watch,
  now,
  selected,
  onSelect,
}: {
  watch: WatchView;
  now: number;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const snapshot = watch.latest_snapshot;
  const latest = watch.latest_assessment;
  return (
    <tr
      className={selected ? "selected" : undefined}
      onClick={() => onSelect(watch.watch_id)}
    >
      <td>
        <button
          type="button"
          className="scout-link"
          aria-label={`Open watch ${pairLabel(watch)}`}
          onClick={(event) => {
            event.stopPropagation();
            onSelect(watch.watch_id);
          }}
        >
          <strong
            className={watch.age_seconds < 21600 ? "scout-young" : undefined}
          >
            {formatAge(watch.age_seconds)}
          </strong>
        </button>
      </td>
      <td>
        <strong>{pairLabel(watch)}</strong>
        <small className="scout-muted"> {shortId(watch.pair_id)}</small>
      </td>
      <td>{watch.chain}</td>
      <td>{watch.venue}</td>
      <td>
        {snapshot ? formatUsd(snapshot.price_usd, snapshot.price_status) : "—"}
      </td>
      <td>
        {snapshot
          ? formatUsd(snapshot.liquidity_usd, snapshot.liquidity_status)
          : "—"}
      </td>
      <td>
        {snapshot
          ? formatUsd(snapshot.volume_usd, snapshot.volume_status)
          : "—"}
      </td>
      <td>
        {latest ? (
          latest.status === "FAILED" ? (
            <StatusBadge tone="red">FAILED</StatusBadge>
          ) : (
            <StatusBadge tone={classificationTone(latest.classification)}>
              {latest.classification}
            </StatusBadge>
          )
        ) : (
          <span className="scout-muted">not yet</span>
        )}
      </td>
      <td>{latest?.strength ?? "—"}</td>
      <td>
        <StatusBadge tone={statusTone(watch.status)}>
          {watch.status}
        </StatusBadge>
      </td>
      <td>{formatRelative(watch.next_orbit_review_at, now)}</td>
      <td>{watch.latest_vector_sufficiency ?? "not checked"}</td>
      <td>{new Date(watch.first_seen_at).toLocaleString("en-GB")}</td>
    </tr>
  );
}

export function WatchTable({
  page,
  now,
  selected,
  onSelect,
}: {
  page: WatchPage;
  now: number;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="table-scroll scout-table">
      <table>
        <caption className="sr-only">
          Discovery watches, newest discovery first
        </caption>
        <thead>
          <tr>
            <th>Age</th>
            <th>Pair</th>
            <th>Chain</th>
            <th>Venue</th>
            <th>Price</th>
            <th>Liquidity</th>
            <th>24h volume</th>
            <th>ORBIT</th>
            <th>Strength</th>
            <th>Status</th>
            <th>Next ORBIT</th>
            <th>VECTOR</th>
            <th>First seen</th>
          </tr>
        </thead>
        <tbody>
          {page.items.map((watch) => (
            <Row
              key={watch.watch_id}
              watch={watch}
              now={now}
              selected={watch.watch_id === selected}
              onSelect={onSelect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
