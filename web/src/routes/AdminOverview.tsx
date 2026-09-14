import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api, queryKeys } from "../lib/api";
import { ROLE_LABEL } from "../auth/AuthContext";
import AuditList from "../components/AuditList";
import {
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
  Stat,
  StatSkeleton,
} from "../components/ui";

/**
 * Where an admin starts: how many accounts of each kind exist, what is waiting
 * on an official anywhere in the system, and the latest admin actions.
 *
 * The waiting work links into the government portal, which an admin can open
 * and where every district's queue is shown to them unscoped.
 */
export default function AdminOverview() {
  const overview = useQuery({
    queryKey: queryKeys.adminOverview(),
    queryFn: api.adminOverview,
  });

  if (overview.error) {
    return (
      <Card>
        <ErrorState error={overview.error} />
      </Card>
    );
  }

  const data = overview.data;
  const total = data
    ? Object.values(data.accounts_by_role).reduce((a, b) => a + (b ?? 0), 0)
    : 0;
  const approvals = data ? data.pending_workers + data.pending_suppliers : 0;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {!data ? (
          <>
            <StatSkeleton />
            <StatSkeleton />
            <StatSkeleton />
            <StatSkeleton />
          </>
        ) : (
          <>
            <Stat
              label="Accounts"
              value={String(total)}
              footnote={(["consumer", "worker", "government", "supplier", "admin"] as const)
                .filter((r) => data.accounts_by_role[r])
                .map((r) => `${data.accounts_by_role[r]} ${ROLE_LABEL[r].toLowerCase()}`)
                .join(" · ")}
            />
            <Stat
              label="Not active"
              value={String(
                (data.accounts_by_status.suspended ?? 0) + (data.accounts_by_status.closed ?? 0),
              )}
              unit="accounts"
              footnote={`${data.accounts_by_status.suspended ?? 0} suspended · ${data.accounts_by_status.closed ?? 0} closed`}
            />
            <Stat
              label="Registrations waiting"
              value={String(approvals)}
              footnote={
                <>
                  <Link className="underline" to="/government/workers">
                    {data.pending_workers} workers
                  </Link>
                  {" · "}
                  <Link className="underline" to="/government/supplier-registrations">
                    {data.pending_suppliers} installer staff
                  </Link>
                </>
              }
            />
            <Stat
              label="Applications waiting"
              value={String(data.open_meter_applications + data.pending_agreements)}
              footnote={
                <>
                  <Link className="underline" to="/government/meter-applications">
                    {data.open_meter_applications} meter
                  </Link>
                  {" · "}
                  <Link className="underline" to="/government/agreements">
                    {data.pending_agreements} net metering
                  </Link>
                </>
              }
            />
          </>
        )}
      </div>

      <Card>
        <CardHeader
          title="Recent admin actions"
          subtitle="The last ten entries in the audit log"
          action={
            <Link to="/admin/audit" className="text-sm text-ink-2 underline">
              All entries
            </Link>
          }
        />
        {!data ? (
          <div className="space-y-3 p-5">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : data.recent_audit.length === 0 ? (
          <EmptyState
            title="No admin actions yet"
            hint="Suspending an account, resetting a password or granting admin is recorded here, with the reason given."
          />
        ) : (
          <AuditList entries={data.recent_audit} />
        )}
      </Card>
    </div>
  );
}
