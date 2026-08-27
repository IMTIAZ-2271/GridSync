import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type BillingPoint,
  type MeterAsset,
  type MeterRegisterResult,
} from "../lib/api";
import { useSelectedSite } from "../components/SitePicker";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * The household's billing meters (consumer requirement 3), and where they get
 * installed (consumer requirement 6).
 *
 * A site holds one *billing point* per connection and one active billing meter
 * per point (rule 7, which counts per point since migration d5a7c2b91e40).
 * Each connection is billed independently and carries its own credit balance,
 * because that is what the utility actually issues.
 *
 * What changed on 2026-08-27: **there is no serial number field here any
 * more.** A meter is the utility's hardware, issued to a consumer against
 * their identity, and typing a number at a form let a household conjure
 * hardware nobody owns. The page now shows what they have been issued and asks
 * only which connection each one serves. A household with nothing spare is
 * pointed at the application, which is the only way more hardware appears.
 */
const FIELD =
  "w-full rounded-md border border-hairline bg-surface px-3 py-2 text-sm " +
  "text-ink outline-none focus:border-series-import";

export default function ConsumerMeters() {
  const { siteId, site, isPending: siteLoading, error: siteError } =
    useSelectedSite();

  const pointsQuery = useQuery({
    queryKey: queryKeys.sitePoints(siteId ?? ""),
    queryFn: () => api.listBillingPoints(siteId!),
    enabled: Boolean(siteId),
  });

  // Not scoped to the selected site: a meter is issued to the *person*, so one
  // sitting spare can be installed at whichever of their addresses needs it.
  const assetsQuery = useQuery({
    queryKey: queryKeys.meterAssets(),
    queryFn: api.meterAssets,
  });

  if (siteError) return <ErrorState error={siteError as Error} />;
  if (siteLoading) return <Skeleton className="h-40" />;
  if (!siteId) {
    return (
      <Card>
        <CardHeader
          title="No connection yet"
          subtitle="Set up a service point from the overview first."
        />
      </Card>
    );
  }

  const assets = assetsQuery.data ?? [];
  const available = assets.filter((a) => a.available);

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="Connections"
          subtitle={
            site
              ? `At ${site.label}. Each is billed separately and keeps its own credit balance.`
              : undefined
          }
        />
        {pointsQuery.error && <ErrorState error={pointsQuery.error as Error} />}
        {pointsQuery.isPending && <Skeleton className="h-24" />}
        {pointsQuery.data && (
          <PointList siteId={siteId} points={pointsQuery.data} />
        )}
      </Card>

      <Card>
        <CardHeader
          title="Your meters"
          subtitle="Hardware your distribution company has issued to you."
        />
        {assetsQuery.error && <ErrorState error={assetsQuery.error as Error} />}
        {assetsQuery.isPending && <Skeleton className="h-24" />}
        {assetsQuery.data && <AssetList assets={assets} />}
      </Card>

      {available.length > 0 ? (
        <AssignMeter
          siteId={siteId}
          available={available}
          points={pointsQuery.data ?? []}
        />
      ) : (
        assetsQuery.data && <NothingSpare />
      )}
    </div>
  );
}

