import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, type Site } from "../lib/api";

/**
 * The one page wired end to end in this scaffold.
 *
 * It exists to prove the whole path works -- api.ts fetcher, TanStack Query
 * cache, Vite's /api proxy to uvicorn -- so the remaining pages are a matter of
 * filling in, not of debugging plumbing.
 */
export default function SupplierSites() {
  const { data, isPending, error } = useQuery({
    queryKey: queryKeys.sites(),
    queryFn: api.listSites,
  });

  if (isPending) return <p className="text-sm text-slate-500">Loading sites…</p>;
  if (error)
    return (
      <p className="text-sm text-red-600">
        Could not load sites: {error.message}
      </p>
    );

  return (
    <div>
      <h1 className="text-xl font-semibold">Sites</h1>
      <p className="mt-1 text-sm text-slate-500">
        {data.length} sites &middot; {data.filter((s) => s.has_solar).length} with
        solar
      </p>

      <div className="mt-6 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3 font-medium">Site</th>
              <th className="px-4 py-3 font-medium">District</th>
              <th className="px-4 py-3 font-medium">Owner</th>
              <th className="px-4 py-3 font-medium">Solar</th>
            </tr>
          </thead>
          <tbody>
            {data.map((site: Site) => (
              <tr key={site.site_id} className="border-b border-slate-100 last:border-0">
                <td className="px-4 py-3 font-medium">{site.label}</td>
                <td className="px-4 py-3 text-slate-600">{site.district}</td>
                <td className="px-4 py-3 text-slate-600">{site.account_name}</td>
                <td className="px-4 py-3">
                  {site.has_solar ? (
                    <span className="rounded bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
                      yes
                    </span>
                  ) : (
                    <span className="text-xs text-slate-400">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
