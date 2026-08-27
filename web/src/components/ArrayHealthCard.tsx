import { useQuery } from "@tanstack/react-query";

import { api, formatKwh, queryKeys, toNumber, type ArrayHealth } from "../lib/api";
import { Badge, Card, CardHeader, Skeleton } from "../components/ui";

/**
 * Consumer requirement 8, as decision 2 settled it: health per **array**, not
 * per panel.
 *
 * The honest limit is worth stating on the screen rather than only in a commit
 * message. An inverter reports one generation figure for everything wired to
 * it; nothing in this system knows a panel exists. A per-panel verdict would be
 * arithmetic dressed as measurement, and the first time it said "panel 7 is
 * failing" it would be inventing a fact. So the card says what is actually
 * known, and says why that is the level.
 *
 * Two signals, and only two:
 *
 * **Reporting** — is the inverter sending intervals at all. This catches a dead
 * array, and nothing else.
 *
 * **Yield** — kWh produced per kW of installed capacity over the window. This
 * is the one that catches the fault that matters: a shaded, soiled or
 * partly-failed array still reports, it just reports less, and raw kWh cannot
 * tell you that because a big array always beats a small one. It is withheld
 * entirely when one inverter carries several arrays, because then the
 * generation figure does not say which array produced what.
 */

/** Seven days in Dhaka. Below this a working array is doing badly enough to
 *  look at; well below it, something is wrong. Deliberately generous — this is
 *  a prompt to check, not a diagnosis. */
const YIELD_LOW = 15;
const YIELD_POOR = 8;

export default function ArrayHealthCard({ siteId }: { siteId: string }) {
  const arrays = useQuery({
    queryKey: queryKeys.siteArrays(siteId),
    queryFn: () => api.siteArrays(siteId),
  });

  // A site with no panels gets no card at all, rather than an empty one
  // explaining what it would have shown.
  if (!arrays.isPending && (arrays.data ?? []).length === 0) return null;

  return (
    <Card>
      <CardHeader
        title="Solar arrays"
        subtitle="Per array, over the last 7 complete days"
      />
      {arrays.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-20 w-full" />
        </div>
      ) : (
        <ul className="divide-y divide-hairline">
          {(arrays.data ?? []).map((a) => (
            <ArrayRow key={a.array_id} array={a} />
          ))}
        </ul>
      )}
      <p className="border-t border-hairline px-5 py-3 text-xs text-ink-muted">
        Your inverter reports one figure for the whole array, so panels are not
        monitored individually. A drop in output per kW is the sign that
        something on the roof needs looking at.
      </p>
    </Card>
  );
}

function ArrayRow({ array: a }: { array: ArrayHealth }) {
  const covered =
    a.intervals_expected > 0
      ? (a.intervals_received / a.intervals_expected) * 100
      : null;
  const reporting = covered !== null && covered >= 90;
  const yieldValue = a.specific_yield_kwh_per_kw
    ? toNumber(a.specific_yield_kwh_per_kw)
    : null;

  const verdict =
    covered === null || covered === 0
      ? { tone: "critical", label: "not reporting" }
      : !reporting
        ? { tone: "warning", label: "gaps in reporting" }
        : yieldValue === null
          ? { tone: "neutral", label: "reporting" }
          : yieldValue < YIELD_POOR
            ? { tone: "critical", label: "output very low" }
            : yieldValue < YIELD_LOW
              ? { tone: "warning", label: "output low" }
              : { tone: "good", label: "healthy" };

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 font-medium text-ink">
            {a.label ?? "Array"}
            <Badge tone={verdict.tone}>{verdict.label}</Badge>
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {formatKwh(a.dc_capacity_kw, 2)} kW
            {a.panel_count && ` · ${a.panel_count} panels`}
            {a.panel_watt_peak && ` × ${a.panel_watt_peak} W`}
            {a.commissioned_on &&
              ` · fitted ${new Date(a.commissioned_on).toLocaleDateString(undefined, { year: "numeric", month: "short" })}`}
            {a.installed_by_supplier_name && ` by ${a.installed_by_supplier_name}`}
          </p>
        </div>
      </div>

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1.5 text-xs">
        <div>
          <dt className="text-ink-muted">Produced</dt>
          <dd className="tabular mt-0.5 font-medium text-ink-2">
            {formatKwh(a.generation_kwh, 1)} kWh
          </dd>
        </div>
        <div>
          <dt className="text-ink-muted">Per kW installed</dt>
          <dd className="tabular mt-0.5 font-medium text-ink-2">
            {yieldValue !== null ? `${a.specific_yield_kwh_per_kw} kWh` : "—"}
          </dd>
        </div>
        <div>
          <dt className="text-ink-muted">Intervals reported</dt>
          <dd className="tabular mt-0.5 font-medium text-ink-2">
            {a.intervals_received} of {a.intervals_expected}
          </dd>
        </div>
        {a.tilt_deg !== null && (
          <div>
            <dt className="text-ink-muted">Tilt / facing</dt>
            <dd className="tabular mt-0.5 font-medium text-ink-2">
              {a.tilt_deg}° / {a.azimuth_deg}°
            </dd>
          </div>
        )}
      </dl>

      {!a.sole_array_on_inverter && (
        <p className="mt-2 text-xs text-ink-muted">
          This array shares an inverter with another, so its output cannot be
          measured separately — the figures above cover both.
        </p>
      )}
    </li>
  );
}
