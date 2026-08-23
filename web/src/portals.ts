/**
 * The four portals and what each one is allowed to look at.
 *
 * GridSync has one database and four audiences with very different views of
 * it: a household sees its own bill, a field worker sees a job queue, a
 * regulator sees district aggregates, and a supplier sees the fleet. The
 * switcher in the layout is currently just navigation -- there is no auth, so
 * nothing here is enforced. When auth lands, this table is the natural place
 * to hang the role each portal requires, and the API is where it gets checked.
 */

export type PortalId = "customer" | "worker" | "government" | "supplier";

export interface PortalRoute {
  path: string;
  label: string;
}

export interface Portal {
  id: PortalId;
  label: string;
  /** One-line description of the audience, shown under the portal heading. */
  blurb: string;
  /** Tailwind accent class, matched to the --color-portal-* theme tokens. */
  accent: string;
  /** Base path; the portal's index route. */
  base: string;
  routes: PortalRoute[];
}

export const PORTALS: Portal[] = [
  {
    id: "customer",
    label: "Customer",
    blurb: "A household's own consumption, generation, bills and credit balance.",
    accent: "bg-portal-customer",
    base: "/customer",
    routes: [
      { path: "", label: "Overview" },
      { path: "meters", label: "Meters" },
      { path: "bills", label: "Bills" },
      { path: "devices", label: "Equipment" },
      { path: "issues", label: "Report an issue" },
    ],
  },
  {
    id: "worker",
    label: "Worker",
    blurb: "Field job queue: assigned work orders and the issues behind them.",
    accent: "bg-portal-worker",
    base: "/worker",
    routes: [
      { path: "", label: "Work orders" },
      { path: "issues", label: "Issue queue" },
    ],
  },
  {
    id: "government",
    label: "Government",
    blurb: "Regulator view: net-metering approvals and district-level rollups.",
    accent: "bg-portal-government",
    base: "/government",
    routes: [
      { path: "", label: "By area" },
      { path: "agreements", label: "Pending agreements" },
    ],
  },
  {
    id: "supplier",
    label: "Supplier",
    blurb: "Utility view across the fleet: sites, telemetry health, billing runs.",
    accent: "bg-portal-supplier",
    base: "/supplier",
    routes: [
      { path: "", label: "Sites" },
      { path: "equipment", label: "Equipment" },
    ],
  },
];

export const portalById = (id: PortalId): Portal =>
  PORTALS.find((p) => p.id === id)!;
