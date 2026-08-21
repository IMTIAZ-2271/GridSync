import { Navigate, Outlet, useLocation } from "react-router-dom";

import { PORTALS, type PortalId } from "../portals";
import { HOME_FOR_ROLE, useAuth } from "./AuthContext";
import type { Role } from "../lib/api";

/** Which roles may open which portal. Mirrors the API's require_role rules. */
const PORTAL_ROLES: Record<PortalId, Role[]> = {
  customer: ["consumer", "admin"],
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

  const portal = PORTALS.find((p) => location.pathname.startsWith(p.base));
  if (portal && !PORTAL_ROLES[portal.id].includes(account.role)) {
    return <Navigate to={HOME_FOR_ROLE[account.role]} replace />;
  }

  return <Outlet />;
}
