import { StatusBadge } from "@/components/console/ui";
import { formatUsd, shortId, type PaperPortfolio } from "@/lib/scout";
import { Empty } from "./states";

// Booked ledger state only. Nothing here is a fixture and nothing is estimated.
export function PaperSection({ portfolio }: { portfolio: PaperPortfolio }) {
  const open = portfolio.positions.filter((item) => item.open);
  const latest = portfolio.pnl[0];
  return (
    <div className="scout-paper">
      <p className="scout-note">
        Paper ledger · booked state only
        {portfolio.account
          ? ` · cash ${formatUsd(portfolio.account.cash_usd)} of ${formatUsd(portfolio.account.initial_cash_usd)}${portfolio.account.paused ? " · PAUSED" : ""}`
          : ""}
      </p>
      <h4>Open positions</h4>
      {open.length === 0 ? (
        <Empty>No paper positions yet.</Empty>
      ) : (
        <div className="table-scroll scout-table">
          <table>
            <thead>
              <tr>
                <th>Asset</th>
                <th>Market</th>
                <th>Quantity</th>
                <th>Cost basis</th>
                <th>Realized P&amp;L</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {open.map((item) => (
                <tr key={item.position_id}>
                  <td className="scout-mono">{shortId(item.asset_id)}</td>
                  <td className="scout-mono">
                    {item.market_pair_id ? shortId(item.market_pair_id) : "—"}
                  </td>
                  <td>{item.quantity}</td>
                  <td>{formatUsd(item.cost_basis_usd)}</td>
                  <td>{formatUsd(item.realized_pnl_usd)}</td>
                  <td>{new Date(item.updated_at).toLocaleString("en-GB")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h4>Recent paper fills</h4>
      {portfolio.fills.length === 0 ? (
        <Empty>No paper fills yet.</Empty>
      ) : (
        <div className="table-scroll scout-table">
          <table>
            <thead>
              <tr>
                <th>Filled</th>
                <th>Side</th>
                <th>Trade case</th>
                <th>Quantity</th>
                <th>Price</th>
                <th>Notional</th>
                <th>Fees</th>
              </tr>
            </thead>
            <tbody>
              {portfolio.fills.map((fill) => (
                <tr key={fill.execution_id}>
                  <td>{new Date(fill.filled_at).toLocaleString("en-GB")}</td>
                  <td>
                    <StatusBadge tone={fill.side === "BUY" ? "blue" : "purple"}>
                      {fill.side} · {fill.mode}
                    </StatusBadge>
                  </td>
                  <td>
                    <a
                      className="scout-link"
                      href={`/api/cockpit/trade-cases/${fill.trade_case_id}`}
                    >
                      {shortId(fill.trade_case_id)}
                    </a>
                  </td>
                  <td>{fill.quantity}</td>
                  <td>{formatUsd(fill.execution_price_usd)}</td>
                  <td>{formatUsd(fill.notional_usd)}</td>
                  <td>{formatUsd(fill.fees_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h4>P&amp;L</h4>
      {latest ? (
        <p>
          Equity {formatUsd(latest.snapshot.equity_usd)} · realized{" "}
          {formatUsd(latest.snapshot.realized_pnl_usd)} · unrealized{" "}
          {formatUsd(latest.snapshot.unrealized_pnl_usd)} · recorded{" "}
          {new Date(latest.recorded_at).toLocaleString("en-GB")}
        </p>
      ) : (
        <Empty>No P&amp;L snapshot recorded.</Empty>
      )}
    </div>
  );
}
