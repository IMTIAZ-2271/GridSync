/**
 * Shared vocabulary for a billing meter's connection to its utility's network.
 *
 * The official's district list and the supplier's fleet table read the same
 * `state` off `GET /api/commissioning`, so both must call it the same thing.
 * The state arrives already decided -- the server resolves the stored
 * deadlines -- and nothing here recomputes it.
 *
 * Deliberately absent from every consumer page (Consumer 9).
 */
import type { MeterConnection, ConnectionState } from "./api";

export const CONNECTION: Record<
  ConnectionState,
  { label: string; tone: string; hint: string }
> = {
  not_commissioned: {
    label: "Not connected",
    tone: "warning",
    hint: "Never handed to its utility's network.",
  },
  offered: {
    label: "Waiting for utility",
    tone: "neutral",
    hint: "Handed to the utility's network, not claimed yet.",
  },
  offer_lapsed: {
    label: "Not claimed",
    tone: "critical",
    hint: "The utility's network did not claim this meter in time.",
  },
  activated: {
    label: "Connecting",
    tone: "neutral",
    hint: "Claimed by the utility's network; waiting for its first readings.",
  },
  activation_lapsed: {
    label: "No readings",
    tone: "critical",
    hint: "Claimed, but no readings arrived in time.",
  },
  live: {
    label: "Connected",
    tone: "good",
    hint: "Readings arrive from the utility's network.",
  },
  failed: {
    label: "Failed",
    tone: "critical",
    hint: "The connection did not complete.",
  },
  cancelled: {
    label: "Disconnected",
    tone: "warning",
    hint: "Its connection was ended.",
  },
};

/** The sentence a person acts on. A failure says why, in the network's own
 *  words when it refused the meter. */
export function connectionHint(meter: MeterConnection): string {
  if (meter.state !== "failed") return CONNECTION[meter.state].hint;
  switch (meter.failed_reason) {
    case "rejected_by_source":
      return meter.failure_detail
        ? `Refused by the utility's network: ${meter.failure_detail}`
        : "Refused by the utility's network. Check the serial recorded.";
    case "no_source":
      return "No network is set up for this meter's utility.";
    case "offer_expired":
      return CONNECTION.offer_lapsed.hint;
    case "activation_expired":
      return CONNECTION.activation_lapsed.hint;
    default:
      return CONNECTION.failed.hint;
  }
}
