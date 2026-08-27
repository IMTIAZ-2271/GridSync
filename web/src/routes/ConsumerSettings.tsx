import { Link } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { useSelectedSite } from "../components/SitePicker";
import ConsumptionLimitCard from "../components/ConsumptionLimitCard";
import { Card, CardHeader } from "../components/ui";

/**
 * Consumer requirement 12: one place for the things a household configures.
 *
 * Small on purpose. A settings page that invents preferences nobody asked for
 * is worse than a short one -- everything here is either a real stored setting
 * or a fact about the account, and nothing is a placeholder.
 *
 * The monthly limit lives here rather than on the overview, which is where it
 * was first built. The overview is what someone opens to see how the house is
 * doing; a budget they set once and change rarely is a setting, and requirement
 * 5 and requirement 12 are the same screen for that reason.
 */
export default function ConsumerSettings() {
  const { account } = useAuth();
  const { siteId, site, sites } = useSelectedSite();

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader
          title="Account"
          subtitle="Held against your National ID and used on every bill"
        />
        <dl className="grid gap-x-8 gap-y-3 p-5 sm:grid-cols-2">
          <Field label="Name" value={account?.full_name} />
          <Field label="Email" value={account?.email} />
          <Field label="Phone" value={account?.phone ?? "Not recorded"} />
          {/* Shown, never editable: changing the National ID an account was
              registered under is an identity change, not a preference, and the
              UNIQUE on it is what stops one person holding two accounts. */}
          <Field
            label="National ID"
            value={account?.national_id ?? "Not recorded"}
            hint="Contact your utility to correct this"
          />
        </dl>
      </Card>

      {siteId && <ConsumptionLimitCard siteId={siteId} />}

      <Card>
        <CardHeader
          title="Your connections"
          subtitle={
            sites && sites.length > 1
              ? `${sites.length} sites on this account`
              : site?.label
          }
        />
        <div className="p-5 text-sm text-ink-2">
          <p>
            Billing meters, solar arrays and everything else attached to a site
            are managed on the{" "}
            <Link to="/consumer/meters" className="font-medium underline">
              Meters
            </Link>{" "}
            page. Each connection is billed on its own and keeps its own credit
            balance.
          </p>
        </div>
      </Card>
    </div>
  );
}

function Field({
  label,
  value,
  hint,
}: {
  label: string;
  value?: string | null;
  hint?: string;
}) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-ink-muted">{label}</dt>
      <dd className="mt-0.5 text-sm text-ink">{value ?? "—"}</dd>
      {hint && <p className="mt-0.5 text-xs text-ink-muted">{hint}</p>}
    </div>
  );
}
