/**
 * Shared device-health vocabulary.
 *
 * The customer sees the equipment on their own site; the supplier sees every
 * device in the fleet. Both read the same `health` verdict off the same query
 * (`device_health` in db/sql/dao/device_queries.sql), so both must call it the
 * same thing. The verdict itself is never computed here -- it arrives already
 * decided, because the threshold that separates "degraded" from "healthy" is
 * one number for every caller of the endpoint, not something each page
 * re-invents.
 */
import type { DeviceHealth, SiteDevice } from "./api";

export const HEALTH: Record<
  DeviceHealth,
  { label: string; tone: string; hint: string; rank: number }
> = {
  // `rank` orders a fleet worst-first. It is presentation order only -- it
  // never feeds a colour, so a filter that changes which states are on screen
  // cannot repaint the survivors.
  faulty: {
    label: "Faulty",
    tone: "critical",
    hint: "Flagged as faulty. A work order may already be open.",
    rank: 0,
  },
  no_data: {
    // Not "never reported": the query only scans the last 90 days, so a device
    // silent for longer lands here too. The wording has to be true of both.
    label: "No recent readings",
    tone: "critical",
    hint: "Nothing received in the last 90 days.",
    rank: 1,
  },
  silent: {
    label: "Silent",
    tone: "critical",
    hint: "Nothing received in over 48 hours.",
    rank: 2,
  },
  degraded: {
    label: "Gaps",
    tone: "warning",
    hint: "Reporting, but intervals are missing from the last 7 days.",
    rank: 3,
  },
  unknown: {
    label: "Too new",
    tone: "neutral",
    hint: "Installed too recently to judge.",
    rank: 4,
  },
  healthy: {
    label: "Reporting",
    tone: "good",
    hint: "Sending readings on schedule.",
    rank: 5,
  },
};

/** Anything that is not `healthy` or `unknown` wants someone's attention. */
export function needsAttention(health: DeviceHealth): boolean {
  return health !== "healthy" && health !== "unknown";
}

/** What a device is for, which is what decides how much a gap costs. */
export function roleOf(device: SiteDevice): string {
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
export function issueCategoryFor(device: SiteDevice): string {
  if (device.health === "faulty") {
    return device.device_type === "inverter" ? "inverter_fault" : "meter_fault";
  }
  return "data_gap";
}

/** Worst state on a site, for a fleet row that stands in for several devices. */
export function worstHealth(devices: SiteDevice[]): DeviceHealth | null {
  if (devices.length === 0) return null;
  return devices.reduce<DeviceHealth>(
    (worst, d) => (HEALTH[d.health].rank < HEALTH[worst].rank ? d.health : worst),
    devices[0].health,
  );
}
