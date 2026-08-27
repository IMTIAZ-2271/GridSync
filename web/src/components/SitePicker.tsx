import { useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, queryKeys, type Site } from "../lib/api";

/**
 * The consumer's current site.
 *
 * The site is no longer named in the URL. /api/sites now returns exactly the
 * sites the token's account owns, so the selection is derived from who is
 * signed in -- a consumer cannot address someone else's site by editing a
 * query string, and the API would answer 404 if they tried.
 *
 * The schema still allows one account to own several sites
 * (ACCOUNT ||--o{ SITE), so this keeps a selection; it just defaults to the
 * first and only offers a control when there is a genuine choice to make.
 */
// A module-level store rather than context: the picker in the header and the
// three pages under it all need the same value, and they are not in a shared
// subtree worth adding a provider for.
let selectedSiteId: string | null = null;
const listeners = new Set<() => void>();

function selectSite(id: string) {
  selectedSiteId = id;
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function snapshot() {
  return selectedSiteId;
}

/** Cleared on sign-out so the next account does not inherit a selection. */
export function resetSelectedSite() {
  selectSite("");
}

export function useSelectedSite() {
  const selected = useSyncExternalStore(subscribe, snapshot, snapshot);
  const sitesQuery = useQuery({
    queryKey: queryKeys.sites(),
    queryFn: api.listSites,
  });

  const sites = sitesQuery.data;
  const valid = sites?.some((s) => s.site_id === selected) ? selected : null;
  const siteId = valid ?? sites?.[0]?.site_id ?? null;

  return {
    siteId,
    setSiteId: selectSite,
    site: sites?.find((s) => s.site_id === siteId) ?? null,
    sites,
    isPending: sitesQuery.isPending,
    error: sitesQuery.error,
  };
}

export default function SitePicker() {
  const { siteId, setSiteId, sites, isPending } = useSelectedSite();

  if (isPending) return <div className="skeleton h-8 w-56" aria-hidden />;

  // One site is the ordinary case. A dropdown with a single option is a
  // control that cannot do anything, so show the site as a label instead.
  if (!sites?.length) return null;
  if (sites.length === 1) {
    return (
      <span className="text-xs text-ink-muted">
        {sites[0].label} &middot; {sites[0].district}
        {sites[0].has_solar && " · solar"}
      </span>
    );
  }

  return (
    <label className="flex items-center gap-2">
      <span className="text-xs font-medium text-ink-2">Site</span>
      <select
        value={siteId ?? ""}
        onChange={(e) => setSiteId(e.target.value)}
        className="rounded-md border border-hairline bg-surface px-2.5 py-1.5 text-sm text-ink outline-none focus:border-series-import focus:ring-2 focus:ring-series-import/25"
      >
        {sites.map((site: Site) => (
          <option key={site.site_id} value={site.site_id}>
            {site.label} — {site.district}
            {site.has_solar ? " ☀" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}
