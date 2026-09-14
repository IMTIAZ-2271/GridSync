import { useEffect, useRef, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type AccountStatus,
  type AdminAccount,
  type AdminAccountQuery,
  type Role,
} from "../lib/api";
import { ROLE_LABEL, useAuth } from "../auth/AuthContext";
import { ACCOUNT_STATUS, formatWhen } from "../lib/admin";
import AuditList from "../components/AuditList";
import Pager from "../components/Pager";
import { Badge, Card, CardHeader, EmptyState, ErrorState, Skeleton } from "../components/ui";

const PAGE = 50;
const ROLES: Role[] = ["consumer", "worker", "government", "supplier", "admin"];
const MIN_PASSWORD = 10;

/**
 * Every account in the system, and the four things an admin can do to one:
 * change its status, sign it out everywhere, reset its password, and grant or
 * revoke admin (households only -- see routes_admin_accounts.py for why).
 *
 * Every action asks for a reason, because the reason is what the audit row is
 * for. Nothing here is optimistic: each mutation invalidates and the server's
 * answer is what renders, so a guard the server applies (the last admin, your
 * own account) surfaces as its own sentence rather than a row that flickers.
 */
export default function AdminAccounts() {
  const [q, setQ] = useState("");
  const [role, setRole] = useState<Role | "">("");
  const [status, setStatus] = useState<AccountStatus | "">("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);

  const query: AdminAccountQuery = {
    q: q.trim() || undefined,
    role: role || undefined,
    status: status || undefined,
    limit: PAGE,
    offset,
  };
  const accounts = useQuery({
    queryKey: queryKeys.adminAccounts(query),
    queryFn: () => api.adminAccounts(query),
    placeholderData: keepPreviousData,
  });

  const resetPage = () => setOffset(0);

  // On a narrow screen the detail sits below a long list: bring it into view
  // once it has loaded, or the tap appears to do nothing. Not on selection --
  // the panel is a short skeleton then, the page is not tall enough yet, and the
  // scroll stops short.
  const panel = useRef<HTMLDivElement>(null);
  const revealPanel = () => {
    if (window.matchMedia("(max-width: 1023px)").matches) {
      panel.current?.scrollIntoView({ block: "start" });
    }
  };

  return (
    <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]">
      <Card>
        <CardHeader
          title="Accounts"
          subtitle={accounts.data ? `${accounts.data.total} matching` : "Every account in the system"}
        />
        <div className="flex flex-wrap gap-3 border-b border-hairline px-5 py-3">
          <label className="flex min-w-0 flex-1 basis-48 flex-col gap-1 text-xs text-ink-muted">
            Search
            <input
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                resetPage();
              }}
              placeholder="Email, name, phone or National ID"
              className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            Role
            <select
              value={role}
              onChange={(e) => {
                setRole(e.target.value as Role | "");
                resetPage();
              }}
              className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
            >
              <option value="">All roles</option>
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABEL[r]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            Status
            <select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value as AccountStatus | "");
                resetPage();
              }}
              className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
            >
              <option value="">Any status</option>
              {(Object.keys(ACCOUNT_STATUS) as AccountStatus[]).map((s) => (
                <option key={s} value={s}>
                  {ACCOUNT_STATUS[s].label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {accounts.isPending ? (
          <div className="space-y-3 p-5">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : accounts.error ? (
          <ErrorState error={accounts.error} />
        ) : accounts.data.items.length === 0 ? (
          <EmptyState title="No accounts match" hint="Try clearing the filters." />
        ) : (
          <>
            <ul className="divide-y divide-hairline">
              {accounts.data.items.map((account) => (
                <AccountRow
                  key={account.account_id}
                  account={account}
                  selected={selected === account.account_id}
                  onSelect={() => setSelected(account.account_id)}
                />
              ))}
            </ul>
            <Pager
              total={accounts.data.total}
              offset={offset}
              pageSize={PAGE}
              onOffset={setOffset}
            />
          </>
        )}
      </Card>

      {selected ? (
        <div ref={panel} className="scroll-mt-4">
          <AccountDetail
            accountId={selected}
            onClose={() => setSelected(null)}
            onLoaded={revealPanel}
          />
        </div>
      ) : (
        <Card className="hidden lg:block">
          <EmptyState
            title="Select an account"
            hint="Its details, sites, actions and audit history open here."
          />
        </Card>
      )}
    </div>
  );
}

function AccountRow({
  account,
  selected,
  onSelect,
}: {
  account: AdminAccount;
  selected: boolean;
  onSelect: () => void;
}) {
  const state = ACCOUNT_STATUS[account.status];
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? "true" : undefined}
        className={`flex w-full flex-wrap items-center justify-between gap-3 px-5 py-3 text-left transition-colors hover:bg-plane ${
          selected ? "bg-plane" : ""
        }`}
      >
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium text-ink">{account.full_name}</span>
          <span className="block truncate text-xs text-ink-muted">
            {account.email}
            {account.district && ` · ${account.district}`}
          </span>
        </span>
        <span className="flex flex-wrap items-center gap-1.5">
          <Badge tone="neutral">{ROLE_LABEL[account.role]}</Badge>
          {account.approval_status && account.approval_status !== "approved" && (
            <Badge tone="warning">{account.approval_status}</Badge>
          )}
          {account.status !== "active" && <Badge tone={state.tone}>{state.label}</Badge>}
        </span>
      </button>
    </li>
  );
}

type Action = "status" | "sessions" | "password" | "admin";

function AccountDetail({
  accountId,
  onClose,
  onLoaded,
}: {
  accountId: string;
  onClose: () => void;
  /** Called once per account, when its detail first renders. */
  onLoaded: () => void;
}) {
  const { account: me } = useAuth();
  const queryClient = useQueryClient();
  const [action, setAction] = useState<Action | null>(null);
  const [nextStatus, setNextStatus] = useState<AccountStatus>("suspended");
  const [reason, setReason] = useState("");
  const [password, setPassword] = useState("");
  const [done, setDone] = useState<string | null>(null);

  const detail = useQuery({
    queryKey: queryKeys.adminAccount(accountId),
    queryFn: () => api.adminAccount(accountId),
  });
  const loadedId = detail.data?.account_id;
  useEffect(() => {
    if (loadedId) onLoaded();
    // Once per account: onLoaded is a fresh function every parent render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedId]);

  const history = useQuery({
    queryKey: queryKeys.adminAudit({ entity_id: accountId, limit: 20 }),
    queryFn: () => api.adminAudit({ entity_id: accountId, limit: 20 }),
  });

  const act = useMutation({
    mutationFn: async (): Promise<string> => {
      const why = reason.trim();
      switch (action) {
        case "status":
          await api.adminSetStatus(accountId, { status: nextStatus, reason: why });
          return `Status changed to ${ACCOUNT_STATUS[nextStatus].label.toLowerCase()}. Their sessions have ended.`;
        case "sessions":
          await api.adminRevokeSessions(accountId, { reason: why });
          return "Signed out everywhere. They must sign in again.";
        case "password":
          await api.adminResetPassword(accountId, { password, reason: why });
          return "Password reset and sessions ended. Give them the temporary password directly.";
        case "admin":
          await api.adminSetAdmin(accountId, {
            granted: detail.data?.role !== "admin",
            reason: why,
          });
          return detail.data?.role === "admin" ? "Admin revoked." : "Admin granted.";
        default:
          throw new Error("no action selected");
      }
    },
    onSuccess: (message) => {
      setDone(message);
      setAction(null);
      setReason("");
      setPassword("");
    },
    // Everything admin-shaped may have moved: the row, the list it sits in, the
    // overview counts and the audit trail.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["admin"] }),
  });

  if (detail.isPending) {
    return (
      <Card className="p-5">
        <Skeleton className="h-6 w-40" />
        <Skeleton className="mt-3 h-4 w-full" />
      </Card>
    );
  }
  if (detail.error) {
    return (
      <Card>
        <ErrorState error={detail.error} />
      </Card>
    );
  }

  const account = detail.data;
  const isMe = me?.account_id === account.account_id;
  const canBeAdmin = account.role === "consumer" || account.role === "admin";
  const reasonOk = reason.trim().length >= 3;
  const passwordOk = action !== "password" || password.length >= MIN_PASSWORD;
  const failure =
    act.error instanceof ApiError && typeof act.error.detail === "string"
      ? act.error.detail
      : act.error?.message;

  const start = (next: Action) => {
    setAction(next);
    setDone(null);
    act.reset();
    if (next === "status") {
      setNextStatus(account.status === "active" ? "suspended" : "active");
    }
  };

  return (
    <Card>
      <CardHeader
        title={account.full_name}
        subtitle={account.email}
        action={
          <button type="button" onClick={onClose} className="text-sm text-ink-muted underline">
            Close
          </button>
        }
      />

      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 px-5 py-4 text-sm">
        <dt className="text-ink-muted">Role</dt>
        <dd className="text-ink">{ROLE_LABEL[account.role]}</dd>
        <dt className="text-ink-muted">Status</dt>
        <dd>
          <Badge tone={ACCOUNT_STATUS[account.status].tone}>
            {ACCOUNT_STATUS[account.status].label}
          </Badge>
        </dd>
        {account.approval_status && (
          <>
            <dt className="text-ink-muted">Registration</dt>
            <dd className="text-ink">{account.approval_status}</dd>
          </>
        )}
        {account.district && (
          <>
            <dt className="text-ink-muted">District</dt>
            <dd className="text-ink">{account.district}</dd>
          </>
        )}
        <dt className="text-ink-muted">National ID</dt>
        <dd className="font-mono text-ink">{account.national_id ?? "—"}</dd>
        <dt className="text-ink-muted">Phone</dt>
        <dd className="text-ink">{account.phone ?? "—"}</dd>
        <dt className="text-ink-muted">Joined</dt>
        <dd className="text-ink">{formatWhen(account.created_at)}</dd>
        <dt className="text-ink-muted">Sessions ended</dt>
        <dd className="text-ink">
          {account.sessions_valid_after ? formatWhen(account.sessions_valid_after) : "Never"}
        </dd>
        <dt className="text-ink-muted">Sites</dt>
        <dd className="text-ink">
          {account.sites.length === 0
            ? "None"
            : account.sites.map((s) => `${s.label} (${s.district})`).join(", ")}
        </dd>
      </dl>

      <div className="border-t border-hairline px-5 py-4">
        <p className="text-xs font-medium tracking-wide text-ink-muted uppercase">Actions</p>
        {isMe && (
          <p className="mt-2 text-xs text-ink-muted">
            This is your account. Another admin changes its status, password or admin rights.
          </p>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          {!isMe && (
            <ActionButton active={action === "status"} onClick={() => start("status")}>
              {account.status === "active" ? "Suspend or close" : "Change status"}
            </ActionButton>
          )}
          <ActionButton active={action === "sessions"} onClick={() => start("sessions")}>
            Sign out everywhere
          </ActionButton>
          {!isMe && (
            <ActionButton active={action === "password"} onClick={() => start("password")}>
              Reset password
            </ActionButton>
          )}
          {!isMe && canBeAdmin && (
            <ActionButton active={action === "admin"} onClick={() => start("admin")}>
              {account.role === "admin" ? "Revoke admin" : "Make admin"}
            </ActionButton>
          )}
        </div>

        {action && (
          <form
            className="mt-4 space-y-3 rounded-lg border border-hairline p-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (reasonOk && passwordOk) act.mutate();
            }}
          >
            {action === "status" && (
              <label className="flex flex-col gap-1 text-xs text-ink-muted">
                New status
                <select
                  value={nextStatus}
                  onChange={(e) => setNextStatus(e.target.value as AccountStatus)}
                  className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
                >
                  {(Object.keys(ACCOUNT_STATUS) as AccountStatus[])
                    .filter((s) => s !== account.status)
                    .map((s) => (
                      <option key={s} value={s}>
                        {ACCOUNT_STATUS[s].label}
                      </option>
                    ))}
                </select>
              </label>
            )}
            {action === "password" && (
              <label className="flex flex-col gap-1 text-xs text-ink-muted">
                Temporary password (at least {MIN_PASSWORD} characters)
                <input
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
                />
              </label>
            )}
            {action === "admin" && (
              <p className="text-xs text-ink-2">
                {account.role === "admin"
                  ? "They return to a household account and lose the admin panel."
                  : "They can see all data and change any account, including yours."}
              </p>
            )}
            <label className="flex flex-col gap-1 text-xs text-ink-muted">
              Reason (kept in the audit log)
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
              />
            </label>
            {failure && <p className="text-xs text-status-critical">{failure}</p>}
            <div className="flex items-center gap-3">
              <button
                type="submit"
                disabled={!reasonOk || !passwordOk || act.isPending}
                className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
              >
                {act.isPending ? "Working…" : "Confirm"}
              </button>
              <button
                type="button"
                onClick={() => setAction(null)}
                className="text-sm text-ink-muted underline"
              >
                Cancel
              </button>
            </div>
          </form>
        )}
        {done && <p className="mt-3 text-sm text-status-good-text">{done}</p>}
      </div>

      <div className="border-t border-hairline">
        <p className="px-5 pt-4 text-xs font-medium tracking-wide text-ink-muted uppercase">
          Admin history
        </p>
        {history.data && history.data.items.length > 0 ? (
          <AuditList entries={history.data.items} />
        ) : (
          <p className="px-5 py-3 text-sm text-ink-muted">
            {history.isPending ? "Loading…" : "No admin has acted on this account."}
          </p>
        )}
      </div>
    </Card>
  );
}

function ActionButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-md border px-3 py-1.5 text-sm transition-colors ${
        active ? "border-ink bg-ink text-surface" : "border-hairline text-ink-2 hover:bg-plane"
      }`}
    >
      {children}
    </button>
  );
}
