import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Notification, type NotificationSeverity } from "../lib/api";
import { Badge } from "./ui";

/**
 * The notification panel (consumer requirement 11, used by every role).
 *
 * Polled rather than pushed. Almost everything that writes a notification here
 * happens outside this browser tab -- a worker marking a job complete, an
 * official approving an agreement, and eventually the jobs sweep expiring an
 * offer -- so there is nothing for a mutation to invalidate. Thirty seconds is
 * slow enough to be free and fast enough that "I marked it done" and "the
 * consumer saw it" happen in the same conversation.
 *
 * `unread_count` comes from the same response as the list, so the badge and
 * the panel can never disagree.
 */
const POLL_MS = 30_000;

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api.listNotifications(),
    refetchInterval: POLL_MS,
    // The panel is ambient. Refetching it every time the window regains focus
    // is the one case where a poll is genuinely worth jumping the interval.
    refetchOnWindowFocus: true,
  });

  const items = data?.items ?? [];
  const unread = data?.unread_count ?? 0;

  const markAll = useMutation({
    mutationFn: () => api.markAllNotificationsRead(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const markOne = useMutation({
    mutationFn: (id: number) => api.markNotificationRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  // Close on an outside click or Escape. Both, because the panel is opened
  // from a toolbar and either gesture is a reasonable way to dismiss it.
  useEffect(() => {
    if (!open) return;
    function onPointer(e: MouseEvent) {
      if (!wrapper.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={wrapper} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={
          unread ? `Notifications, ${unread} unread` : "Notifications"
        }
        className="relative rounded-md border border-hairline p-2 text-ink-2 transition-colors hover:bg-plane"
      >
        <BellIcon />
        {unread > 0 && (
          <span
            className="absolute -right-1 -top-1 min-w-4 rounded-full bg-status-critical px-1 text-[10px] font-semibold leading-4 text-white"
            // The badge duplicates the aria-label above, so it is decorative
            // to a screen reader rather than read out twice.
            aria-hidden="true"
          >
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Notifications"
          className="absolute right-0 z-20 mt-2 w-80 overflow-hidden rounded-lg border border-hairline bg-surface shadow-lg sm:w-96"
        >
          <div className="flex items-center justify-between border-b border-hairline px-4 py-2.5">
            <span className="text-sm font-medium text-ink">Notifications</span>
            {unread > 0 && (
              <button
                type="button"
                onClick={() => markAll.mutate()}
                disabled={markAll.isPending}
                className="text-xs text-series-import hover:underline disabled:opacity-50"
              >
                Mark all read
              </button>
            )}
          </div>

          <ul className="max-h-96 divide-y divide-hairline overflow-y-auto">
            {items.length === 0 && (
              <li className="px-4 py-8 text-center text-sm text-ink-muted">
                Nothing yet.
              </li>
            )}
            {items.map((n) => (
              <Row key={n.notification_id} n={n} onRead={markOne.mutate} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Row({
  n,
  onRead,
}: {
  n: Notification;
  onRead: (id: number) => void;
}) {
  const unread = n.read_at === null;
  const tone = SEVERITY_TONE[n.severity];
  return (
    <li className={unread ? "bg-plane/60" : undefined}>
      <div className="flex items-start gap-3 px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-ink">{n.title}</p>
            {/* Severity is a labelled pill, not a coloured dot. Status is
                never communicated by colour alone here -- see Badge in ui.tsx
                -- and `info` gets nothing at all, because a badge on every
                row would say nothing about any of them. */}
            {tone && <Badge tone={tone}>{SEVERITY_LABEL[n.severity]}</Badge>}
          </div>
          {n.body && <p className="mt-0.5 text-xs text-ink-2">{n.body}</p>}
          <p className="mt-1 text-[11px] text-ink-muted">{relative(n.created_at)}</p>
        </div>
        {unread && (
          <button
            type="button"
            onClick={() => onRead(n.notification_id)}
            className="shrink-0 text-[11px] text-series-import hover:underline"
          >
            Mark read
          </button>
        )}
      </div>
    </li>
  );
}

/**
 * Notification severity onto the shared badge scale. `info` maps to nothing:
 * it is the default, and most rows are it.
 */
const SEVERITY_TONE: Record<NotificationSeverity, string | null> = {
  info: null,
  warning: "serious",
  critical: "critical",
};

const SEVERITY_LABEL: Record<NotificationSeverity, string> = {
  info: "Info",
  warning: "Attention",
  critical: "Urgent",
};

/** "4m ago", "2d ago". Falls back to the date once it stops being useful. */
function relative(iso: string): string {
  const then = new Date(iso).getTime();
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

function BellIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
  );
}
