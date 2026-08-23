import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import {
  api,
  formatKwh,
  queryKeys,
  type DeviceHealth,
  type SiteDevice,
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
 * Equipment health for the signed-in customer's site.
 *
 * Only devices that are supposed to report appear here: the API filters on
 * `reports_telemetry`, because a coverage figure for a device that produces no
 * readings by design would read 0% forever and mean nothing.
 *
 * `health` arrives already derived from interval coverage -- this page renders
 * the verdict, it does not compute one. Keeping that in SQL matters: the
 * threshold that decides "degraded" is the same number for every caller of the
 * endpoint, not something each client re-invents.
 */

const HEALTH: Record<
  DeviceHealth,
  { label: string; tone: string; hint: string }
> = {
  healthy: {
    label: "Reporting",
    tone: "good",
    hint: "Sending readings on schedule.",
  },
  degraded: {
    label: "Gaps",
    tone: "warning",
    hint: "Reporting, but intervals are missing from the last 7 days.",
  },
  silent: {
    label: "Silent",
    tone: "critical",
    hint: "Nothing received in over 48 hours.",
  },
  no_data: {
    // Not "never reported": the API only scans the last 90 days, so a device
    // silent for longer than that lands here too. The wording has to be true
    // of both.
    label: "No recent readings",
    tone: "critical",
    hint: "Nothing received in the last 90 days.",
  },
  faulty: {
    label: "Faulty",
    tone: "critical",
    hint: "Flagged as faulty. A work order may already be open.",
  },
  unknown: {
    label: "Too new",
    tone: "neutral",
    hint: "Installed too recently to judge.",
  },
};

/** What a device is for, which is what decides how much a gap costs. */
function roleOf(device: SiteDevice): string {
  if (device.device_type === "inverter") return "Solar inverter";
  switch (device.billing_role) {
    case "billing":
      return "Billing meter";
    case "generation_only":
      return "Generation meter";
    case "check_meter":
      return "Check meter";
    default:
      return "Meter";
  }
}

/**
 * Pre-selected category on the issue form. A silent billing meter is a data
 * gap, not a meter fault -- the meter may be fine and the link down -- so the
 * category follows the symptom, not the guess.
 */
function issueCategoryFor(device: SiteDevice): string {
  if (device.health === "faulty") {
    return device.device_type === "inverter" ? "inverter_fault" : "meter_fault";
  }
  return "data_gap";
}

export default function CustomerDevices() {
  const { siteId, site } = useSelectedSite();

  const devices = useQuery({
    queryKey: queryKeys.siteDevices(siteId!),
    queryFn: () => api.siteDevices(siteId!),
    enabled: !!siteId,
  });

  return (
    <Card>
      <CardHeader
        title="Equipment"
        subtitle={
          site
            ? `Reporting health for ${site.label}, over the last 7 complete days`
            : undefined
        }
      />

      {devices.isPending || !siteId ? (
        <div className="space-y-3 p-5">
          {[0, 1].map((i) => (
            <Skeleton key={i} className="h-28 w-full" />
          ))}
        </div>
      ) : devices.error ? (
        <ErrorState error={devices.error} />
      ) : devices.data.length === 0 ? (
        <EmptyState
          title="No reporting equipment"
          hint="This site has no device configured to send readings yet."
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {devices.data.map((device) => (
            <DeviceRow key={device.device_id} device={device} />
          ))}
        </ul>
      )}

      {/* The honest footnote. There is no ingest service yet (CLAUDE.md, NOT
          DONE), so "reporting" means rows exist for the expected intervals --
          not that a device answered a poll a minute ago. Saying so is better
          than implying a live heartbeat this system does not have. */}
      <p className="border-t border-hairline px-5 py-3 text-xs text-ink-muted">
        Health is measured from the readings actually stored for each device
        against the number its reporting interval calls for. It is not a live
        heartbeat.
      </p>
    </Card>
  );
}

function DeviceRow({ device }: { device: SiteDevice }) {
  const health = HEALTH[device.health];
  const isBillingMeter = device.billing_role === "billing";
  const coverage = device.coverage_pct == null ? null : Number(device.coverage_pct);

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
            {roleOf(device)}
            <Badge tone={health.tone}>{health.label}</Badge>
            {isBillingMeter && <Badge tone="neutral">bills this site</Badge>}
          </p>
          <p className="mt-0.5 truncate text-xs text-ink-muted">
            {device.serial_no}
            {device.manufacturer && ` · ${device.manufacturer}`}
            {device.model && ` ${device.model}`}
            {device.firmware_version && ` · fw ${device.firmware_version}`}
          </p>
        </div>

        <Link
          to={`/customer/issues?device=${device.device_id}&category=${issueCategoryFor(device)}`}
          className="shrink-0 rounded-md border border-hairline px-2.5 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-plane"
        >
          Report a problem
        </Link>
      </div>

      <p className="mt-2 text-xs text-ink-muted">{health.hint}</p>

      <CoverageBar
        received={device.intervals_received}
        expected={device.intervals_expected}
        pct={coverage}
        health={device.health}
      />

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1.5 text-xs">
        <Figure
          label="Last reading"
          value={
            device.last_reading_at
              ? new Date(device.last_reading_at).toLocaleString(undefined, {
                  dateStyle: "medium",
                  timeStyle: "short",
                })
              : "never"
          }
        />
        <Figure
          label="Intervals"
          value={`${device.intervals_received} of ${device.intervals_expected}`}
        />
        <Figure label="Reports every" value={`${device.interval_minutes} min`} />
        {device.meter_flow && (
          <Figure label="Flow" value={device.meter_flow.replace(/_/g, " ")} />
        )}
        {device.ac_capacity_kw && (
          <Figure
            label="Inverter"
            value={`${formatKwh(device.ac_capacity_kw, 2)} kW AC`}
          />
        )}
        {device.array_count > 0 && (
          <Figure
            label={device.array_count === 1 ? "Array" : "Arrays"}
            value={`${device.array_count} · ${formatKwh(device.dc_capacity_kw, 2)} kW DC`}
          />
        )}
        {device.intervals_suspect > 0 && (
          <Figure
            label="Flagged readings"
            value={`${device.intervals_suspect}`}
          />
        )}
      </dl>
    </li>
  );
}

/**
 * Coverage as a proportion of what was owed.
 *
 * The bar is redundant with the "n of m" figure below it on purpose -- the
 * number is the accessible reading, the bar is what makes a row scannable in a
 * list. Colour never carries the verdict alone; the badge above says it in
 * words.
 */
function CoverageBar({
  received,
  expected,
  pct,
  health,
}: {
  received: number;
  expected: number;
  pct: number | null;
  health: DeviceHealth;
}) {
  if (expected === 0) return null;

  const filled = Math.max(0, Math.min(100, (received / expected) * 100));
  const tone =
    health === "healthy"
      ? "bg-status-good"
      : health === "degraded"
        ? "bg-status-warning"
        : "bg-status-critical";

  return (
    <div className="mt-3">
      <div className="flex items-center gap-3">
        <div
          className="h-1.5 flex-1 overflow-hidden rounded-full bg-hairline"
          role="img"
          aria-label={`${received} of ${expected} expected intervals received`}
        >
          <div
            className={`h-full rounded-full ${tone}`}
            style={{ width: `${filled}%` }}
          />
        </div>
        <span className="tabular shrink-0 text-xs text-ink-2">
          {pct == null ? "—" : `${pct}%`}
        </span>
      </div>
    </div>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-ink-muted">{label}</dt>
      <dd className="mt-0.5 font-medium text-ink-2">{value}</dd>
    </div>
  );
}
