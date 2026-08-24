import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  api,
  formatKwh,
  formatMoney,
  queryKeys,
  sumDecimals,
  toNumber,
  type NetMeteringOutcome,
} from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
  Stat,
  StatSkeleton,
} from "../components/ui";

/**
 * Government requirement 5: what net metering actually produced.
 *
 * The by-area page shows energy moving in both directions. This shows the
 * consequence — credit earned for exporting, credit spent against a bill, and
 * the balance rolling forward. It is the one view of `credit_ledger` that
 * nobody had built, and the ledger is the thing this whole application exists
 * to demonstrate.
 *
 * **The headline figure is the share of earned credit that has been used.** A
 * scheme that hands out credit nobody can spend looks generous in the export
 * column and is not; that number is what tells a regulator the difference, and
 * it is why it sits above the raw totals rather than in a column of the table.
 *
 * Balance is deliberately not earned-minus-applied. It is the sum of each
 * connection's latest running ledger balance — what the next bill can actually
 * spend. The two agree today and diverge the moment an expiry or an adjustment
 * is written, and when they diverge the running balance is the true one.
 */
export default function GovernmentNetMetering() {
  const { account } = useAuth();
  const home = account?.government_district ?? null;
  const [scope, setScope] = useState<"mine" | "all">(home ? "mine" : "all");
  const district = scope === "mine" ? home : null;

  const report = useQuery({
    queryKey: queryKeys.netMetering(district ?? undefined),
    queryFn: () => api.netMetering(district ?? undefined),
  });

  const totals = useMemo(() => {
    const rows = report.data?.by_area ?? [];
    if (rows.length === 0) return null;
    const earned = sumDecimals(rows.map((r) => r.earned_kwh));
    const applied = sumDecimals(rows.map((r) => r.applied_kwh));
    const balance = sumDecimals(rows.map((r) => r.balance_kwh));
    const e = toNumber(earned);
    return {
      earned,
      applied,
      balance,
      earnedAmount: sumDecimals(rows.map((r) => r.earned_amount)),
      appliedAmount: sumDecimals(rows.map((r) => r.applied_amount)),
      balanceAmount: sumDecimals(rows.map((r) => r.balance_amount)),
      usedPct: e > 0 ? (toNumber(applied) / e) * 100 : null,
      sites: rows.reduce((n, r) => n + r.site_count, 0),
      inCredit: rows.reduce((n, r) => n + r.sites_in_credit, 0),
    };
  }, [report.data]);

  const agreements = report.data?.agreements ?? [];

  return (
    <div className="space-y-6">
      {home && (
        <nav className="flex flex-wrap items-center gap-1" aria-label="Reporting area">
          {[
            { id: "mine" as const, label: home },
            { id: "all" as const, label: "All districts" },
          ].map((o) => (
            <button
              key={o.id}
              type="button"
              onClick={() => setScope(o.id)}
              aria-current={scope === o.id}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                scope === o.id
                  ? "bg-portal-government text-white"
                  : "text-ink-2 hover:bg-hairline/60"
              }`}
            >
              {o.label}
            </button>
          ))}
        </nav>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {report.isPending || !totals ? (
          <>
            <StatSkeleton />
            <StatSkeleton />
            <StatSkeleton />
          </>
        ) : (
          <>
            <Stat
              label="Credit put to use"
              value={totals.usedPct === null ? "—" : `${totals.usedPct.toFixed(0)}%`}
              unit={totals.usedPct === null ? undefined : "of what was earned"}
              footnote={
                <>
                  {formatKwh(totals.applied, 0)} kWh spent against bills, of{" "}
                  {formatKwh(totals.earned, 0)} kWh earned
                </>
              }
            />
            <Stat
              label="Credit earned"
              value={formatKwh(totals.earned, 0)}
              unit="kWh"
              footnote={<>Worth {formatMoney(totals.earnedAmount)} at the rates it was earned at</>}
            />
            <Stat
              label="Still held"
              value={formatKwh(totals.balance, 0)}
              unit="kWh"
              footnote={
                <>
                  {formatMoney(totals.balanceAmount)} rolling forward ·{" "}
                  {totals.inCredit} of {totals.sites} sites in credit
                </>
              }
            />
          </>
        )}
      </div>

      <Card>
        <CardHeader
          title="By district"
          subtitle="Districts where at least one household is on the scheme"
        />
        {report.isPending ? (
          <div className="space-y-3 p-5">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : report.error ? (
          <ErrorState error={report.error} />
        ) : (report.data?.by_area ?? []).length === 0 ? (
          <EmptyState
            title="No net metering here yet"
            hint="A district appears once a household on it holds a net-metering agreement. Others are still on the energy rollup."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-hairline text-left text-xs text-ink-muted">
                  <th className="px-5 py-2 font-medium">District</th>
                  <th className="px-5 py-2 text-right font-medium">Sites</th>
                  <th className="px-5 py-2 text-right font-medium">Earned kWh</th>
                  <th className="px-5 py-2 text-right font-medium">Used kWh</th>
                  <th className="px-5 py-2 text-right font-medium">Held kWh</th>
                  <th className="px-5 py-2 text-right font-medium">Used</th>
                </tr>
              </thead>
              <tbody>
                {(report.data?.by_area ?? []).map((r) => (
                  <Row key={r.district} row={r} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {agreements.length > 0 && (
        <Card>
          <CardHeader
            title="Agreements"
            subtitle="Who has been let onto the scheme, and for how much capacity"
          />
          <ul className="flex flex-wrap gap-6 p-5">
            {agreements.map((a) => (
              <li key={a.status}>
                <p className="text-xs uppercase tracking-wide text-ink-muted">
                  {a.status}
                </p>
                <p className="tabular mt-0.5 text-lg font-medium text-ink">
                  {a.agreement_count}
                </p>
                <p className="text-xs text-ink-muted">
                  {formatKwh(a.sanctioned_capacity_kw, 1)} kW sanctioned
                </p>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function Row({ row }: { row: NetMeteringOutcome }) {
  const pct = row.applied_pct === null ? null : toNumber(row.applied_pct);

  return (
    <tr className="border-b border-hairline last:border-0">
      <td className="px-5 py-3 font-medium text-ink">{row.district}</td>
      <td className="tabular px-5 py-3 text-right text-ink-2">
        {row.sites_in_credit}/{row.site_count}
      </td>
      <td className="tabular px-5 py-3 text-right text-ink-2">
        {formatKwh(row.earned_kwh, 0)}
      </td>
      <td className="tabular px-5 py-3 text-right text-ink-2">
        {formatKwh(row.applied_kwh, 0)}
      </td>
      <td className="tabular px-5 py-3 text-right text-ink-2">
        {formatKwh(row.balance_kwh, 0)}
      </td>
      <td className="px-5 py-3 text-right">
        {/* Never colour alone: the percentage is spelled out beside the tone. */}
        {pct === null ? (
          <span className="text-ink-muted">—</span>
        ) : (
          <Badge tone={pct >= 50 ? "good" : pct >= 25 ? "neutral" : "warning"}>
            {pct.toFixed(0)}%
          </Badge>
        )}
      </td>
    </tr>
  );
}
