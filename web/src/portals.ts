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

import { VIEWS, type ViewKey } from "./lib/unread";

export type PortalId = "consumer" | "worker" | "government" | "supplier";

export interface PortalRoute {
  path: string;
  label: string;
  /**
   * The list this page shows, if it carries an unread indicator. Must be a
   * key from VIEWS in lib/unread.ts; a page without one simply never shows a
   * dot. Site-scoped lists (bills, meters) are deliberately left off -- see
   * lib/unreadSources.ts.
   */
  viewKey?: ViewKey;
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
    id: "consumer",
    label: "Consumer",
    blurb: "A household's own consumption, generation, bills and credit balance.",
    accent: "bg-portal-consumer",
    base: "/consumer",
    routes: [
      { path: "", label: "Overview" },
      { path: "meters", label: "Meters" },
      { path: "bills", label: "Bills" },
      { path: "devices", label: "Equipment" },
      { path: "issues", label: "Report an issue", viewKey: VIEWS.consumerIssues },
      { path: "applications", label: "Applications", viewKey: VIEWS.consumerApplications },
      { path: "visits", label: "Visits", viewKey: VIEWS.consumerVisits },
      { path: "settings", label: "Settings" },
    ],
  },
  {
    id: "worker",
    label: "Worker",
    blurb: "Field job queue: assigned work orders and the issues behind them.",
    accent: "bg-portal-worker",
    base: "/worker",
    routes: [
      { path: "", label: "Work orders", viewKey: VIEWS.workerOrders },
      { path: "issues", label: "Issue queue", viewKey: VIEWS.workerIssues },
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
      { path: "agreements", label: "Pending agreements", viewKey: VIEWS.governmentAgreements },
      { path: "net-metering", label: "Net metering" },
      { path: "workers", label: "Worker approvals", viewKey: VIEWS.governmentWorkers },
      {
        path: "supplier-registrations",
        label: "Supplier approvals",
        viewKey: VIEWS.governmentSupplierRegistrations,
      },
      { path: "meter-applications", label: "Meter applications", viewKey: VIEWS.governmentMeterApplications },
    ],
  },
  {
    id: "supplier",
    label: "Supplier",
    blurb: "Utility view across the fleet: dispatch, sites and telemetry health.",
    accent: "bg-portal-supplier",
    base: "/supplier",
    routes: [
      { path: "", label: "Sites" },
      { path: "dispatch", label: "Dispatch", viewKey: VIEWS.supplierDispatch },
      { path: "applications", label: "Applications", viewKey: VIEWS.supplierApplications },
      { path: "issues", label: "Complaints", viewKey: VIEWS.supplierIssues },
      { path: "equipment", label: "Equipment" },
    ],
  },
];

export const portalById = (id: PortalId): Portal =>
  PORTALS.find((p) => p.id === id)!;
