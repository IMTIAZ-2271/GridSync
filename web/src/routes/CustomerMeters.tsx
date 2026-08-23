import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type BillingPoint,
  type MeterRegisterResult,
} from "../lib/api";
import { useSelectedSite } from "../components/SitePicker";
import {
  Badge,
  Card,
  CardHeader,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * The household's billing meters (consumer requirement 3).
 *
 * A site holds one *billing point* per connection and one active billing
 * meter per point (rule 7, which counts per point since migration
 * d5a7c2b91e40). So "add a billing meter" is really "open a new connection
 * and register the meter serving it", which is what POST /sites/{id}/meter
 * does in one call.
 *
 * Each connection is billed independently and carries its own credit
 * balance, because that is what the utility actually issues. The page says so
 * once a second meter exists rather than on every household's screen.
 */
const FIELD =
  "w-full rounded-md border border-hairline bg-surface px-3 py-2 text-sm " +
  "text-ink outline-none focus:border-series-import";

export default function CustomerMeters() {
  const { siteId, site, isPending: siteLoading, error: siteError } =
    useSelectedSite();

  const pointsQuery = useQuery({
    queryKey: queryKeys.sitePoints(siteId ?? ""),
    queryFn: () => api.listBillingPoints(siteId!),
    enabled: Boolean(siteId),
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

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="Billing meters"
          subtitle={
            site
              ? `Connections at ${site.label}. Each is billed separately and keeps its own credit balance.`
              : undefined
          }
        />
        {pointsQuery.error && <ErrorState error={pointsQuery.error as Error} />}
        {pointsQuery.isPending && <Skeleton className="h-24" />}
        {pointsQuery.data && <PointList points={pointsQuery.data} />}
      </Card>

      <AddMeter siteId={siteId} />
    </div>
  );
}

function PointList({ points }: { points: BillingPoint[] }) {
  if (points.length === 0) {
    return <p className="text-sm text-ink-2">No connections on this site.</p>;
  }
  return (
    <ul className="divide-y divide-hairline">
      {points.map((p) => (
        <li key={p.point_id} className="flex flex-wrap items-center gap-3 py-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-ink">{p.label}</span>
              {p.has_solar && <Badge tone="good">Solar</Badge>}
              {/* A connection with no meter yet is a legal state -- the point
                  is created before the meter is registered against it. */}
              {!p.meter_device_id && <Badge tone="warning">No meter yet</Badge>}
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
        </li>
      ))}
    </ul>
  );
}

function AddMeter({ siteId }: { siteId: string }) {
  const queryClient = useQueryClient();
  const [serial, setSerial] = useState("");
  const [label, setLabel] = useState("");
  const [reference, setReference] = useState("");
  const [manufacturer, setManufacturer] = useState("Hexing");
  const [model, setModel] = useState("HXE310-BD");
  const [result, setResult] = useState<MeterRegisterResult | null>(null);

  const add = useMutation({
    mutationFn: () =>
      api.registerMeter(siteId, {
        serial_no: serial.trim(),
        manufacturer: manufacturer.trim(),
        model: model.trim(),
        // Blank means "reuse the site's one empty connection if there is
        // exactly one, otherwise open a numbered one" -- the server decides,
        // because it is the side that knows what is already there.
        point_label: label.trim() || undefined,
        point_reference: reference.trim() || undefined,
      }),
    onSuccess: (r) => {
      setResult(r);
      setSerial("");
      setLabel("");
      setReference("");
      // The new meter changes the connection list, the bills (a new point can
      // be billed), and the equipment page. Invalidate rather than patch:
      // the server backfilled 90 days of readings and derived a health
      // verdict from them, and the client must not guess at either.
      queryClient.invalidateQueries({ queryKey: queryKeys.sitePoints(siteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.siteDevices(siteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.siteBills(siteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.sites() });
    },
  });

  const error = add.error as ApiError | Error | null;

  return (
    <Card>
      <CardHeader
        title="Add a billing meter"
        subtitle="Register another connection at this address. It gets 90 days of history so its chart and bills are not empty."
      />

      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          setResult(null);
          add.mutate();
        }}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Meter serial" hint="Printed on the meter's faceplate.">
            <input
              value={serial}
              onChange={(e) => setSerial(e.target.value)}
              className={`font-mono ${FIELD}`}
              placeholder="HXE-2026-00042"
              required
            />
          </Field>
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
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
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
          <Field label="Manufacturer">
            <input
              value={manufacturer}
              onChange={(e) => setManufacturer(e.target.value)}
              className={FIELD}
              required
            />
          </Field>
          <Field label="Model">
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className={FIELD}
              required
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

        {result && (
          <p className="rounded-md bg-status-good/12 px-3 py-2 text-xs text-status-good-text">
            Registered <span className="font-mono">{result.serial_no}</span> on{" "}
            <strong>{result.point_label}</strong> with{" "}
            {result.readings_backfilled.toLocaleString()} readings from{" "}
            {result.backfill_from} to {result.backfill_to}.
          </p>
        )}

        <button
          type="submit"
          disabled={add.isPending || !serial.trim()}
          className="rounded-md bg-portal-customer px-4 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-50"
        >
          {add.isPending ? "Registering…" : "Add meter"}
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
