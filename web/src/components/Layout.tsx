import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { PORTALS } from "../portals";
import { ROLE_LABEL, useAuth } from "../auth/AuthContext";
import { portalsForRole } from "../auth/RequireAuth";
import Logo from "./Logo";
import NotificationBell from "./NotificationBell";
import SitePicker from "./SitePicker";

/**
 * App shell: portal switcher, the active portal's sub-nav, then the page.
 *
 * The switcher lists only the portals this role may open. Showing all four and
 * bouncing the user off three of them would advertise doors that are locked --
 * the API refuses them anyway, so there is nothing to gain by displaying them.
 */
export default function Layout() {
  const { pathname } = useLocation();
  const { account, signOut } = useAuth();
  const navigate = useNavigate();

  const active = PORTALS.find((p) => pathname.startsWith(p.base)) ?? PORTALS[0];
  const available = account ? portalsForRole(account.role) : [];

  return (
    <div className="flex min-h-full flex-col bg-plane text-ink">
      <header className="border-b border-hairline bg-surface">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-3 px-6 py-3">
          <span className="flex items-center gap-2.5">
            <Logo className="h-7 w-7" />
            <span className="text-lg font-semibold tracking-tight">GridSync</span>
          </span>

          {available.length > 1 && (
            <nav className="flex gap-1" aria-label="Portal">
              {available.map((portal) => (
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
          )}

          {account && (
            <div className="ml-auto flex items-center gap-4">
              {/* Every role has an inbox: a household hears about its work
                  orders and its net-metering decision, a worker will hear
                  about their approval, a supplier about expired offers. */}
              <NotificationBell />
              <div className="text-right leading-tight">
                <p className="text-sm font-medium text-ink">{account.full_name}</p>
                <p className="text-xs text-ink-muted">{ROLE_LABEL[account.role]}</p>
              </div>
              <button
                type="button"
                onClick={() => {
                  signOut();
                  navigate("/login", { replace: true });
                }}
                className="rounded-md border border-hairline px-3 py-1.5 text-sm text-ink-2 transition-colors hover:bg-plane"
              >
                Sign out
              </button>
            </div>
          )}
        </div>
      </header>

      <div className="border-b border-hairline bg-surface">
        <div className="mx-auto max-w-6xl px-6 pb-3">
          <p className="mb-3 text-sm text-ink-2">{active.blurb}</p>

          <div className="flex flex-wrap items-center justify-between gap-4">
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
                        ? "border-ink font-medium text-ink"
                        : "border-transparent text-ink-muted hover:text-ink-2",
                    ].join(" ")
                  }
                >
                  {route.label}
                </NavLink>
              ))}
            </nav>

            {/* Only rendered when the consumer owns more than one site; see
                SitePicker. The site itself comes from the token now, not the
                query string. */}
            {active.id === "consumer" && <SitePicker />}
          </div>
        </div>
      </div>

      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
