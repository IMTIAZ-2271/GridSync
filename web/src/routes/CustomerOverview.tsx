import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  api,
  formatKwh,
  formatMoney,
  queryKeys,
  type SiteSummary,
  type TimeframeId,
} from "../lib/api";
import { READING_VIEWS, SERIES, type ReadingViewId } from "../lib/series";
import { useSelectedSite } from "../components/SitePicker";
import ScopePicker, { type ScopeOption } from "../components/ScopePicker";
import ReadingsChart from "../components/ReadingsChart";
import CustomerOnboarding from "./CustomerOnboarding";
import {
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
  Stat,
  StatSkeleton,
} from "../components/ui";

/**
 * Timeframes, in the order a household thinks about them.
 *
 * The windows are rolling rather than calendar-anchored, and the subtitle says
 * so rather than leaving it to be guessed: "this week" has no agreed start day
 * here, and a Monday-anchored week would show two bars on a Tuesday and be
 * read as a collapse in consumption. The bucket size travels with the window
 * and is decided server-side -- see site_readings in
 * db/sql/dao/site_queries.sql.
 */
const TIMEFRAMES: { id: TimeframeId; label: string; window: string }[] = [
  { id: "day", label: "Day", window: "last 24 hours, hourly" },
  { id: "week", label: "Week", window: "last 7 days, half-hourly" },
  { id: "month", label: "Month", window: "last 30 days, daily" },
  { id: "year", label: "Year", window: "last 12 months, monthly" },
];

