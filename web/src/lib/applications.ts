/**
 * Solar-application vocabulary, shared by the household's page and the
 * installer's inbox.
 *
 * Two screens read the same rows from opposite ends -- one applies, the other
 * decides -- so they have to agree on what "under review" is called and what
 * colour it wears. The enum values are the schema's; only the human labels and
 * tones live here.
 */
import type { ApplicationStatus } from "./api";

export const APPLICATION_STATUS_LABEL: Record<ApplicationStatus, string> = {
  submitted: "Submitted",
  under_review: "Under review",
  accepted: "Accepted",
  rejected: "Not accepted",
  withdrawn: "Withdrawn",
  completed: "Installed",
};

/**
 * `submitted` and `under_review` are the only live states -- everything else is
 * settled, which is what `solar_application_one_open` keys its partial unique
 * index on. Neutral for the two waiting states: waiting is not good or bad, and
 * a queue where every row shouts is a queue nobody reads.
 */
export const APPLICATION_STATUS_TONE: Record<ApplicationStatus, string> = {
  submitted: "warning",
  under_review: "neutral",
  accepted: "good",
  rejected: "critical",
  withdrawn: "neutral",
  completed: "good",
};

export const OPEN_APPLICATION_STATES: ApplicationStatus[] = [
  "submitted",
  "under_review",
];

export function isOpenApplication(status: ApplicationStatus): boolean {
  return OPEN_APPLICATION_STATES.includes(status);
}
