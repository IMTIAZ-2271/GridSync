import { NavLink, Outlet, useLocation } from "react-router-dom";
import { PORTALS } from "../portals";

/**
 * App shell: portal switcher across the top, the active portal's sub-nav
 * beneath it, and the routed page below.
 *
 * The active portal is derived from the URL rather than held in state, so a
 * deep link lands in the right portal and the back button behaves.
 */
export default function Layout() {
  const { pathname } = useLocation();
  const active =
    PORTALS.find((p) => pathname.startsWith(p.base)) ?? PORTALS[0];

  return (
    <div className="flex min-h-full flex-col bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
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
                      : "text-slate-600 hover:bg-slate-100",
                  ].join(" ")
                }
              >
                {portal.label}
              </NavLink>
            ))}
          </nav>

          {/* No auth yet, so say so rather than implying a signed-in user. */}
          <span className="ml-auto text-xs text-slate-400">
            no auth &middot; demo data
          </span>
        </div>
      </header>

      <div className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-6xl px-6 pb-3">
          <p className="mb-2 text-sm text-slate-500">{active.blurb}</p>
          <nav className="flex gap-4" aria-label={`${active.label} sections`}>
            {active.routes.map((route) => (
              <NavLink
                key={route.path}
                to={route.path ? `${active.base}/${route.path}` : active.base}
                end={route.path === ""}
                className={({ isActive }) =>
                  [
                    "border-b-2 pb-1 text-sm transition-colors",
                    isActive
                      ? "border-slate-900 font-medium text-slate-900"
                      : "border-transparent text-slate-500 hover:text-slate-800",
                  ].join(" ")
                }
              >
                {route.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </div>

      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
