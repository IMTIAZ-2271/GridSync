import { useQuery } from "@tanstack/react-query";

import {
  api,
  formatKwh,
  formatMoney,
  queryKeys,
  type SiteSummary,
} from "../lib/api";
import { SERIES, type SeriesId } from "../lib/series";
import { useSelectedSite } from "../components/SitePicker";
import ReadingsChart from "../components/ReadingsChart";
import {
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
  Stat,
  StatSkeleton,
} from "../components/ui";

const READING_DAYS = 7;

export default function CustomerOverview() {
  const { siteId, site } = useSelectedSite();

  const summary = useQuery({
    queryKey: queryKeys.siteSummary(siteId!),
    queryFn: () => api.siteSummary(siteId!),
    enabled: !!siteId,
  });

  const readings = useQuery({
    queryKey: queryKeys.siteReadings(siteId!, READING_DAYS),
    queryFn: () => api.siteReadings(siteId!, READING_DAYS),
    enabled: !!siteId,
  });

  // A site with no panels has no generation series to draw. Dropping it must
  // not shift import or export onto different colours.
  const series: SeriesId[] = site?.has_solar
    ? ["import", "export", "generation"]
    : ["import", "export"];

  return (
    <div className="space-y-6">
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
          title={`Last ${READING_DAYS} days`}
          subtitle="Half-hourly intervals, in kWh"
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
              hint="The meter has not reported in the last 7 days. If that is unexpected, raise a data gap issue."
            />
          ) : (
            <ReadingsChart readings={readings.data} series={series} />
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
              {" · "}
              {bill.amount_due === "0.0000" ? (
                <span className="font-medium text-status-good-text">
                  Covered by credit
                </span>
              ) : (
                <>due {bill.due_date ?? "—"}</>
              )}
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
