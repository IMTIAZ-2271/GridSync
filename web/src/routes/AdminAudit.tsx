import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api, queryKeys, type AdminAuditQuery } from "../lib/api";
import AuditList from "../components/AuditList";
import { Card, CardHeader, EmptyState, ErrorState, Skeleton } from "../components/ui";
import Pager from "../components/Pager";

const PAGE = 50;

/**
 * The audit log, newest first. Append-only in the database: nothing on this
 * page -- or anywhere else -- can edit or remove an entry.
 */
export default function AdminAudit() {
  const [entityId, setEntityId] = useState("");
  const [action, setAction] = useState("");
  const [offset, setOffset] = useState(0);

  const query: AdminAuditQuery = {
    entity_id: entityId.trim() || undefined,
    action: action || undefined,
    limit: PAGE,
    offset,
  };
  const audit = useQuery({
    queryKey: queryKeys.adminAudit(query),
    queryFn: () => api.adminAudit(query),
    placeholderData: keepPreviousData,
  });

  return (
    <Card>
      <CardHeader
        title="Audit log"
        subtitle="Every change made through the admin panel, with who made it and why"
      />
      <div className="flex flex-wrap gap-3 border-b border-hairline px-5 py-3">
        <label className="flex flex-col gap-1 text-xs text-ink-muted">
          Action
          <select
            value={action}
            onChange={(e) => {
              setAction(e.target.value);
              setOffset(0);
            }}
            className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
          >
            <option value="">All actions</option>
            <option value="account.status">Changed account status</option>
            <option value="account.sessions_revoked">Signed out everywhere</option>
            <option value="account.password_reset">Reset password</option>
            <option value="account.admin_granted">Granted admin</option>
            <option value="account.admin_revoked">Revoked admin</option>
            <option value="admin.bootstrap">Admin created from the command line</option>
          </select>
        </label>
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs text-ink-muted">
          Account or entity ID
          <input
            value={entityId}
            onChange={(e) => {
              setEntityId(e.target.value);
              setOffset(0);
            }}
            placeholder="Paste a full ID"
            className="rounded-md border border-hairline bg-surface px-2 py-1.5 font-mono text-sm text-ink"
          />
        </label>
      </div>

      {audit.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : audit.error ? (
        <ErrorState error={audit.error} />
      ) : audit.data.items.length === 0 ? (
        <EmptyState title="Nothing matches" hint="Try clearing the filters." />
      ) : (
        <>
          <AuditList entries={audit.data.items} />
          <Pager total={audit.data.total} offset={offset} pageSize={PAGE} onOffset={setOffset} />
        </>
      )}
    </Card>
  );
}
