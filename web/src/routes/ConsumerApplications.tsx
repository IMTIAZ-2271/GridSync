import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  formatKwh,
  queryKeys,
  type ApplicationVisit,
  type Inverter,
  type MeterApplication,
  type NetMeteringApplication,
} from "../lib/api";
import {
  NET_METERING_REQUIREMENTS,
  meterApplicationStage,
  netMeteringStage,
  type Stage,
} from "../lib/fulfilment";
import { useSelectedSite } from "../components/SitePicker";
import SolarApplications from "../components/SolarApplications";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * Everything a household can ask somebody else for, in one place.
 *
 * Replaces the old "Solar" tab. Three things a consumer applies for, and they
 * are genuinely three: a **solar installation** is a request to a private
 * installer, **net metering** is a request to the regulator about a connection
 * that already has panels, and a **new meter** is a request to the
 * distribution company for hardware. Different counterparties, different
 * preconditions, months apart in real life -- so they are tabs rather than one
 * queue, and each says what has to be true before it makes sense.
 *
 * The order is the order they actually happen in.
 */

type TabId = "solar" | "net-metering" | "meter";

const TABS: { id: TabId; label: string; blurb: string }[] = [
  {
    id: "solar",
    label: "Solar installation",
    blurb: "Ask an installer in your area to fit panels",
  },
  {
    id: "net-metering",
    label: "Net metering",
    blurb: "Ask the regulator to credit what your panels export",
  },
  {
    id: "meter",
    label: "New meter",
    blurb: "Ask your distribution company to issue you a meter",
  },
];

export default function ConsumerApplications() {
  const [tab, setTab] = useState<TabId>("solar");
  const active = TABS.find((t) => t.id === tab)!;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <nav
          aria-label="Application type"
          className="flex flex-wrap gap-1 rounded-lg bg-plane p-1"
        >
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              aria-current={tab === t.id}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                tab === t.id
                  ? "bg-surface text-ink shadow-sm"
                  : "text-ink-2 hover:text-ink"
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <p className="mt-2 text-xs text-ink-2">{active.blurb}.</p>
      </div>

      {tab === "solar" && <SolarApplications />}
      {tab === "net-metering" && <NetMeteringPanel />}
      {tab === "meter" && <MeterPanel />}
    </div>
  );
}

/**
 * The stage badge and its one-line explanation.
 *
 * The label is never carried by colour alone -- the badge always has words in
 * it, which is the codebase's rule and matters most for `warning`, whose amber
 * is sub-3:1 at this size.
 */
function StageLine({ stage }: { stage: Stage }) {
  return (
    <div className="mt-2 flex flex-wrap items-start gap-2">
      <Badge tone={stage.tone}>{stage.label}</Badge>
      <p className="min-w-0 flex-1 text-sm text-ink-2">{stage.detail}</p>
    </div>
  );
}

/**
 * What the technician did, and the household's answer to it.
 *
 * Confirming is what unlocks registration on the server, so this is not
 * decoration: `register` is guarded on `consumer_confirmed_at`. Disputing is
 * offered beside it rather than hidden, because a household that says nothing
 * when the work was not done leaves the application stuck with no signal to
 * anyone.
 */
