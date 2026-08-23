import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  api,
  formatKwh,
  queryKeys,
  type DeviceHealth,
  type SiteDevice,
} from "../lib/api";
import { HEALTH, needsAttention, roleOf, worstHealth } from "../lib/devices";
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
 * Fleet equipment inventory.
 *
 * One request, not one per site: `GET /api/devices` runs the same
 * `device_health` statement the customer's equipment page runs, with the site
 * filter left NULL. Rendering this table from the per-site route would be N+1
 * requests for a single screen.
 *
 * Sorted worst-first and defaulting to the attention filter, because the
 * utility's question here is not "what do we own" -- the site list answers
 * that -- it is "what has stopped talking to us".
 */

const ORDER: DeviceHealth[] = [
  "faulty",
  "no_data",
  "silent",
  "degraded",
  "unknown",
  "healthy",
];

export default function SupplierEquipment() {
  const [filter, setFilter] = useState<DeviceHealth | "attention" | "all">(
    "attention",
  );

  const devices = useQuery({
    queryKey: queryKeys.fleetDevices(),
    queryFn: api.fleetDevices,
  });

  const counts = useMemo(() => {
    const tally = {} as Record<DeviceHealth, number>;
    for (const h of ORDER) tally[h] = 0;
    for (const d of devices.data ?? []) tally[d.health] += 1;
    return tally;
  }, [devices.data]);

  const attentionCount = (devices.data ?? []).filter((d) =>
    needsAttention(d.health),
  ).length;

  const siteCount = new Set((devices.data ?? []).map((d) => d.site_id)).size;
  const sitesNeedingAttention = useMemo(() => {
    const bySite = new Map<string, SiteDevice[]>();
    for (const d of devices.data ?? []) {
      bySite.set(d.site_id, [...(bySite.get(d.site_id) ?? []), d]);
    }
    let n = 0;
    for (const group of bySite.values()) {
      const worst = worstHealth(group);
      if (worst && needsAttention(worst)) n += 1;
    }
    return n;
  }, [devices.data]);

  const shown = useMemo(() => {
    const all = devices.data ?? [];
    const filtered =
      filter === "all"
        ? all
        : filter === "attention"
          ? all.filter((d) => needsAttention(d.health))
          : all.filter((d) => d.health === filter);
    // Worst first, then by site so a site's devices stay together.
    return [...filtered].sort(
      (a, b) =>
        HEALTH[a.health].rank - HEALTH[b.health].rank ||
        a.site_label.localeCompare(b.site_label) ||
        a.serial_no.localeCompare(b.serial_no),
    );
  }, [devices.data, filter]);

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {devices.isPending ? (
          <>
            <StatSkeleton />
            <StatSkeleton />
            <StatSkeleton />
          </>
        ) : (
          <>
            <Stat
              label="Reporting devices"
              value={String(devices.data?.length ?? 0)}
              unit="devices"
              footnote={`Across ${siteCount} sites`}
            />
            <Stat
              label="Need attention"
              value={String(attentionCount)}
              unit="devices"
              footnote={
                attentionCount === 0
                  ? "Every device is reporting on schedule"
                  : `On ${sitesNeedingAttention} of ${siteCount} sites`
              }
            />
            <Stat
              label="Billing meters silent"
              value={String(
                (devices.data ?? []).filter(
                  (d) => d.billing_role === "billing" && needsAttention(d.health),
                ).length,
              )}
              unit="meters"
              footnote="Rule 8 refuses to bill a period with missing intervals"
            />
          </>
        )}
      </div>

      <Card>
        <CardHeader
          title="Fleet equipment"
          subtitle="Coverage over the last 7 complete days. Worst first."
          action={
            <div className="flex flex-wrap gap-1">
              <FilterChip
                active={filter === "attention"}
                onClick={() => setFilter("attention")}
                label="Needs attention"
                count={attentionCount}
              />
              {ORDER.filter((h) => counts[h] > 0).map((h) => (
                <FilterChip
                  key={h}
                  active={filter === h}
                  onClick={() => setFilter(h)}
                  label={HEALTH[h].label}
                  count={counts[h]}
                />
              ))}
              <FilterChip
                active={filter === "all"}
                onClick={() => setFilter("all")}
                label="All"
                count={devices.data?.length ?? 0}
              />
            </div>
          }
        />

        {devices.isPending ? (
          <div className="space-y-3 p-5">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : devices.error ? (
          <ErrorState error={devices.error} />
        ) : shown.length === 0 ? (
          <EmptyState
            title="Nothing here"
            hint={
              filter === "attention"
                ? "Every device in the fleet is reporting on schedule."
                : "No device is in that state."
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-hairline text-xs font-medium tracking-wide text-ink-muted uppercase">
                  <th className="px-5 py-3 text-left">Site</th>
                  <th className="px-3 py-3 text-left">Device</th>
                  <th className="px-3 py-3 text-left">Serial</th>
                  <th className="px-3 py-3 text-right">Coverage</th>
                  <th className="px-3 py-3 text-left">Last reading</th>
                  <th className="px-5 py-3 text-left">State</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((device) => (
                  <DeviceRow key={device.device_id} device={device} />
                ))}
              </tbody>
            </table>
          </div>
        )}

        <p className="border-t border-hairline px-5 py-3 text-xs text-ink-muted">
          Health is measured from the readings actually stored for each device
          against the number its reporting interval calls for. It is not a live
          heartbeat.
        </p>
      </Card>
    </div>
  );
}

function DeviceRow({ device }: { device: SiteDevice }) {
  const health = HEALTH[device.health];
  const isBillingMeter = device.billing_role === "billing";

  return (
    <tr className="border-b border-hairline last:border-0">
      <td className="px-5 py-3">
        <span className="font-medium text-ink">{device.site_label}</span>
        <span className="block text-xs text-ink-muted">{device.district}</span>
      </td>
      <td className="px-3 py-3 text-ink-2">
        {roleOf(device)}
        {isBillingMeter && (
          <span className="ml-1.5">
            <Badge tone="neutral">bills</Badge>
          </span>
        )}
      </td>
      <td className="px-3 py-3 font-mono text-xs text-ink-muted">
        {device.serial_no}
      </td>
      <td className="tabular px-3 py-3 text-right text-ink-2">
        {device.coverage_pct == null
          ? "—"
          : `${formatKwh(device.coverage_pct, 1)}%`}
        <span className="block text-xs text-ink-muted">
          {device.intervals_received}/{device.intervals_expected}
        </span>
      </td>
      <td className="px-3 py-3 text-xs text-ink-2">
        {device.last_reading_at
          ? new Date(device.last_reading_at).toLocaleString(undefined, {
              dateStyle: "medium",
              timeStyle: "short",
            })
          : "never"}
      </td>
      <td className="px-5 py-3">
        <Badge tone={health.tone}>{health.label}</Badge>
      </td>
    </tr>
  );
}

function FilterChip({
  active,
  onClick,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
        active
          ? "bg-portal-supplier text-white"
          : "text-ink-2 hover:bg-hairline/60"
      }`}
    >
      {label}
      <span className="tabular ml-1.5 opacity-70">{count}</span>
    </button>
  );
}
