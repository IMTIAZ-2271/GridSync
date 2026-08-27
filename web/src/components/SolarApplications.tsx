import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  formatKwh,
  queryKeys,
  toNumber,
  type SolarApplication,
} from "../lib/api";
import {
  APPLICATION_STATUS_LABEL,
  APPLICATION_STATUS_TONE,
  isOpenApplication,
} from "../lib/applications";
import { useSelectedSite } from "./SitePicker";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "./ui";

/**
 * Consumer requirement 7's install half: pick a nearby installer and apply.
 *
 * One of the three panels on /customer/applications. The requirement bundles
 * two things that happen months apart and this is deliberately only the first:
 * applying to an installer is choosing who climbs on the roof. **Net metering
 * is a different application** -- the regulator agreeing that what the panels
 * produce may be sold back -- and it only makes sense once the panels exist,
 * which is why it is its own tab rather than another field on this form.
 *
 * Installers are filtered to the site's district, because the requirement asks
 * for a supplier "in the consumer's nearby region" -- and sorted by rating,
 * with the unrated shown as unrated rather than as zero stars.
 */
export default function SolarApplications() {
  const queryClient = useQueryClient();
  const { siteId, site } = useSelectedSite();
  const [showForm, setShowForm] = useState(false);

  const applications = useQuery({
    queryKey: queryKeys.solarApplications(),
    queryFn: () => api.solarApplications(),
  });

  const withdraw = useMutation({
    mutationFn: (id: string) => api.decideSolarApplication(id, "withdrawn"),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: queryKeys.solarApplications(),
      }),
  });

  const mine = applications.data ?? [];
  const hasOpen = mine.some((a) => isOpenApplication(a.status));

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader
          title="Solar installation"
          subtitle="Apply to an installer who works in your area"
          action={
            !showForm && !hasOpen ? (
              <button
                type="button"
                onClick={() => setShowForm(true)}
                className="rounded-lg bg-ink px-3.5 py-1.5 text-sm font-medium text-surface"
              >
                Apply
              </button>
            ) : undefined
          }
        />

        {showForm && siteId ? (
          <ApplyForm
            siteId={siteId}
            district={site?.district}
            onDone={() => setShowForm(false)}
          />
        ) : (
          <div className="p-5 text-sm text-ink-2">
            {hasOpen ? (
              <p>
                You have an application waiting. Withdraw it below if you want
                to change what you asked for or apply to someone else.
              </p>
            ) : (
              <p>
                An installer surveys your roof and fits the panels. Once they
                are in, applying for <b>net metering</b> is the next tab —
                that is your utility agreeing to credit what you send back to
                the grid, and it only makes sense once there is something to
                send.
              </p>
            )}
          </div>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Your applications"
          subtitle="Including ones that were withdrawn or turned down"
        />
        {applications.isPending ? (
          <div className="space-y-3 p-5">
            <Skeleton className="h-16 w-full" />
          </div>
        ) : applications.error ? (
          <ErrorState error={applications.error} />
        ) : mine.length === 0 ? (
          <EmptyState
            title="You have not applied yet"
            hint="Applications stay listed here whatever happens to them, so you always have the record of having asked."
          />
        ) : (
          <ul className="divide-y divide-hairline">
            {mine.map((app) => (
              <ApplicationRow
                key={app.application_id}
                app={app}
                busy={
                  withdraw.isPending &&
                  withdraw.variables === app.application_id
                }
                onWithdraw={() => withdraw.mutate(app.application_id)}
              />
            ))}
          </ul>
        )}
        {withdraw.error && (
          <p className="border-t border-hairline px-5 py-3 text-sm text-status-critical">
            {withdraw.error instanceof ApiError
              ? String(withdraw.error.detail)
              : withdraw.error.message}
          </p>
        )}
      </Card>
    </div>
  );
}

