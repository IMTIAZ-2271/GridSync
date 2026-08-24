import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  api,
  formatKwh,
  queryKeys,
  subtractDecimals,
  sumDecimals,
  toNumber,
  type AreaStats,
} from "../lib/api";
import { CHART_INK, SERIES } from "../lib/series";
import { useAuth } from "../auth/AuthContext";
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
 * The regulator's view: net-metering uptake and energy balance per district.
 *
 * The question this page answers is which districts send more to the grid than
 * they draw from it, so import and export are plotted against **one** kWh axis
 * and the rows are sorted by export -- a magnitude comparison reads as a
 * ranking or it does not read at all.
 *
 * Import and export wear the same two colours here as on the customer's own
 * chart. Colour follows the entity, never the view: a reader who has learned
 * that orange is "sent to the grid" must not have to relearn it one portal
 * over. (Slots validated as a pair against this surface: worst CVD dE 24.7,
 * both above 3:1 contrast.)
 *
 * Government requirements 2 and 4 are both answered here, and they pull in
 * opposite directions: "monitor consumption within their own region" and
 * "observe total overall power usage". Scoping the endpoint to the official's
 * district by role would have satisfied the first by breaking the second -- and
 * an official who cannot see the national picture cannot tell whether their own
 * district is doing well or badly. So the scope is a control on the page, it
 * defaults to their own district (which is the question they open this page
 * with), and the whole country is one click away.
 */

