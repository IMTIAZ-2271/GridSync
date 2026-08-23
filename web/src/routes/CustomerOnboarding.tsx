import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  queryKeys,
  type ConnectionType,
  type TariffPlan,
} from "../lib/api";
import { useSelectedSite } from "../components/SitePicker";
import { Card, CardHeader } from "../components/ui";
import { FIELD } from "../components/AuthShell";

/**
 * Onboarding for a customer whose account owns no site yet.
 *
 * Three steps collect the site, its billing meter, and (optionally) its
 * solar array, then a single submit sequence walks the backend chain:
 * POST /sites -> /meter -> [/solar] -> /bill. Each backfill call writes
 * roughly 4,300 rows in one request/response, so the finishing screen shows
 * a simulated progress bar per step rather than a spinner with no sense of
 * how long "a few seconds" actually is.
 */

const CONNECTION_TYPES: { id: ConnectionType; label: string }[] = [
  { id: "residential", label: "Residential" },
  { id: "commercial", label: "Commercial" },
  { id: "industrial", label: "Industrial" },
];

type FinishPhase = "site" | "meter" | "solar" | "billing" | "done";
type StepStatus = "pending" | "active" | "done" | "error";

interface SiteForm {
  address_line: string;
  city: string;
  district: string;
  postal_code: string;
  connection_type: ConnectionType;
  sanctioned_load_kw: string;
  tariff_plan_id: string;
}

interface MeterForm {
  serial_no: string;
  manufacturer: string;
  model: string;
}

interface SolarForm {
  capacity_kw: string;
  panel_count: string;
  azimuth_deg: string;
  tilt_deg: string;
}

const STEP_LABELS = ["Site", "Billing meter", "Solar"];

