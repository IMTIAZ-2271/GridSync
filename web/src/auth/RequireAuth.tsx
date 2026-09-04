import { Navigate, Outlet, useLocation } from "react-router-dom";

import { PORTALS, type PortalId } from "../portals";
import PendingApproval from "../routes/PendingApproval";
import { HOME_FOR_ROLE, useAuth } from "./AuthContext";
import type { Account, Role } from "../lib/api";

/**
 * Is this account's registration still with an official?
 *
 * Only two roles carry an approval at all, and the answer comes from the
 * profile row the server resolved at sign-in -- never from the role or from
 * anything the client remembers. An account with no context (a seeded row
 * mid-claim) reads as decided rather than as blocked: the API is the authority
 * either way, and refusing to render a portal the server would have served is
 * a lockout the client has no business inventing.
 */
export function awaitsApproval(account: Account): boolean {
  const status = (account.worker ?? account.supplier)?.approval_status;
  return status === "pending" || status === "rejected";
}

/** Which roles may open which portal. Mirrors the API's require_role rules. */
const PORTAL_ROLES: Record<PortalId, Role[]> = {
  consumer: ["consumer", "admin"],
  worker: ["worker", "admin"],
  government: ["government", "admin"],
  supplier: ["supplier", "admin"],
};

export function rolesForPortal(id: PortalId): Role[] {
  return PORTAL_ROLES[id];
}

export function portalsForRole(role: Role) {
  return PORTALS.filter((p) => PORTAL_ROLES[p.id].includes(role));
}

/**
 * Gate for every portal route.
 *
 * This is convenience, not security -- it decides what to render, and the API
 * decides what data anyone actually gets. A user who edits their way past this
 * guard reaches a portal whose every request comes back 403.
 *
 * An undecided worker or supplier registration is the one case that replaces
 * the whole shell rather than a page inside it. `get_current_account` refuses
 * everything outside `/auth` and `/notifications` for those accounts, so the
 * portal would render as nav items that all lead to 403s and counters that
 * never load. One screen saying where the application stands is the honest
 * version of that -- see routes/PendingApproval.tsx.
 */
export default function RequireAuth() {
  const { account, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="flex min-h-full items-center justify-center">
        <p className="text-sm text-ink-muted">Checking your session…</p>
      </div>
    );
  }

  if (!account) {
    // Remember where they were headed so signing in can return them there
    // instead of dumping everyone on their role's home page.
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  if (awaitsApproval(account)) return <PendingApproval />;

  const portal = PORTALS.find((p) => location.pathname.startsWith(p.base));
  if (portal && !PORTAL_ROLES[portal.id].includes(account.role)) {
    return <Navigate to={HOME_FOR_ROLE[account.role]} replace />;
  }

  return <Outlet />;
}