export default function GovernmentByArea() {
  const { account } = useAuth();
  // Their own district comes from /api/auth/me, which the session already
  // holds -- no second request to find out who is asking.
  const home = account?.government_district ?? null;
  const [scope, setScope] = useState<"mine" | "all">(home ? "mine" : "all");
  const district = scope === "mine" ? home : null;

  const areas = useQuery({
    queryKey: queryKeys.analyticsByArea(district ?? undefined),
    queryFn: () => api.analyticsByArea(district ?? undefined),
  });

  const totals = useMemo(() => {
    const rows = areas.data ?? [];
    if (rows.length === 0) return null;
    const imported = sumDecimals(rows.map((r) => r.total_import_kwh));
    const exported = sumDecimals(rows.map((r) => r.total_export_kwh));
    return {
      sites: rows.reduce((n, r) => n + r.site_count, 0),
      solarSites: rows.reduce((n, r) => n + r.solar_site_count, 0),
      imported,
      exported,
      // Exact, not Number(a) - Number(b): this figure is displayed.
      net: subtractDecimals(exported, imported),
    };
  }, [areas.data]);

  return (
    <div className="space-y-6">
      {/* Only an official has a district; a supplier reads this page too and
          has no "mine" to switch to. */}
      {home && (
        <nav
          className="flex flex-wrap items-center gap-1"
          aria-label="Reporting area"
        >
          {(
            [
              { id: "mine" as const, label: home },
              { id: "all" as const, label: "All districts" },
            ]
          ).map((o) => (
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
          <span className="ml-2 text-xs text-ink-muted">
            {scope === "mine"
              ? "The district your official code was issued for"
              : "Every district on the grid"}
          </span>
        </nav>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {areas.isPending || !totals ? (
          <>
            <StatSkeleton />
            <StatSkeleton />
            <StatSkeleton />
          </>
        ) : (
          <>
            <Stat
              label="Sites on the grid"
              value={String(totals.sites)}
              unit="sites"
              footnote={
                scope === "mine" && home
                  ? `In ${home}`
                  : `Across ${areas.data!.length} districts`
              }
            />
            <Stat
              label="With solar"
              value={String(totals.solarSites)}
              unit="sites"
              footnote={`${Math.round((totals.solarSites / totals.sites) * 100)}% of sites have a live array`}
            />
            <Stat
              label="Net position"
              value={formatKwh(totals.net, 1)}
              unit="kWh"
              accent={
                totals.net.startsWith("-") ? SERIES.import.hex : SERIES.export.hex
              }
              footnote={
                totals.net.startsWith("-")
                  ? "Drawn from the grid, net of everything exported"
                  : "Sent to the grid, net of everything drawn"
              }
            />
          </>
        )}
      </div>

      <Card>
        <CardHeader
          title="Import against export, by district"
          subtitle="All recorded telemetry, in kWh. Sorted by export."
        />
        <div className="p-5">
          {areas.isPending ? (
            <Skeleton className="h-80 w-full" />
          ) : areas.error ? (
            <ErrorState error={areas.error} />
          ) : areas.data.length === 0 ? (
            <EmptyState title="No districts" hint="No site has been registered yet." />
          ) : (
            <AreaBars areas={areas.data} />
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Districts"
          subtitle="The same figures, exactly as stored."
        />
        {areas.isPending ? (
          <div className="space-y-3 p-5">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : areas.error ? null : (
          <AreaTable areas={areas.data} />
        )}
      </Card>
    </div>
  );
}

interface Row {
  district: string;
  import_kwh: number;
  export_kwh: number;
}

function AreaBars({ areas }: { areas: AreaStats[] }) {
  // toNumber at the axis boundary only -- the exact strings go to the table.
  const data: Row[] = [...areas]
    .sort((a, b) => toNumber(b.total_export_kwh) - toNumber(a.total_export_kwh))
    .map((a) => ({
      district: a.district,
      import_kwh: toNumber(a.total_import_kwh),
      export_kwh: toNumber(a.total_export_kwh),
    }));

  return (
    <div className="w-full">
      {/* Height grows with the row count so the bars keep a constant weight
          instead of thinning out as districts are added. */}
      <div className="w-full" style={{ height: Math.max(240, data.length * 46) }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 4, right: 24, bottom: 4, left: 0 }}
            barGap={2}
            barCategoryGap="28%"
          >
            {/* Vertical only on a horizontal chart: the gridline should run
                along the measure, not across the categories. */}
            <CartesianGrid
              stroke={CHART_INK.grid}
              strokeWidth={1}
              horizontal={false}
            />
            <XAxis
              type="number"
              tick={{ fill: CHART_INK.muted, fontSize: 11 }}
              tickLine={false}
              axisLine={{ stroke: CHART_INK.axis }}
              tickFormatter={(v: number) =>
                v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v)
              }
            />
            <YAxis
              type="category"
              dataKey="district"
              width={104}
              tick={{ fill: CHART_INK.secondary, fontSize: 11 }}
              tickLine={false}
              axisLine={false}
            />
            <Tooltip
              cursor={{ fill: CHART_INK.grid, fillOpacity: 0.4 }}
              content={<AreaTooltip />}
            />
            <Bar
              dataKey="import_kwh"
              name={SERIES.import.label}
              fill={SERIES.import.hex}
              radius={[0, 4, 4, 0]}
              isAnimationActive={false}
            />
            <Bar
              dataKey="export_kwh"
              name={SERIES.export.label}
              fill={SERIES.export.hex}
              radius={[0, 4, 4, 0]}
              isAnimationActive={false}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Legend. Two series, so identity is never carried by colour alone. */}
      <div className="mt-3 flex flex-wrap justify-end gap-4">
        {([SERIES.import, SERIES.export] as const).map((s) => (
          <span
            key={s.key}
            className="inline-flex items-center gap-1.5 text-xs text-ink-2"
          >
            <span
              aria-hidden
              className="h-2 w-2 rounded-sm"
              style={{ backgroundColor: s.hex }}
            />
            {s.label}
            <span className="text-ink-muted">· {s.hint}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

interface TooltipProps {
  active?: boolean;
  payload?: { payload: Row }[];
}

function AreaTooltip({ active, payload }: TooltipProps) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  const net = row.export_kwh - row.import_kwh;

  return (
    <div className="rounded-lg border border-hairline bg-surface px-3 py-2 shadow-sm">
      <p className="text-xs font-medium text-ink">{row.district}</p>
      <dl className="mt-1.5 space-y-1">
        {(
          [
            [SERIES.import, row.import_kwh],
            [SERIES.export, row.export_kwh],
          ] as const
        ).map(([s, value]) => (
          <div key={s.key} className="flex items-center gap-2 text-xs">
            <span
              aria-hidden
              className="h-2 w-2 shrink-0 rounded-sm"
              style={{ backgroundColor: s.hex }}
            />
            <dt className="text-ink-muted">{s.label}</dt>
            <dd className="tabular ml-auto font-medium text-ink">
              {value.toFixed(1)}
            </dd>
          </div>
        ))}
      </dl>
      <p className="mt-1.5 border-t border-hairline pt-1.5 text-xs text-ink-muted">
        Net{" "}
        <span className="tabular font-medium text-ink-2">
          {net > 0 ? "+" : ""}
          {net.toFixed(1)}
        </span>{" "}
        kWh
      </p>
    </div>
  );
}

/**
 * The table view. Not a fallback -- the chart ranks, the table states, and the
 * figures here are the exact NUMERIC strings the API sent rather than anything
 * that has been through a double on its way to the axis.
 */
function AreaTable({ areas }: { areas: AreaStats[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-hairline text-xs font-medium tracking-wide text-ink-muted uppercase">
            <th className="px-5 py-3 text-left">District</th>
            <th className="px-3 py-3 text-right">Sites</th>
            <th className="px-3 py-3 text-right">With solar</th>
            <th className="px-3 py-3 text-right">Import</th>
            <th className="px-3 py-3 text-right">Export</th>
            <th className="px-5 py-3 text-right">Generation</th>
          </tr>
        </thead>
        <tbody>
          {areas.map((a) => (
            <tr
              key={a.district}
              className="border-b border-hairline last:border-0"
            >
              <td className="px-5 py-3 font-medium text-ink">{a.district}</td>
              <td className="tabular px-3 py-3 text-right text-ink-2">
                {a.site_count}
              </td>
              <td className="tabular px-3 py-3 text-right text-ink-2">
                {a.solar_site_count}
              </td>
              <td className="tabular px-3 py-3 text-right text-ink-2">
                {formatKwh(a.total_import_kwh, 1)}
              </td>
              <td className="tabular px-3 py-3 text-right text-ink-2">
                {formatKwh(a.total_export_kwh, 1)}
              </td>
              <td className="tabular px-5 py-3 text-right text-ink-2">
                {formatKwh(a.total_generation_kwh, 1)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