export default function CustomerOverview() {
  const { siteId, site, sites, isPending: sitesPending } = useSelectedSite();

  const [timeframe, setTimeframe] = useState<TimeframeId>("week");
  // null = the whole site. Consumer requirement 4's "overall usage".
  const [pointId, setPointId] = useState<string | null>(null);

  // enabled: !!siteId already keeps these from firing with no site selected,
  // so it is safe to call them unconditionally -- the onboarding branch below
  // is a render choice, not an early return, and must not change how many
  // hooks this component calls between renders.
  const points = useQuery({
    queryKey: queryKeys.sitePoints(siteId!),
    queryFn: () => api.listBillingPoints(siteId!),
    enabled: !!siteId,
  });

  const summary = useQuery({
    queryKey: queryKeys.siteSummary(siteId!, pointId),
    queryFn: () => api.siteSummary(siteId!, pointId),
    enabled: !!siteId,
  });

  const readings = useQuery({
    queryKey: queryKeys.siteReadings(siteId!, timeframe, pointId),
    queryFn: () => api.siteReadings(siteId!, timeframe, pointId),
    enabled: !!siteId,
  });

  const connections = points.data ?? [];
  const scoped = pointId
    ? connections.find((p) => p.point_id === pointId)
    : undefined;

  // "Whole site" plus one row per connection, each carrying the serial of the
  // meter measuring it so either name finds it. Offered only when there is
  // more than one connection: a single-meter household choosing between "the
  // site" and "its only meter" is choosing between two names for one thing.
  const scopeOptions = useMemo<ScopeOption[]>(
    () => [
      { id: null, label: "Whole site", detail: `${connections.length} meters` },
      ...connections.map((p) => ({
        id: p.point_id,
        label: p.label,
        detail: p.meter_serial ?? "no meter",
      })),
    ],
    [connections],
  );

  // Solar is a property of what is currently in scope, not of the site: a
  // household with panels on one connection must not be offered an empty solar
  // chart for the other. An empty chart is a dead end, not an interface.
  const hasSolar = scoped ? scoped.has_solar : Boolean(site?.has_solar);
  const [view, setView] = useState<ReadingViewId>("consumption");
  const views = READING_VIEWS.filter(
    (v) => v.id === "consumption" || hasSolar,
  );
  const active = views.find((v) => v.id === view) ?? views[0];

  const frame = TIMEFRAMES.find((t) => t.id === timeframe)!;

  // A newly registered customer (empty meter serial) owns no site at all --
  // an empty dashboard would just look broken. Walk them through building
  // one instead of rendering stat tiles for data that does not exist.
  if (!sitesPending && sites && sites.length === 0) {
    return <CustomerOnboarding />;
  }

  return (
    <div className="space-y-6">
      {connections.length > 1 && (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="mr-auto text-xs text-ink-2">
            {scoped
              ? `Showing ${scoped.label} only — its own readings, bill and credit.`
              : "Showing every connection at this site, added together."}
          </span>
          <ScopePicker
            options={scopeOptions}
            value={pointId}
            onChange={setPointId}
            label="Which connection"
          />
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {summary.isPending || !siteId ? (
          <>
            <StatSkeleton />
            <StatSkeleton />
            <StatSkeleton />
          </>
        ) : summary.error ? (
          <Card className="lg:col-span-3">
            <ErrorState error={summary.error} />
          </Card>
        ) : (
          <SummaryStats summary={summary.data} />
        )}
      </div>

      <Card>
        <CardHeader
          title={frame.label}
          subtitle={`${active.blurb} · ${frame.window}, in kWh`}
          action={
            <div className="flex flex-wrap items-center gap-2">
              {views.length > 1 && (
                <nav className="flex gap-1" aria-label="Reading type">
                  {views.map((v) => (
                    <button
                      key={v.id}
                      type="button"
                      onClick={() => setView(v.id)}
                      aria-current={active.id === v.id}
                      className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                        active.id === v.id
                          ? "bg-portal-customer text-white"
                          : "text-ink-2 hover:bg-hairline/60"
                      }`}
                    >
                      {v.label}
                    </button>
                  ))}
                </nav>
              )}
              <nav
                className="flex gap-1 rounded-md bg-plane p-0.5"
                aria-label="Timeframe"
              >
                {TIMEFRAMES.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => setTimeframe(t.id)}
                    aria-current={timeframe === t.id}
                    className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                      timeframe === t.id
                        ? "bg-surface text-ink shadow-sm"
                        : "text-ink-2 hover:text-ink"
                    }`}
                  >
                    {t.label}
                  </button>
                ))}
              </nav>
            </div>
          }
        />
        <div className="p-5">
          {readings.isPending || !siteId ? (
            <div className="space-y-3">
              <Skeleton className="h-72 w-full" />
              <div className="flex justify-end gap-4">
                <Skeleton className="h-3 w-20" />
                <Skeleton className="h-3 w-20" />
                <Skeleton className="h-3 w-24" />
              </div>
            </div>
          ) : readings.error ? (
            <ErrorState error={readings.error} />
          ) : readings.data.length === 0 ? (
            <EmptyState
              title="No readings in this window"
              hint={
                timeframe === "day"
                  ? "Nothing recorded in the last 24 hours. Try a longer timeframe."
                  : "The meter has not reported in this window. If that is unexpected, raise a data gap issue."
              }
            />
          ) : (
            <ReadingsChart
              readings={readings.data}
              series={active.series}
              timeframe={timeframe}
            />
          )}
        </div>
      </Card>
    </div>
  );
}

function SummaryStats({ summary }: { summary: SiteSummary }) {
  const bill = summary.latest_bill;
  const w = summary.last_30_days;

  return (
    <>
      <Stat
        label="Credit balance"
        value={formatKwh(summary.credit_balance_kwh, 1)}
        unit="kWh"
        footnote={
          <>
            Worth {formatMoney(summary.credit_balance_amount)} at the current
            export rate
          </>
        }
      />

      <Stat
        label="Latest bill"
        value={bill ? formatKwh(bill.amount_due, 2) : "—"}
        unit={bill ? bill.currency : undefined}
        footnote={
          bill ? (
            <>
              {new Date(bill.period_start).toLocaleDateString(undefined, {
                month: "long",
                year: "numeric",
              })}
              {" · "}due {bill.due_date ?? "—"}
            </>
          ) : (
            "This site has not been billed yet"
          )
        }
      />

      <Stat
        label="Last 30 days"
        value={formatKwh(w.import_kwh, 1)}
        unit="kWh imported"
        accent={SERIES.import.hex}
        footnote={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden
                className="h-0.5 w-3 rounded-full"
                style={{ backgroundColor: SERIES.export.hex }}
              />
              <span className="tabular">{formatKwh(w.export_kwh, 1)}</span>{" "}
              exported
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden
                className="h-0.5 w-3 rounded-full"
                style={{ backgroundColor: SERIES.generation.hex }}
              />
              <span className="tabular">{formatKwh(w.generation_kwh, 1)}</span>{" "}
              generated
            </span>
          </span>
        }
      />
    </>
  );
}