function VisitPanel({
  visit,
  onVerdict,
  busy,
}: {
  visit: ApplicationVisit;
  onVerdict: (confirmed: boolean, note: string | null) => void;
  busy: boolean;
}) {
  const [disputing, setDisputing] = useState(false);
  const [note, setNote] = useState("");

  const awaitingVerdict =
    visit.status === "completed" &&
    !visit.consumer_confirmed_at &&
    !visit.consumer_disputed_at;

  return (
    <div className="mt-3 rounded-lg bg-plane p-3 text-sm">
      <p className="text-xs font-medium uppercase tracking-wide text-ink-muted">
        The visit
      </p>
      <ul className="mt-1.5 space-y-1 text-ink-2">
        {visit.scheduled_for && (
          <li>
            Scheduled for{" "}
            {new Date(visit.scheduled_for).toLocaleString(undefined, {
              dateStyle: "medium",
              timeStyle: "short",
            })}
          </li>
        )}
        {visit.completed_at && (
          <li>
            Marked complete{" "}
            {new Date(visit.completed_at).toLocaleDateString(undefined, {
              dateStyle: "medium",
            })}
          </li>
        )}
        {visit.installed_serial_no && (
          <li>
            Meter fitted:{" "}
            <span className="font-mono text-ink">
              {visit.installed_serial_no}
            </span>
          </li>
        )}
        {visit.completion_notes && <li>{visit.completion_notes}</li>}
        {visit.failure_reason && (
          <li className="text-status-critical">{visit.failure_reason}</li>
        )}
        {visit.consumer_note && <li>You said: {visit.consumer_note}</li>}
      </ul>

      {awaitingVerdict && !disputing && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={busy}
            onClick={() => onVerdict(true, null)}
            className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
          >
            {busy ? "Sending…" : "Confirm the work is done"}
          </button>
          <button
            type="button"
            onClick={() => setDisputing(true)}
            className="text-sm text-ink-muted underline"
          >
            It is not done
          </button>
        </div>
      )}

      {awaitingVerdict && disputing && (
        <div className="mt-3 space-y-2">
          <label className="block text-sm">
            <span className="font-medium text-ink-2">What is wrong?</span>
            <input
              type="text"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Nobody came, or the meter is not fitted"
              className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
            />
          </label>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={busy || !note.trim()}
              onClick={() => onVerdict(false, note.trim())}
              className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
            >
              {busy ? "Sending…" : "Report it is not done"}
            </button>
            <button
              type="button"
              onClick={() => setDisputing(false)}
              className="text-sm text-ink-muted underline"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** The rules, shown before applying and again after a failed inspection. */
function Requirements({ heading }: { heading: string }) {
  return (
    <div className="rounded-lg border border-hairline p-4">
      <p className="text-sm font-medium text-ink">{heading}</p>
      <ol className="mt-2 space-y-2">
        {NET_METERING_REQUIREMENTS.map((r, i) => (
          <li key={r.title} className="flex gap-2.5 text-sm">
            <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-plane text-xs font-medium text-ink-2">
              {i + 1}
            </span>
            <span>
              <span className="font-medium text-ink">{r.title}.</span>{" "}
              <span className="text-ink-2">{r.body}</span>
            </span>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-xs text-ink-muted">
        What happens next: your district office orders an inspection, a
        technician fits an export-capable meter, you confirm the work, and the
        office registers the meter and approves your net metering. You then add
        the meter from the Meters page and your exports start earning credit.
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Net metering
// ---------------------------------------------------------------------------

/**
 * Consumer requirement 7's other half.
 *
 * Until 2026-08-27 this application was filed *by accident*: registering an
 * array opened a pending agreement as a side effect, so a household asked to
 * sell power back to the grid by filling in a panel-count form and was never
 * told. Registering hardware and applying for net metering are now separate
 * calls, and this is where the second one is made.
 *
 * Two choices, and the first gates the second. The inverter is measured
 * against the property's own consumption over 30 days; only if it clears that
 * bar does the meter dropdown appear at all. The API re-runs the identical
 * check (the page's gate is a convenience, not the enforcement), so a caller
 * that skips the form does not skip the test.
 *
 * Not scoped to the selected site: an inverter belongs to a site, so choosing
 * the inverter is what decides which site's meters are on offer.
 */
function NetMeteringPanel() {
  const queryClient = useQueryClient();

  const applications = useQuery({
    queryKey: queryKeys.netMeteringApplications(),
    queryFn: api.netMeteringApplications,
  });

  // The first dropdown. Account-scoped rather than site-scoped: an inverter
  // belongs to a site, and the household picks the inverter first -- the site
  // follows from it.
  const inverters = useQuery({
    queryKey: queryKeys.inverters(),
    queryFn: api.inverters,
  });

  const [inverterId, setInverterId] = useState("");
  const [point, setPoint] = useState("");

  const chosen = (inverters.data ?? []).find((i) => i.device_id === inverterId);

  // The second dropdown, and it is fetched only once an ELIGIBLE inverter has
  // been chosen. That is the gate stated as data flow rather than as a
  // disabled control: until the production test passes there is no request to
  // make, because there is no question to ask yet.
  const swappable = useQuery({
    queryKey: queryKeys.swappableMeters(chosen?.site_id ?? ""),
    queryFn: () => api.swappableMeters(chosen!.site_id),
    enabled: Boolean(chosen?.eligible),
  });

  const apply = useMutation({
    mutationFn: (vars: { inverterId: string; pointId: string }) =>
      api.applyForNetMetering(vars.inverterId, vars.pointId),
    onSuccess: async () => {
      setInverterId("");
      setPoint("");
      await queryClient.invalidateQueries({
        queryKey: queryKeys.netMeteringApplications(),
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.inverters() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.sites() });
    },
  });

  const withdraw = useMutation({
    mutationFn: (id: string) => api.withdrawNetMeteringApplication(id),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: queryKeys.netMeteringApplications(),
      }),
  });

  const verdict = useMutation({
    mutationFn: ({
      orderId,
      confirmed,
      note,
    }: {
      orderId: string;
      confirmed: boolean;
      note: string | null;
    }) => api.confirmWorkOrder(orderId, confirmed, note),
    // Invalidate rather than patch: confirming notifies the district office
    // and unlocks registration server-side, and the row's stage is derived
    // from state the server owns.
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: queryKeys.netMeteringApplications(),
      }),
  });

  const mine = applications.data ?? [];
  const myInverters = inverters.data ?? [];
  // Panels already inside a connection are done: they are net-metered, and
  // there is nothing left to apply for.
  const applicable = myInverters.filter((i) => !i.billing_point_id);
  const meters = swappable.data ?? [];
  const canSubmit = Boolean(chosen?.eligible && point);

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader
          title="Apply for net metering"
          subtitle="Choose the inverter, then the meter it will replace"
        />
        <div className="space-y-4 p-5">
          <p className="text-sm text-ink-2">
            Net metering is your distribution company agreeing to credit the
            energy your panels send back to the grid. Only a{" "}
            <b>bidirectional</b> meter can measure that, so approving it means
            swapping one of your normal meters for one — which is why this asks
            for both: the panels that will generate, and the meter that will be
            replaced.
          </p>

          <Requirements heading="What you need before you apply" />

          {inverters.isPending ? (
            <Skeleton className="h-10 w-full" />
          ) : applicable.length === 0 ? (
            <p className="rounded-md bg-plane px-3 py-2 text-sm text-ink-2">
              {myInverters.length === 0
                ? "You have no inverter registered. Add your solar on the Meters page — net metering credits what the panels export, so the panels have to exist first."
                : "Every inverter you own is already net-metered."}
            </p>
          ) : (
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                if (canSubmit) apply.mutate({ inverterId, pointId: point });
              }}
            >
              {/* Step 1. Every inverter is listed, including ones that cannot
                  qualify: a dropdown that silently omits the household's only
                  inverter is indistinguishable from a broken page, and the
                  reason underneath is the half they can act on. */}
              <label className="block text-sm">
                <span className="font-medium text-ink-2">1 · Inverter</span>
                <select
                  required
                  value={inverterId}
                  onChange={(e) => {
                    setInverterId(e.target.value);
                    // The meter choice belongs to the inverter's site, so it
                    // cannot survive a change of inverter.
                    setPoint("");
                  }}
                  className="mt-1 block w-full max-w-lg rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
                >
                  <option value="">Choose an inverter…</option>
                  {applicable.map((i) => (
                    <option key={i.device_id} value={i.device_id}>
                      {i.serial_no} · {i.ac_capacity_kw} kW · {i.site_label}
                      {i.eligible ? "" : " — not yet eligible"}
                    </option>
                  ))}
                </select>
              </label>

              {chosen && <Eligibility inverter={chosen} />}

              {/* Step 2 exists only once step 1 has passed. This is the gate:
                  an ineligible inverter does not merely disable the button, it
                  means there is no second question to ask. */}
              {chosen?.eligible && (
                <label className="block text-sm">
                  <span className="font-medium text-ink-2">
                    2 · Meter to be swapped
                  </span>
                  <span className="mt-0.5 block text-xs text-ink-muted">
                    This meter is removed and replaced with a bidirectional one.
                    Its connection, bills and credit balance all stay where they
                    are.
                  </span>
                  {swappable.isPending ? (
                    <Skeleton className="mt-1 h-10 w-full max-w-lg" />
                  ) : meters.length === 0 ? (
                    <span className="mt-1 block rounded-md bg-plane px-3 py-2 text-sm text-ink-2">
                      No meter at {chosen.site_label} can be swapped. Either
                      every connection is already net-metered or already has an
                      application, or there is no meter installed yet.
                    </span>
                  ) : (
                    <select
                      required
                      value={point}
                      onChange={(e) => setPoint(e.target.value)}
                      className="mt-1 block w-full max-w-lg rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
                    >
                      <option value="">Choose a meter…</option>
                      {meters.map((m) => (
                        <option key={m.billing_point_id} value={m.billing_point_id}>
                          {m.point_label} · {m.meter_serial}
                          {m.point_reference ? ` · ${m.point_reference}` : ""}
                        </option>
                      ))}
                    </select>
                  )}
                </label>
              )}

              <button
                type="submit"
                disabled={apply.isPending || !canSubmit}
                className="rounded-lg bg-ink px-4 py-2 text-sm font-medium text-surface disabled:opacity-50"
              >
                {apply.isPending ? "Sending…" : "Apply"}
              </button>
            </form>
          )}

          {apply.error && (
            <p className="text-sm text-status-critical">
              {apply.error instanceof ApiError
                ? String(apply.error.detail)
                : apply.error.message}
            </p>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Your net-metering applications"
          subtitle="Including approved agreements and ones that were turned down"
        />
        {applications.isPending ? (
          <div className="p-5">
            <Skeleton className="h-16 w-full" />
          </div>
        ) : applications.error ? (
          <ErrorState error={applications.error} />
        ) : mine.length === 0 ? (
          <EmptyState
            title="You have not applied yet"
            hint="Once panels are registered on a connection, apply here and your district office decides."
          />
        ) : (
          <ul className="divide-y divide-hairline">
            {mine.map((a) => (
              <NetMeteringRow
                key={a.agreement_id}
                app={a}
                busy={withdraw.isPending && withdraw.variables === a.agreement_id}
                verdictBusy={
                  verdict.isPending &&
                  verdict.variables?.orderId === a.visit?.order_id
                }
                onWithdraw={() => withdraw.mutate(a.agreement_id)}
                onVerdict={(confirmed, note) =>
                  a.visit &&
                  verdict.mutate({ orderId: a.visit.order_id, confirmed, note })
                }
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

/**
 * The verdict on one inverter, with the arithmetic behind it.
 *
 * Shown for an eligible inverter as well as a refused one. A household about
 * to give up a working meter is entitled to see why the system thinks the
 * panels can carry the house, and the same three numbers are what make a
 * refusal actionable rather than merely final.
 *
 * `blocking_reason` is the server's sentence, not one composed here: the 409
 * from applying carries the identical string, so the form and the enforcement
 * can never explain the same refusal two different ways.
 */
function Eligibility({ inverter }: { inverter: Inverter }) {
  const ok = inverter.eligible;
  return (
    <div
      className={[
        "rounded-lg border px-4 py-3",
        ok
          ? "border-status-good/40 bg-status-good/8"
          : "border-status-warning/50 bg-status-warning/8",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={ok ? "good" : "warning"}>
          {ok ? "Eligible" : "Not yet eligible"}
        </Badge>
        <span className="font-mono text-xs text-ink-2">
          {inverter.serial_no}
        </span>
      </div>

      {inverter.blocking_reason && (
        <p className="mt-2 text-sm text-ink-2">{inverter.blocking_reason}</p>
      )}

      {/* The measurements, always. Withheld only when there is genuinely
          nothing to average yet -- a dash would be read as zero output. */}
      {inverter.generation_daily_kwh && inverter.consumption_daily_kwh && (
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
          <div>
            <dt className="text-ink-muted">Produces</dt>
            <dd className="font-medium text-ink">
              {inverter.generation_daily_kwh} kWh/day
            </dd>
          </div>
          <div>
            <dt className="text-ink-muted">This property uses</dt>
            <dd className="font-medium text-ink">
              {inverter.consumption_daily_kwh} kWh/day
            </dd>
          </div>
          <div>
            <dt className="text-ink-muted">Needed to qualify</dt>
            <dd className="font-medium text-ink">
              {inverter.required_daily_kwh} kWh/day
            </dd>
          </div>
        </dl>
      )}
      <p className="mt-2 text-xs text-ink-muted">
        Measured over the last 30 days ({inverter.gen_days} day
        {inverter.gen_days === 1 ? "" : "s"} of generation readings).
      </p>
    </div>
  );
}

function NetMeteringRow({
  app,
  busy,
  verdictBusy,
  onWithdraw,
  onVerdict,
}: {
  app: NetMeteringApplication;
  busy: boolean;
  verdictBusy: boolean;
  onWithdraw: () => void;
  onVerdict: (confirmed: boolean, note: string | null) => void;
}) {
  // Derived from the agreement AND its visit, in one shared place -- see
  // lib/fulfilment.ts. The agreement's own status cannot answer "where is my
  // application?" once the answer is "a technician is on the way".
  const stage = netMeteringStage(app.status, app.visit);
  const failed = app.visit?.status === "failed";

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-medium text-ink">
            {app.point_label} · {formatKwh(app.sanctioned_capacity_kw, 1)} kW
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {app.site_label} · ref{" "}
            <span className="font-mono">{app.approval_ref}</span> · applied{" "}
            {new Date(app.created_at).toLocaleDateString(undefined, {
              dateStyle: "medium",
            })}
          </p>

          <StageLine stage={stage} />

          {app.status === "active" && (
            <p className="mt-1 text-sm text-ink-2">
              Exports have earned credit since {app.effective_from}. Up to{" "}
              {formatKwh(app.export_cap_pct, 0)}% of what you generate may be
              exported
              {app.credit_rollover_months != null && (
                <>, and credit rolls over for {app.credit_rollover_months} months</>
              )}
              .
            </p>
          )}

          {app.visit && (
            <VisitPanel
              visit={app.visit}
              busy={verdictBusy}
              onVerdict={onVerdict}
            />
          )}

          {/* The failure branch the flow calls for: what went wrong, and what
              would have to be true next time. Shown here rather than only in a
              notification, because a notification is read once. */}
          {failed && (
            <div className="mt-3">
              <Requirements heading="To pass the inspection next time" />
              <p className="mt-2 text-xs text-ink-muted">
                Withdraw this application once you have sorted the problem, and
                apply again — the connection is free to reapply as soon as you
                do.
              </p>
            </div>
          )}
        </div>
        {app.status === "pending" && (
          <button
            type="button"
            disabled={busy}
            onClick={onWithdraw}
            className="shrink-0 text-sm text-ink-muted underline disabled:opacity-50"
          >
            {busy ? "Withdrawing…" : "Withdraw"}
          </button>
        )}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// New meter
// ---------------------------------------------------------------------------

/**
 * Consumer requirement 6's other half.
 *
 * A household installs a meter by choosing one already issued to them. When
 * they have none, this is the only way another appears: the district office
 * issues it, and it lands in their list on the Meters page. There is no path
 * that lets a consumer mint hardware.
 */
function MeterPanel() {
  const queryClient = useQueryClient();
  const { siteId, site, sites } = useSelectedSite();
  const [reason, setReason] = useState("");

  const applications = useQuery({
    queryKey: queryKeys.meterApplications(),
    queryFn: api.meterApplications,
  });

  const assets = useQuery({
    queryKey: queryKeys.meterAssets(),
    queryFn: api.meterAssets,
  });

  const apply = useMutation({
    mutationFn: () =>
      api.applyForMeter({ site_id: siteId!, reason: reason.trim() || null }),
    onSuccess: async () => {
      setReason("");
      await queryClient.invalidateQueries({
        queryKey: queryKeys.meterApplications(),
      });
    },
  });

  const withdraw = useMutation({
    mutationFn: (id: string) =>
      api.decideMeterApplication(id, { status: "withdrawn" }),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: queryKeys.meterApplications(),
      }),
  });

  const verdict = useMutation({
    mutationFn: ({
      orderId,
      confirmed,
      note,
    }: {
      orderId: string;
      confirmed: boolean;
      note: string | null;
    }) => api.confirmWorkOrder(orderId, confirmed, note),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.meterApplications(),
      });
      // Confirming is what lets the office register the meter, and a
      // registered meter shows up in the household's stock.
      await queryClient.invalidateQueries({ queryKey: queryKeys.meterAssets() });
    },
  });

  const mine = applications.data ?? [];
  const available = (assets.data ?? []).filter((a) => a.available);
  const openHere = mine.some(
    (a) =>
      a.site_id === siteId &&
      (a.status === "submitted" || a.status === "under_review"),
  );

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader
          title="Apply for a meter"
          subtitle={
            site ? `Filed against ${site.label}` : "Filed against one of your sites"
          }
        />
        <div className="space-y-4 p-5">
          <p className="text-sm text-ink-2">
            Meters are issued by your distribution company. Your district office
            reviews this request and, if it is approved, a meter is issued to you
            — it then appears under <b>Your meters</b> on the Meters page, ready
            to install on a connection.
          </p>

          {available.length > 0 && (
            <p className="rounded-md bg-status-good/12 px-3 py-2 text-sm text-status-good-text">
              You already have {available.length} meter
              {available.length > 1 ? "s" : ""} available to install. You only
              need to apply if you need another.
            </p>
          )}

          {!siteId ? (
            <p className="rounded-md bg-plane px-3 py-2 text-sm text-ink-2">
              Set up a site first — a meter application names the address it is
              wanted at.
            </p>
          ) : openHere ? (
            <p className="rounded-md bg-plane px-3 py-2 text-sm text-ink-2">
              You already have a request waiting for {site?.label}. Withdraw it
              below to change what you asked for.
            </p>
          ) : (
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault();
                apply.mutate();
              }}
            >
              <label className="block text-sm">
                <span className="font-medium text-ink-2">
                  Why do you need one? (optional)
                </span>
                <input
                  type="text"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="New shop unit at the back, needs its own connection"
                  className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
                />
                <span className="mt-1 block text-xs text-ink-muted">
                  A line about what the meter is for makes the decision quicker.
                </span>
              </label>
              <button
                type="submit"
                disabled={apply.isPending}
                className="rounded-lg bg-ink px-4 py-2 text-sm font-medium text-surface disabled:opacity-50"
              >
                {apply.isPending ? "Sending…" : "Send application"}
              </button>
            </form>
          )}

          {apply.error && (
            <p className="text-sm text-status-critical">
              {apply.error instanceof ApiError
                ? String(apply.error.detail)
                : apply.error.message}
            </p>
          )}
          {sites && sites.length > 1 && (
            <p className="text-xs text-ink-muted">
              Applying for {site?.label}. Switch sites with the picker in the
              header to apply for another.
            </p>
          )}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Your meter applications"
          subtitle="Including ones that were withdrawn or turned down"
        />
        {applications.isPending ? (
          <div className="p-5">
            <Skeleton className="h-16 w-full" />
          </div>
        ) : applications.error ? (
          <ErrorState error={applications.error} />
        ) : mine.length === 0 ? (
          <EmptyState
            title="You have not applied yet"
            hint="Applications you submit appear here."
          />
        ) : (
          <ul className="divide-y divide-hairline">
            {mine.map((a) => (
              <MeterApplicationRow
                key={a.application_id}
                app={a}
                busy={
                  withdraw.isPending && withdraw.variables === a.application_id
                }
                verdictBusy={
                  verdict.isPending &&
                  verdict.variables?.orderId === a.visit?.order_id
                }
                onWithdraw={() => withdraw.mutate(a.application_id)}
                onVerdict={(confirmed, note) =>
                  a.visit &&
                  verdict.mutate({ orderId: a.visit.order_id, confirmed, note })
                }
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

function MeterApplicationRow({
  app,
  busy,
  verdictBusy,
  onWithdraw,
  onVerdict,
}: {
  app: MeterApplication;
  busy: boolean;
  verdictBusy: boolean;
  onWithdraw: () => void;
  onVerdict: (confirmed: boolean, note: string | null) => void;
}) {
  const open = app.status === "submitted" || app.status === "under_review";
  const stage = meterApplicationStage(
    app.status,
    app.visit,
    app.issued_meter_available,
  );

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-medium text-ink">{app.site_label}</p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {app.district} · applied{" "}
            {new Date(app.submitted_at).toLocaleDateString(undefined, {
              dateStyle: "medium",
            })}
          </p>
          {app.reason && <p className="mt-1 text-sm text-ink-2">{app.reason}</p>}

          <StageLine stage={stage} />

          {app.decision_notes && (
            <p className="mt-1 text-sm text-ink-2">
              Your district office said: {app.decision_notes}
            </p>
          )}
          {app.issued_serial_no && (
            <p className="mt-1 text-sm text-ink-2">
              Meter <span className="font-mono">{app.issued_serial_no}</span>{" "}
              {app.issued_meter_available
                ? "is registered to you — install it from the Meters page."
                : "is registered to you and is now installed."}
            </p>
          )}

          {app.visit && (
            <VisitPanel
              visit={app.visit}
              busy={verdictBusy}
              onVerdict={onVerdict}
            />
          )}
        </div>
        {open && (
          <button
            type="button"
            disabled={busy}
            onClick={onWithdraw}
            className="shrink-0 text-sm text-ink-muted underline disabled:opacity-50"
          >
            {busy ? "Withdrawing…" : "Withdraw"}
          </button>
        )}
      </div>
    </li>
  );
}
