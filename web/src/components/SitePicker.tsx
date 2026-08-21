import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api, queryKeys, type Site } from "../lib/api";

export const SITE_PARAM = "site";

/**
 * The selected site, held in the URL.
 *
 * The query string is the store rather than React state or localStorage, which
 * buys three things at once: the selection survives a refresh, it persists
 * across the three customer routes because each link carries the search string
 * forward, and a link to a particular site is shareable.
 *
 * When no site is named, the first solar site is selected and written back
 * with `replace`, so landing on /customer does not leave a blank URL in
 * history that the back button can return to.
 */
export function useSelectedSite() {
  const [params, setParams] = useSearchParams();
  const sitesQuery = useQuery({
    queryKey: queryKeys.sites(),
    queryFn: api.listSites,
  });

  const requested = params.get(SITE_PARAM);
  const sites = sitesQuery.data;

  // Only sites that exist may be selected -- a stale or hand-edited id falls
  // back rather than leaving every panel below in a 404 state.
  const valid = sites?.some((s) => s.site_id === requested) ? requested : null;
  const fallback = sites?.find((s) => s.has_solar)?.site_id ?? sites?.[0]?.site_id;
  const siteId = valid ?? fallback ?? null;

  useEffect(() => {
    if (!sites || !siteId || requested === siteId) return;
    const next = new URLSearchParams(params);
    next.set(SITE_PARAM, siteId);
    setParams(next, { replace: true });
  }, [sites, siteId, requested, params, setParams]);

  const setSiteId = (id: string) => {
    const next = new URLSearchParams(params);
    next.set(SITE_PARAM, id);
    setParams(next);
  };

  return {
    siteId,
    setSiteId,
    site: sites?.find((s) => s.site_id === siteId) ?? null,
    sites,
    isPending: sitesQuery.isPending,
    error: sitesQuery.error,
  };
}

export default function SitePicker() {
  const { siteId, setSiteId, sites, isPending } = useSelectedSite();

  if (isPending) {
    return <div className="skeleton h-8 w-56" aria-hidden />;
  }
  if (!sites?.length) return null;

  return (
    <label className="flex items-center gap-2">
      <span className="text-xs font-medium text-ink-2">Site</span>
      <select
        value={siteId ?? ""}
        onChange={(e) => setSiteId(e.target.value)}
        className="rounded-md border border-hairline bg-surface px-2.5 py-1.5 text-sm text-ink shadow-xs outline-none focus:border-series-import focus:ring-2 focus:ring-series-import/25"
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