function ApplyForm({
  siteId,
  district,
  onDone,
}: {
  siteId: string;
  district?: string;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [point, setPoint] = useState("");
  const [supplier, setSupplier] = useState("");
  const [capacity, setCapacity] = useState("3");
  const [panels, setPanels] = useState("");
  const [notes, setNotes] = useState("");

  const points = useQuery({
    queryKey: queryKeys.sitePoints(siteId),
    queryFn: () => api.listBillingPoints(siteId),
  });
  // Nearby, as the requirement puts it. Filtering on the server rather than
  // fetching every installer and hiding some: the API refuses an application
  // naming a firm that does not serve the district, so offering one would be a
  // button that cannot work.
  const suppliers = useQuery({
    queryKey: ["suppliers", district ?? "all"],
    queryFn: () => api.listSuppliers(district),
    enabled: !!district,
  });

  const apply = useMutation({
    mutationFn: () =>
      api.createSolarApplication({
        billing_point_id: point,
        supplier_id: supplier,
        requested_capacity_kw: capacity.trim(),
        panel_count: panels.trim() ? Number(panels) : null,
        notes: notes.trim() || null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.solarApplications(),
      });
      onDone();
    },
  });

  const ranked = [...(suppliers.data ?? [])].sort((a, b) => {
    const ar = a.rating_avg ? toNumber(a.rating_avg) : -1;
    const br = b.rating_avg ? toNumber(b.rating_avg) : -1;
    return br - ar || a.name.localeCompare(b.name);
  });

  return (
    <form
      className="space-y-4 p-5"
      onSubmit={(e) => {
        e.preventDefault();
        apply.mutate();
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block text-sm">
          <span className="font-medium text-ink-2">Connection</span>
          <select
            required
            value={point}
            onChange={(e) => setPoint(e.target.value)}
            className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
          >
            <option value="">Choose a connection…</option>
            {(points.data ?? []).map((p) => (
              <option key={p.point_id} value={p.point_id}>
                {p.label}
                {p.reference ? ` · ${p.reference}` : ""}
              </option>
            ))}
          </select>
          <span className="mt-1 block text-xs text-ink-muted">
            Panels are fitted to one connection. Each is billed on its own.
          </span>
        </label>

        <label className="block text-sm">
          <span className="font-medium text-ink-2">Installer</span>
          <select
            required
            value={supplier}
            onChange={(e) => setSupplier(e.target.value)}
            className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
          >
            <option value="">
              {suppliers.isPending
                ? "Loading…"
                : ranked.length === 0
                  ? `No installer works in ${district ?? "your area"}`
                  : "Choose an installer…"}
            </option>
            {ranked.map((s) => (
              <option key={s.supplier_id} value={s.supplier_id}>
                {s.name} —{" "}
                {s.rating_avg
                  ? `${s.rating_avg}★ from ${s.rating_count}`
                  : "not yet rated"}
              </option>
            ))}
          </select>
          <span className="mt-1 block text-xs text-ink-muted">
            Installers who work in {district ?? "your district"}, best-rated
            first.
          </span>
        </label>

        <label className="block text-sm">
          <span className="font-medium text-ink-2">Capacity wanted (kW)</span>
          <input
            type="number"
            min="0.5"
            max="1000"
            step="0.5"
            required
            value={capacity}
            onChange={(e) => setCapacity(e.target.value)}
            className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
          />
        </label>

        <label className="block text-sm">
          <span className="font-medium text-ink-2">
            Panels, if you know (optional)
          </span>
          <input
            type="number"
            min="1"
            step="1"
            value={panels}
            onChange={(e) => setPanels(e.target.value)}
            className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
          />
        </label>
      </div>

      <label className="block text-sm">
        <span className="font-medium text-ink-2">
          Anything the installer should know (optional)
        </span>
        <input
          type="text"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Flat roof, access from the back lane"
          className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
        />
      </label>

      {apply.error && (
        <p className="text-sm text-status-critical">
          {apply.error instanceof ApiError
            ? String(apply.error.detail)
            : apply.error.message}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={apply.isPending || !point || !supplier}
          className="rounded-lg bg-ink px-4 py-2 text-sm font-medium text-surface disabled:opacity-50"
        >
          {apply.isPending ? "Sending…" : "Send application"}
        </button>
        <button
          type="button"
          onClick={onDone}
          className="text-sm text-ink-muted underline"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function ApplicationRow({
  app,
  busy,
  onWithdraw,
}: {
  app: SolarApplication;
  busy: boolean;
  onWithdraw: () => void;
}) {
  const open = isOpenApplication(app.status);

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-ink">
            {app.supplier_name} · {formatKwh(app.requested_capacity_kw, 1)} kW
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {app.site_label} · {app.point_label} · applied{" "}
            {new Date(app.submitted_at).toLocaleDateString(undefined, {
              dateStyle: "medium",
            })}
            {app.panel_count && ` · ${app.panel_count} panels`}
          </p>
          {app.notes && <p className="mt-1 text-sm text-ink-2">{app.notes}</p>}
          {app.decision_notes && (
            <p className="mt-1 text-sm text-ink-2">
              {app.supplier_name} said: {app.decision_notes}
            </p>
          )}
          {app.status === "completed" && (
            <p className="mt-1 text-sm text-ink-2">
              Next: register the array on the <b>Meters</b> page, then apply
              for <b>net metering</b> on the next tab so your exports start
              earning credit.
            </p>
          )}
          {app.status === "accepted" && app.supplier_phone && (
            <p className="mt-1 text-xs text-ink-muted">
              Contact: {app.supplier_phone}
              {app.supplier_email && ` · ${app.supplier_email}`}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <Badge tone={APPLICATION_STATUS_TONE[app.status]}>
            {APPLICATION_STATUS_LABEL[app.status]}
          </Badge>
          {open && (
            <button
              type="button"
              disabled={busy}
              onClick={onWithdraw}
              className="text-sm text-ink-muted underline disabled:opacity-50"
            >
              {busy ? "Withdrawing…" : "Withdraw"}
            </button>
          )}
        </div>
      </div>
    </li>
  );
}
