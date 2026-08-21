import { NavLink, Outlet, useLocation, useSearchParams } from "react-router-dom";

import { PORTALS } from "../portals";
import SitePicker from "./SitePicker";

/**
 * App shell: portal switcher, the active portal's sub-nav, then the page.
 *
 * The active portal comes from the URL rather than state, so a deep link lands
 * in the right portal and the back button behaves.
 */
export default function Layout() {
  const { pathname } = useLocation();
  const [params] = useSearchParams();
  const active = PORTALS.find((p) => pathname.startsWith(p.base)) ?? PORTALS[0];

  // Customer pages are all scoped to one site, so the sub-nav has to carry the
  // selection forward -- a bare `to="/customer/bills"` would drop it and
  // silently reset the reader to the default site on every tab change.
  const search = active.id === "customer" ? `?${params.toString()}` : "";

  return (
    <div className="flex min-h-full flex-col bg-plane text-ink">
      <header className="border-b border-hairline bg-surface">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
          <span className="text-lg font-semibold tracking-tight">GridSync</span>

          <nav className="flex gap-1" aria-label="Portal">
            {PORTALS.map((portal) => (
              <NavLink
                key={portal.id}
                to={portal.base}
                className={({ isActive }) =>
                  [
                    "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                    isActive
                      ? `${portal.accent} text-white`
                      : "text-ink-2 hover:bg-hairline/60",
                  ].join(" ")
                }
              >
                {portal.label}
              </NavLink>
            ))}
          </nav>

          <span className="ml-auto text-xs text-ink-muted">
            no auth &middot; demo data
          </span>
        </div>
      </header>

      <div className="border-b border-hairline bg-surface">
        <div className="mx-auto max-w-6xl px-6 pb-3">
          <p className="mb-3 text-sm text-ink-2">{active.blurb}</p>

          {/* Filters sit in one row above the content they scope. */}
          <div className="flex flex-wrap items-center justify-between gap-4">
            <nav className="flex gap-4" aria-label={`${active.label} sections`}>
              {active.routes.map((route) => (
                <NavLink
                  key={route.path}
                  to={`${route.path ? `${active.base}/${route.path}` : active.base}${search}`}
                  end={route.path === ""}
                  className={({ isActive }) =>
                    [
                      "border-b-2 pb-1 text-sm transition-colors",
                      isActive
                        ? "border-ink font-medium text-ink"
                        : "border-transparent text-ink-muted hover:text-ink-2",
                    ].join(" ")
                  }
                >
                  {route.label}
                </NavLink>
              ))}
            </nav>

            {active.id === "customer" && <SitePicker />}
          </div>
        </div>
      </div>

      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
