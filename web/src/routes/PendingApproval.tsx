import { useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { AuthShell } from "../components/AuthShell";
import type { ApprovalStatus } from "../lib/api";

/**
 * What a field worker or an installer's staff sees while their registration is
 * undecided — and what they see if it is refused.
 *
 * This stands in place of the whole portal rather than beside it. The API
 * refuses every endpoint outside `/auth` and `/notifications` for these
 * accounts, so rendering the portal shell would be a page of nav items that
 * all lead to 403s, plus counters that never load. One screen that says where
 * the application stands is the honest version of that.
 *
 * The two outcomes are deliberately not the same screen with a different
 * colour. "Waiting" is a thing to come back to; "not approved" is a thing to
 * act on, which is why the rejection reason is quoted rather than summarised —
 * an official wrote it for this person to read.
 *
 * Everything here comes from `/auth/me`, which the account can still reach.
 * Nothing is asserted by the client: the status, the region and the reason are
 * all read off `worker_profile` / `supplier_profile`.
 */
export default function PendingApproval() {
  const { account, signOut } = useAuth();
  const navigate = useNavigate();

  if (!account) return null;

  const context = account.worker ?? account.supplier;
  const status: ApprovalStatus = context?.approval_status ?? "pending";
  const district = context?.service_district;
  const reason = context?.rejection_reason;
  const rejected = status === "rejected";

  // The claim, not the firm: nothing has been resolved yet, and printing a
  // firm name here would tell someone they belong to an organisation an
  // official has not linked them to.
  const subject =
    account.role === "supplier"
      ? account.supplier
        ? `${account.supplier.supplier_name ?? account.supplier.claimed_organisation} — ${account.supplier.job_title ?? "staff"}`
        : "Installer staff account"
      : account.worker?.worker_kind === "government"
        ? `Government worker${
            account.worker.distribution_company_name
              ? ` — ${account.worker.distribution_company_name}`
              : ""
          }`
        : "Private worker";

  return (
    <AuthShell
      title={rejected ? "Registration not approved" : "Waiting for approval"}
      subtitle={
        rejected
          ? "A government official reviewed your registration and did not approve it."
          : `A government official${
              district ? ` in ${district}` : ""
            } is reviewing your registration. You will be notified by email address on this account once they decide.`
      }
      footer={
        <button
          type="button"
          onClick={() => {
            signOut();
            navigate("/login", { replace: true });
          }}
          className="font-medium text-series-import underline-offset-4 transition-colors hover:underline"
        >
          Sign out
        </button>
      }
    >
      <dl className="divide-y divide-hairline rounded-lg border border-hairline">
        <Row label="Name" value={account.full_name} />
        <Row label="Email" value={account.email} />
        {account.national_id && (
          <Row label="National ID" value={account.national_id} mono />
        )}
        <Row label="Registered as" value={subject} />
        {district && <Row label="Region" value={district} />}
      </dl>

      {rejected ? (
        <div className="mt-5 rounded-lg border border-status-critical/20 bg-status-critical/8 px-4 py-3">
          <p className="text-[15px] font-medium text-status-critical">
            Not approved
          </p>
          <p className="mt-1 text-[14px] leading-relaxed text-ink-2">
            {reason
              ? `The official gave this reason: “${reason}”`
              : "No reason was recorded."}
          </p>
          <p className="mt-2 text-[14px] leading-relaxed text-ink-2">
            A registration is decided once. To apply again, register with
            corrected details — and note that a National ID can only be used
            for one account.
          </p>
        </div>
      ) : (
        <p className="mt-5 rounded-lg bg-plane px-4 py-3 text-[14px] leading-relaxed text-ink-2">
          Nothing else needs doing. The official checks your name, National ID
          and{" "}
          {account.role === "supplier" ? "organisation" : "region"} against
          their own records. Sign in again after they decide — this page will
          be replaced by your portal.
        </p>
      )}
    </AuthShell>
  );
}

function Row({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-2.5">
      <dt className="text-[14px] text-ink-2">{label}</dt>
      <dd
        className={`text-[15px] text-ink-1 ${mono ? "font-mono tabular" : ""}`}
      >
        {value}
      </dd>
    </div>
  );
}
