import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api, queryKeys, type Site, type SiteDevice } from "../lib/api";
import { HEALTH, needsAttention, worstHealth } from "../lib/devices";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * The fleet, one row per site.
 *
 * Two requests, not one per site: `/api/sites` for the roster and
 * `/api/devices` for every device's health in one go, joined here on site_id.
 * The health column is the worst state on the site, which is the only summary
 * that is safe to act on -- a site with one healthy inverter and one silent
 * billing meter is a silent site, and averaging that away would hide the
 * device that costs money.
 */
export default function SupplierSites() {
  const [attentionOnly, setAttentionOnly] = useState(false);

  const sites = useQuery({
    queryKey: queryKeys.sites(),
    queryFn: api.listSites,
  });

  const devices = useQuery({
    queryKey: queryKeys.fleetDevices(),
    queryFn: api.fleetDevices,
  });

  const bySite = useMemo(() => {
    const map = new Map<string, SiteDevice[]>();
    for (const d of devices.data ?? []) {
      map.set(d.site_id, [...(map.get(d.site_id) ?? []), d]);
    }
    return map;
  }, [devices.data]);

  const rows = useMemo(() => {
    const all = sites.data ?? [];
    if (!attentionOnly) return all;
    return all.filter((s) => {
      const worst = worstHealth(bySite.get(s.site_id) ?? []);
      return worst != null && needsAttention(worst);
    });
  }, [sites.data, bySite, attentionOnly]);

  const solarCount = (sites.data ?? []).filter((s) => s.has_solar).length;

  return (
    <Card>
      <CardHeader
        title="Sites"
        subtitle={
          sites.data
            ? `${sites.data.length} sites · ${solarCount} with solar`
            : undefined
        }
        action={
          <label className="flex items-center gap-2 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={attentionOnly}
              onChange={(e) => setAttentionOnly(e.target.checked)}
              className="size-3.5 accent-portal-supplier"
            />
            Needs attention only
          </label>
        }
      />

      {sites.isPending ? (
        <div className="space-y-3 p-5">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : sites.error ? (
        <ErrorState error={sites.error} />
      ) : rows.length === 0 ? (
        <EmptyState
          title={attentionOnly ? "Every site is reporting" : "No sites"}
          hint={
            attentionOnly
              ? "No site has a device that has stopped reporting."
              : "No site has been registered yet."
          }
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-hairline text-xs font-medium tracking-wide text-ink-muted uppercase">
                <th className="px-5 py-3 text-left">Site</th>
                <th className="px-3 py-3 text-left">District</th>
                <th className="px-3 py-3 text-left">Owner</th>
                <th className="px-3 py-3 text-right">Devices</th>
                <th className="px-3 py-3 text-left">Solar</th>
                <th className="px-5 py-3 text-left">Telemetry</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((site: Site) => (
                <SiteRow
                  key={site.site_id}
                  site={site}
                  devices={bySite.get(site.site_id) ?? []}
                  healthPending={devices.isPending}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function SiteRow({
  site,
  devices,
  healthPending,
}: {
  site: Site;
  devices: SiteDevice[];
  healthPending: boolean;
}) {
  const worst = worstHealth(devices);

  return (
    <tr className="border-b border-hairline last:border-0">
      <td className="px-5 py-3 font-medium text-ink">{site.label}</td>
      <td className="px-3 py-3 text-ink-2">{site.district}</td>
      <td className="px-3 py-3 text-ink-2">{site.account_name}</td>
      <td className="tabular px-3 py-3 text-right text-ink-2">
        {healthPending ? "—" : devices.length}
      </td>
      <td className="px-3 py-3">
        {site.has_solar ? (
          <Badge tone="good">yes</Badge>
        ) : (
          <span className="text-xs text-ink-muted">—</span>
        )}
      </td>
      <td className="px-5 py-3">
        {healthPending ? (
          <Skeleton className="h-4 w-20" />
        ) : worst == null ? (
          // A site on the roster with nothing reporting is not "healthy" and
          // not a fault either -- it has no billing meter yet.
          <span className="text-xs text-ink-muted">no devices</span>
        ) : needsAttention(worst) ? (
          <Link
            to="/supplier/equipment"
            className="inline-flex items-center gap-1.5"
            title={HEALTH[worst].hint}
          >
            <Badge tone={HEALTH[worst].tone}>{HEALTH[worst].label}</Badge>
          </Link>
        ) : (
          <Badge tone={HEALTH[worst].tone}>{HEALTH[worst].label}</Badge>
        )}
      </td>
    </tr>
  );
}