export default function CustomerOnboarding() {
  const queryClient = useQueryClient();
  const { setSiteId } = useSelectedSite();

  const [step, setStep] = useState(1);
  const [error, setError] = useState<string | null>(null);

  const [site, setSite] = useState<SiteForm>({
    address_line: "",
    city: "Dhaka",
    district: "",
    postal_code: "",
    connection_type: "residential",
    sanctioned_load_kw: "5",
    tariff_plan_id: "",
  });

  const [meter, setMeter] = useState<MeterForm>({
    serial_no: "",
    manufacturer: "Hexing",
    model: "HXE310-BD",
  });

  const [solar, setSolar] = useState<SolarForm>({
    capacity_kw: "3.5",
    panel_count: "8",
    azimuth_deg: "180",
    tilt_deg: "23",
  });

  // Served rather than hardcoded: the same list validates the POST, and a
  // district typed freehand used to become its own row in the government's
  // rollup ("Dhaka", "dhaka" and "g" all existed at once).
  const districtsQuery = useQuery({
    queryKey: queryKeys.districts(),
    queryFn: api.listDistricts,
  });

  const plansQuery = useQuery({
    queryKey: queryKeys.tariffPlans(site.connection_type),
    queryFn: () => api.listTariffPlans(site.connection_type),
  });

  // Default (and re-default, on a connection-type change) to the first plan
  // that matches, so the field is never left pointing at a stale selection.
  useEffect(() => {
    const plans = plansQuery.data;
    if (!plans?.length) return;
    if (!plans.some((p) => p.plan_id === site.tariff_plan_id)) {
      setSite((s) => ({ ...s, tariff_plan_id: plans[0].plan_id }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plansQuery.data]);

  const [finishing, setFinishing] = useState(false);
  const [phase, setPhase] = useState<FinishPhase | null>(null);
  const [withSolar, setWithSolar] = useState(false);
  const [failedPhase, setFailedPhase] = useState<FinishPhase | null>(null);

  const siteValid =
    site.address_line.trim() &&
    site.city.trim() &&
    site.district.trim() &&
    Number(site.sanctioned_load_kw) > 0 &&
    site.tariff_plan_id;

  const meterValid =
    meter.serial_no.trim() && meter.manufacturer.trim() && meter.model.trim();

  const solarValid =
    Number(solar.capacity_kw) > 0 && Number(solar.panel_count) > 0;

  async function finish(includeSolar: boolean) {
    setError(null);
    setFailedPhase(null);
    setWithSolar(includeSolar);
    setFinishing(true);
    try {
      setPhase("site");
      const createdSite = await api.createSite({
        address_line: site.address_line.trim(),
        city: site.city.trim(),
        district: site.district.trim(),
        postal_code: site.postal_code.trim() || null,
        connection_type: site.connection_type,
        sanctioned_load_kw: Number(site.sanctioned_load_kw),
        tariff_plan_id: site.tariff_plan_id,
      });

      setPhase("meter");
      await api.registerMeter(createdSite.site_id, {
        serial_no: meter.serial_no.trim(),
        manufacturer: meter.manufacturer.trim(),
        model: meter.model.trim(),
      });

      if (includeSolar) {
        setPhase("solar");
        await api.registerSolar(createdSite.site_id, {
          capacity_kw: Number(solar.capacity_kw),
          panel_count: Number(solar.panel_count),
          azimuth_deg: Number(solar.azimuth_deg),
          tilt_deg: Number(solar.tilt_deg),
        });
      }

      setPhase("billing");
      await api.billSite(createdSite.site_id);

      await queryClient.invalidateQueries({ queryKey: queryKeys.sites() });
      setSiteId(createdSite.site_id);
      setPhase("done");
    } catch (err) {
      setFailedPhase(phase);
      setError(err instanceof Error ? err.message : "Setup failed");
      setFinishing(false);
    }
  }

  if (finishing || phase === "done") {
    return (
      <FinishingScreen
        phase={phase}
        withSolar={withSolar}
        error={error}
        failedPhase={failedPhase}
        onRetry={() => {
          setFinishing(false);
          setPhase(null);
          setError(null);
        }}
      />
    );
  }

  return (
    <div className="mx-auto max-w-2xl">
      <div className="mb-6 text-center">
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          Set up your connection
        </h1>
        <p className="mt-1.5 text-sm text-ink-2">
          Your account has no service point yet. A few details and you will
          have live readings and a first bill.
        </p>
      </div>

      <div className="mb-5 flex items-center justify-center gap-2">
        {STEP_LABELS.map((label, i) => {
          const n = i + 1;
          const state = n === step ? "active" : n < step ? "done" : "pending";
          return (
            <div key={label} className="flex items-center gap-2">
              <span
                className={[
                  "flex size-6 items-center justify-center rounded-full text-xs font-medium",
                  state === "active" && "bg-series-import text-white",
                  state === "done" && "bg-status-good/20 text-status-good-text",
                  state === "pending" && "bg-hairline text-ink-muted",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                {state === "done" ? "✓" : n}
              </span>
              <span
                className={`text-xs ${n === step ? "font-medium text-ink" : "text-ink-muted"}`}
              >
                {label}
              </span>
              {n < STEP_LABELS.length && (
                <span className="mx-1 h-px w-6 bg-hairline" aria-hidden />
              )}
            </div>
          );
        })}
      </div>

      <Card>
        {step === 1 && (
          <>
            <CardHeader
              title="Where is this connection?"
              subtitle="Address, load, and the tariff plan it bills under."
            />
            <div className="space-y-4 p-5">
              <Field label="Address">
                <input
                  value={site.address_line}
                  onChange={(e) =>
                    setSite((s) => ({ ...s, address_line: e.target.value }))
                  }
                  placeholder="House 12, Road 5"
                  className={FIELD}
                />
              </Field>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="City">
                  <input
                    value={site.city}
                    onChange={(e) =>
                      setSite((s) => ({ ...s, city: e.target.value }))
                    }
                    className={FIELD}
                  />
                </Field>
                <Field label="District">
                  <select
                    value={site.district}
                    onChange={(e) =>
                      setSite((s) => ({ ...s, district: e.target.value }))
                    }
                    className={FIELD}
                  >
                    <option value="">Select a district</option>
                    {(districtsQuery.data ?? []).map((d) => (
                      <option key={d.name} value={d.name}>
                        {d.name}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Postal code (optional)">
                  <input
                    value={site.postal_code}
                    onChange={(e) =>
                      setSite((s) => ({ ...s, postal_code: e.target.value }))
                    }
                    className={FIELD}
                  />
                </Field>
                <Field label="Sanctioned load (kW)">
                  <input
                    type="number"
                    min="0.1"
                    step="0.1"
                    value={site.sanctioned_load_kw}
                    onChange={(e) =>
                      setSite((s) => ({
                        ...s,
                        sanctioned_load_kw: e.target.value,
                      }))
                    }
                    className={FIELD}
                  />
                </Field>
              </div>
              <div>
                <span className="text-xs font-medium text-ink-2">
                  Connection type
                </span>
                <div className="mt-1.5 flex gap-2">
                  {CONNECTION_TYPES.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() =>
                        setSite((s) => ({ ...s, connection_type: c.id }))
                      }
                      className={[
                        "flex-1 rounded-md border px-3 py-2 text-sm transition-colors",
                        site.connection_type === c.id
                          ? "border-series-import bg-series-import/10 font-medium text-series-import"
                          : "border-hairline text-ink-2 hover:bg-plane",
                      ].join(" ")}
                    >
                      {c.label}
                    </button>
                  ))}
                </div>
              </div>
              <Field label="Tariff plan">
                {plansQuery.isPending ? (
                  <div className="skeleton h-9 w-full" aria-hidden />
                ) : plansQuery.error ? (
                  <p className="text-xs text-status-critical">
                    Could not load tariff plans.
                  </p>
                ) : !plansQuery.data?.length ? (
                  <p className="text-xs text-ink-muted">
                    No plan is currently effective for this connection type.
                  </p>
                ) : (
                  <select
                    value={site.tariff_plan_id}
                    onChange={(e) =>
                      setSite((s) => ({ ...s, tariff_plan_id: e.target.value }))
                    }
                    className={FIELD}
                  >
                    {plansQuery.data.map((p: TariffPlan) => (
                      <option key={p.plan_id} value={p.plan_id}>
                        {p.name} — {p.currency} {p.fixed_monthly_charge}/mo fixed
                      </option>
                    ))}
                  </select>
                )}
              </Field>

              <div className="flex justify-end pt-2">
                <button
                  type="button"
                  disabled={!siteValid}
                  onClick={() => setStep(2)}
                  className="rounded-md bg-series-import px-4 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Continue
                </button>
              </div>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <CardHeader
              title="Register the billing meter"
              subtitle="The one bidirectional meter that measures import and export at the grid boundary."
            />
            <div className="space-y-4 p-5">
              <Field label="Meter serial">
                <input
                  value={meter.serial_no}
                  onChange={(e) =>
                    setMeter((m) => ({ ...m, serial_no: e.target.value }))
                  }
                  placeholder="HXE-000123"
                  className={`font-mono ${FIELD}`}
                />
              </Field>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Manufacturer">
                  <input
                    value={meter.manufacturer}
                    onChange={(e) =>
                      setMeter((m) => ({ ...m, manufacturer: e.target.value }))
                    }
                    className={FIELD}
                  />
                </Field>
                <Field label="Model">
                  <input
                    value={meter.model}
                    onChange={(e) =>
                      setMeter((m) => ({ ...m, model: e.target.value }))
                    }
                    className={FIELD}
                  />
                </Field>
              </div>

              <div className="flex justify-between pt-2">
                <button
                  type="button"
                  onClick={() => setStep(1)}
                  className="rounded-md border border-hairline px-4 py-2.5 text-sm text-ink-2 transition-colors hover:bg-plane"
                >
                  Back
                </button>
                <button
                  type="button"
                  disabled={!meterValid}
                  onClick={() => setStep(3)}
                  className="rounded-md bg-series-import px-4 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Continue
                </button>
              </div>
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <CardHeader
              title="Add solar (optional)"
              subtitle="Skip this if the site has no panels yet -- it can be added later."
            />
            <div className="space-y-4 p-5">
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Capacity (kW)">
                  <input
                    type="number"
                    min="0.1"
                    step="0.1"
                    value={solar.capacity_kw}
                    onChange={(e) =>
                      setSolar((s) => ({ ...s, capacity_kw: e.target.value }))
                    }
                    className={FIELD}
                  />
                </Field>
                <Field label="Panel count">
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={solar.panel_count}
                    onChange={(e) =>
                      setSolar((s) => ({ ...s, panel_count: e.target.value }))
                    }
                    className={FIELD}
                  />
                </Field>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Azimuth (deg)">
                  <input
                    type="number"
                    min="0"
                    max="359"
                    value={solar.azimuth_deg}
                    onChange={(e) =>
                      setSolar((s) => ({ ...s, azimuth_deg: e.target.value }))
                    }
                    className={FIELD}
                  />
                </Field>
                <Field label="Tilt (deg)">
                  <input
                    type="number"
                    min="0"
                    max="90"
                    value={solar.tilt_deg}
                    onChange={(e) =>
                      setSolar((s) => ({ ...s, tilt_deg: e.target.value }))
                    }
                    className={FIELD}
                  />
                </Field>
              </div>

              {error && (
                <p className="rounded-md bg-status-critical/10 px-3 py-2 text-xs text-status-critical">
                  {error}
                </p>
              )}

              <div className="flex justify-between pt-2">
                <button
                  type="button"
                  onClick={() => setStep(2)}
                  className="rounded-md border border-hairline px-4 py-2.5 text-sm text-ink-2 transition-colors hover:bg-plane"
                >
                  Back
                </button>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => finish(false)}
                    className="rounded-md border border-hairline px-4 py-2.5 text-sm text-ink-2 transition-colors hover:bg-plane"
                  >
                    Skip solar
                  </button>
                  <button
                    type="button"
                    disabled={!solarValid}
                    onClick={() => finish(true)}
                    className="rounded-md bg-series-import px-4 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    Finish setup
                  </button>
                </div>
              </div>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-ink-2">{label}</span>
      <div className="mt-1">{children}</div>
    </label>
  );
}

// ---------------------------------------------------------------------------
// Finishing screen
// ---------------------------------------------------------------------------

function FinishingScreen({
  phase,
  withSolar,
  error,
  failedPhase,
  onRetry,
}: {
  phase: FinishPhase | null;
  withSolar: boolean;
  error: string | null;
  failedPhase: FinishPhase | null;
  onRetry: () => void;
}) {
  const order: FinishPhase[] = withSolar
    ? ["site", "meter", "solar", "billing"]
    : ["site", "meter", "billing"];

  function statusOf(p: FinishPhase): StepStatus {
    if (failedPhase === p) return "error";
    if (phase === "done") return "done";
    const currentIndex = order.indexOf(phase!);
    const pIndex = order.indexOf(p);
    if (pIndex < currentIndex) return "done";
    if (pIndex === currentIndex) return "active";
    return "pending";
  }

  return (
    <div className="mx-auto max-w-md">
      <Card>
        <CardHeader
          title={phase === "done" ? "You're all set" : "Setting up your connection"}
          subtitle={
            phase === "done"
              ? "Redirecting to your dashboard."
              : "This takes a few seconds -- each device backfills 90 days of readings."
          }
        />
        <div className="space-y-4 p-5">
          <StepRow
            label="Site"
            status={statusOf("site")}
          />
          <StepRow
            label="Billing meter"
            detail="Backfilling 90 days of readings (~4,300 rows)"
            status={statusOf("meter")}
          />
          {withSolar && (
            <StepRow
              label="Solar array"
              detail="Backfilling 90 days of generation (~4,300 rows)"
              status={statusOf("solar")}
            />
          )}
          <StepRow
            label="First bills"
            detail="Billing every complete month"
            status={statusOf("billing")}
          />

          {error && (
            <div className="space-y-3">
              <p className="rounded-md bg-status-critical/10 px-3 py-2 text-xs text-status-critical">
                {error}
              </p>
              <button
                type="button"
                onClick={onRetry}
                className="w-full rounded-md border border-hairline px-4 py-2.5 text-sm text-ink-2 transition-colors hover:bg-plane"
              >
                Back to form
              </button>
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

function StepRow({
  label,
  detail,
  status,
}: {
  label: string;
  detail?: string;
  status: StepStatus;
}) {
  return (
    <div>
      <div className="flex items-center gap-2.5">
        <span
          aria-hidden
          className={[
            "flex size-5 shrink-0 items-center justify-center rounded-full text-[10px] font-medium",
            status === "done" && "bg-status-good/20 text-status-good-text",
            status === "active" && "animate-pulse bg-series-import/20 text-series-import",
            status === "pending" && "bg-hairline text-ink-muted",
            status === "error" && "bg-status-critical/15 text-status-critical",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          {status === "done" ? "✓" : status === "error" ? "!" : ""}
        </span>
        <span
          className={`text-sm ${
            status === "pending" ? "text-ink-muted" : "font-medium text-ink"
          }`}
        >
          {label}
        </span>
      </div>
      {status === "active" && detail && (
        <div className="ml-7.5 mt-1.5">
          <p className="text-xs text-ink-muted">{detail}</p>
          <BackfillBar />
        </div>
      )}
    </div>
  );
}

/**
 * Simulated progress: the backfill is one request/response, so there is no
 * real percentage to report. Creeps toward 92% over ~2.5s via a CSS
 * transition and holds -- the step's checkmark, not this bar, is what
 * signals completion.
 */
function BackfillBar() {
  const [width, setWidth] = useState(4);

  useEffect(() => {
    const id = requestAnimationFrame(() => setWidth(92));
    return () => cancelAnimationFrame(id);
  }, []);

  return (
    <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-hairline">
      <div
        className="h-full rounded-full bg-series-import transition-all duration-[2500ms] ease-out"
        style={{ width: `${width}%` }}
      />
    </div>
  );
}