function PointList({
  siteId,
  points,
}: {
  siteId: string;
  points: BillingPoint[];
}) {
  const [adding, setAdding] = useState<string | null>(null);

  if (points.length === 0) {
    return (
      <div className="p-5">
        <p className="text-sm text-ink-2">No connections on this site.</p>
      </div>
    );
  }
  return (
    <ul className="divide-y divide-hairline px-5">
      {points.map((p) => (
        <li key={p.point_id} className="py-3">
          <div className="flex flex-wrap items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-ink">{p.label}</span>
                {p.has_solar && <Badge tone="good">Solar</Badge>}
                {/* A connection with no meter yet is a legal state -- the point
                    is created before the meter is registered against it. */}
                {!p.meter_device_id && (
                  <Badge tone="warning">No meter yet</Badge>
                )}
              </div>
              <p className="mt-0.5 text-xs text-ink-2">
                {p.meter_serial ? (
                  <>
                    Meter <span className="font-mono">{p.meter_serial}</span>
                  </>
                ) : (
                  "Awaiting a meter"
                )}
                {p.reference && (
                  <>
                    {" · "}Connection{" "}
                    <span className="font-mono">{p.reference}</span>
                  </>
                )}
              </p>
            </div>
            {/* Registering an array was reachable only from the onboarding
                wizard, which renders at zero sites -- so a household that had
                panels fitted afterwards could never record them, and once net
                metering became an explicit application (2026-08-27) could
                never apply for it either. Solar needs the meter first: rule 6
                says only the bidirectional meter knows the import/export
                split, so there is nothing to net against without one. */}
            {p.meter_device_id && (
              <button
                type="button"
                onClick={() =>
                  setAdding(adding === p.point_id ? null : p.point_id)
                }
                className="shrink-0 rounded-md border border-hairline px-3 py-1.5 text-xs text-ink-2 transition-colors hover:bg-plane"
              >
                {adding === p.point_id
                  ? "Cancel"
                  : p.has_solar
                    ? "Add another array"
                    : "Add solar"}
              </button>
            )}
          </div>
          {adding === p.point_id && (
            <AddSolar siteId={siteId} point={p} onDone={() => setAdding(null)} />
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * Register an array on one connection.
 *
 * Scoped to a point, never to the site: the backfill re-nets that point's
 * billing meter against the point's total capacity, and netting the site's
 * whole fleet into whichever meter answered first would credit one connection
 * for another's export.
 *
 * Registering panels is deliberately NOT an application for net metering --
 * that is a separate act with a different counterparty, and it lives on the
 * Applications page. The success message says where to go next rather than
 * leaving the household to wonder why nothing is being credited.
 */
function AddSolar({
  siteId,
  point,
  onDone,
}: {
  siteId: string;
  point: BillingPoint;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [capacity, setCapacity] = useState("3.5");
  const [panels, setPanels] = useState("8");
  const [azimuth, setAzimuth] = useState("180");
  const [tilt, setTilt] = useState("23");
  const [done, setDone] = useState(false);

  const add = useMutation({
    mutationFn: () =>
      api.registerSolar(siteId, {
        point_id: point.point_id,
        capacity_kw: Number(capacity),
        panel_count: Number(panels),
        azimuth_deg: Number(azimuth),
        tilt_deg: Number(tilt),
      }),
    onSuccess: () => {
      setDone(true);
      // The meter's own history was re-netted against the new capacity, so
      // every reading-derived view moves with it -- not just the array list.
      queryClient.invalidateQueries({ queryKey: queryKeys.sitePoints(siteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.siteDevices(siteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.siteArrays(siteId) });
      queryClient.invalidateQueries({ queryKey: ["sites", siteId, "readings"] });
      queryClient.invalidateQueries({ queryKey: ["sites", siteId, "summary"] });
      queryClient.invalidateQueries({ queryKey: queryKeys.sites() });
    },
  });

  const error = add.error as ApiError | Error | null;
  const valid = Number(capacity) > 0 && Number(panels) > 0;

  if (done) {
    return (
      <div className="mt-3 rounded-md bg-status-good/12 px-3 py-2 text-xs text-status-good-text">
        Registered on <strong>{point.label}</strong>, and its meter history was
        re-netted so past export shows up. Next: apply for{" "}
        <Link to="/consumer/applications" className="underline">
          net metering
        </Link>{" "}
        — until your utility approves that, exports earn no credit.
      </div>
    );
  }

  return (
    <form
      className="mt-3 space-y-3 rounded-lg bg-plane p-3"
      onSubmit={(e) => {
        e.preventDefault();
        add.mutate();
      }}
    >
      <div className="grid gap-3 sm:grid-cols-4">
        <Field label="Capacity (kW)">
          <input
            type="number"
            min="0.1"
            step="0.1"
            value={capacity}
            onChange={(e) => setCapacity(e.target.value)}
            className={FIELD}
          />
        </Field>
        <Field label="Panels">
          <input
            type="number"
            min="1"
            step="1"
            value={panels}
            onChange={(e) => setPanels(e.target.value)}
            className={FIELD}
          />
        </Field>
        <Field label="Azimuth (deg)">
          <input
            type="number"
            min="0"
            max="359"
            value={azimuth}
            onChange={(e) => setAzimuth(e.target.value)}
            className={FIELD}
          />
        </Field>
        <Field label="Tilt (deg)">
          <input
            type="number"
            min="0"
            max="90"
            value={tilt}
            onChange={(e) => setTilt(e.target.value)}
            className={FIELD}
          />
        </Field>
      </div>
      {error && (
        <p className="rounded-md bg-status-critical/10 px-3 py-2 text-xs text-status-critical">
          {error instanceof ApiError && typeof error.detail === "string"
            ? error.detail
            : error.message}
        </p>
      )}
      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={add.isPending || !valid}
          className="rounded-md bg-portal-consumer px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {add.isPending ? "Registering…" : "Register array"}
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

function AssetList({ assets }: { assets: MeterAsset[] }) {
  if (assets.length === 0) {
    return (
      <EmptyState
        title="No meters issued to you yet"
        hint="Apply for one from the Applications page and it will appear here once your district office approves it."
      />
    );
  }
  return (
    <ul className="divide-y divide-hairline px-5">
      {assets.map((a) => (
        <li
          key={a.meter_asset_id}
          className="flex flex-wrap items-center gap-3 py-3"
        >
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-sm text-ink">{a.serial_no}</span>
              {a.available ? (
                <Badge tone="good">Available</Badge>
              ) : a.removed_at ? (
                <Badge tone="neutral">Removed</Badge>
              ) : (
                <Badge tone="neutral">In use</Badge>
              )}
            </div>
            <p className="mt-0.5 text-xs text-ink-2">
              {[a.manufacturer, a.model].filter(Boolean).join(" ") ||
                "Make not recorded"}
              {a.issued_by && <> · issued by {a.issued_by}</>}
              {!a.available && a.site_label && (
                <>
                  {" · "}serving <strong>{a.point_label}</strong> at{" "}
                  {a.site_label}
                </>
              )}
            </p>
          </div>
        </li>
      ))}
    </ul>
  );
}

function NothingSpare() {
  return (
    <Card>
      <CardHeader
        title="Need another meter?"
        subtitle="You have none spare to install."
      />
      <div className="p-5 text-sm text-ink-2">
        <p>
          Meters are issued by your distribution company, not registered here.
          Apply through this site and your district office will issue one to
          you; it appears in <b>Your meters</b> above and you can then put it on
          a connection.
        </p>
        <Link
          to="/consumer/applications"
          className="mt-3 inline-block rounded-md bg-portal-consumer px-4 py-2 text-sm font-medium text-white"
        >
          Apply for a meter
        </Link>
      </div>
    </Card>
  );
}

function AssignMeter({
  siteId,
  available,
  points,
}: {
  siteId: string;
  available: MeterAsset[];
  points: BillingPoint[];
}) {
  const queryClient = useQueryClient();
  const [assetId, setAssetId] = useState(available[0]?.meter_asset_id ?? "");
  // "" means open a new connection; otherwise the point to meter or replace.
  const [target, setTarget] = useState("");
  const [label, setLabel] = useState("");
  const [reference, setReference] = useState("");
  const [result, setResult] = useState<MeterRegisterResult | null>(null);

  const targetPoint = points.find((p) => p.point_id === target);
  // Replacing rather than adding. This is what a net-metering approval ends
  // in: the connection already has a meter, rule 7 allows exactly one, and the
  // new one is bidirectional. The old meter is retired, not deleted -- the
  // connection keeps every reading it ever recorded.
  const replacing = Boolean(targetPoint?.meter_device_id);

  // available[0] can change under us when another tab assigns one, so fall
  // back rather than leaving the select pointing at a meter that is gone.
  const selected = useMemo(
    () =>
      available.find((a) => a.meter_asset_id === assetId) ?? available[0],
    [available, assetId],
  );

  const add = useMutation({
    mutationFn: () =>
      api.registerMeter(siteId, {
        meter_asset_id: selected.meter_asset_id,
        point_id: target || undefined,
        // Blank means "reuse the site's one empty connection if there is
        // exactly one, otherwise open a numbered one" -- the server decides,
        // because it is the side that knows what is already there.
        point_label: target ? undefined : label.trim() || undefined,
        point_reference: target ? undefined : reference.trim() || undefined,
        replace_existing: replacing,
      }),
    onSuccess: (r) => {
      setResult(r);
      setLabel("");
      setReference("");
      // The new meter changes the connection list, the meter stock, the bills
      // (a new point can be billed), and the equipment page. Invalidate rather
      // than patch: the server backfilled 90 days of readings and derived a
      // health verdict from them, and the client must not guess at either.
      queryClient.invalidateQueries({ queryKey: queryKeys.sitePoints(siteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.meterAssets() });
      queryClient.invalidateQueries({ queryKey: queryKeys.siteDevices(siteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.siteBills(siteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.sites() });
    },
  });

  const error = add.error as ApiError | Error | null;

  return (
    <Card>
      <CardHeader
        title="Install a meter"
        subtitle="Put one of your available meters on a connection at this address."
      />

      <form
        className="space-y-4 p-5"
        onSubmit={(e) => {
          e.preventDefault();
          setResult(null);
          add.mutate();
        }}
      >
        <Field
          label="Which meter"
          hint="Only meters issued to you and not already installed."
        >
          <select
            value={selected?.meter_asset_id ?? ""}
            onChange={(e) => setAssetId(e.target.value)}
            className={FIELD}
          >
            {available.map((a) => (
              <option key={a.meter_asset_id} value={a.meter_asset_id}>
                {a.serial_no}
                {a.manufacturer ? ` — ${a.manufacturer} ${a.model ?? ""}` : ""}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Where does it go?"
          hint="An existing connection that already has a meter is replaced — the old one is retired and this one takes over."
        >
          <select
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            className={FIELD}
          >
            <option value="">Open a new connection</option>
            {points.map((p) => (
              <option key={p.point_id} value={p.point_id}>
                {p.label}
                {p.meter_serial
                  ? ` — replace ${p.meter_serial}`
                  : " — no meter yet"}
              </option>
            ))}
          </select>
        </Field>

        {replacing && (
          <p className="rounded-md bg-plane px-3 py-2 text-xs text-ink-2">
            <b>{targetPoint?.meter_serial}</b> will be retired and{" "}
            <b>{selected?.serial_no}</b> takes over{" "}
            <b>{targetPoint?.label}</b>. Your bills, credit balance and reading
            history all stay with the connection — only the hardware changes.
            The new meter records from now on.
          </p>
        )}

        {!target && (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Name this connection"
              hint="Optional. How you tell it apart — 'Shop', 'Flat 2'."
            >
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                className={FIELD}
                placeholder="Shop"
              />
            </Field>
            <Field
              label="Connection number"
              hint="Optional. From your utility bill."
            >
              <input
                value={reference}
                onChange={(e) => setReference(e.target.value)}
                className={`font-mono ${FIELD}`}
                placeholder="DESCO-77219"
              />
            </Field>
          </div>
        )}

        {error && (
          <p className="rounded-md bg-status-critical/10 px-3 py-2 text-xs text-status-critical">
            {error instanceof ApiError && typeof error.detail === "string"
              ? error.detail
              : error.message}
          </p>
        )}

        {result && (
          <p className="rounded-md bg-status-good/12 px-3 py-2 text-xs text-status-good-text">
            Installed <span className="font-mono">{result.serial_no}</span> on{" "}
            <strong>{result.point_label}</strong>
            {result.replaced_serial_no ? (
              <>
                , replacing{" "}
                <span className="font-mono">{result.replaced_serial_no}</span>.
                The connection keeps its history and its credit balance.
              </>
            ) : (
              <>
                {" "}
                with {result.readings_backfilled.toLocaleString()} readings from{" "}
                {result.backfill_from} to {result.backfill_to}.
              </>
            )}
          </p>
        )}

        <button
          type="submit"
          disabled={add.isPending || !selected}
          className="rounded-md bg-portal-consumer px-4 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-50"
        >
          {add.isPending
            ? "Installing…"
            : replacing
              ? "Replace meter"
              : "Install meter"}
        </button>
      </form>
    </Card>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="text-xs font-medium text-ink-2">{label}</label>
      <div className="mt-1">{children}</div>
      {hint && <p className="mt-1 text-xs text-ink-muted">{hint}</p>}
    </div>
  );
}
