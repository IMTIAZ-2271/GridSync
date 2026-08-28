import { useQuery } from "@tanstack/react-query";

import { countUnread, useViewWatermarks, type ViewKey } from "../lib/unread";
import { UNREAD_SOURCES } from "../lib/unreadSources";

/**
 * The red indicator beside a nav item, and the count behind it.
 *
 * Renders nothing at all when there is nothing new -- an always-present badge
 * showing 0 is furniture people stop reading, and the whole value of this mark
 * is that its presence means something.
 *
 * The number is not decoration either: "3 new" tells you whether to open the
 * page now, where a bare dot only tells you something happened. It is capped
 * at 9+ because past that the exact figure stops changing the decision.
 */
export default function UnreadDot({ viewKey }: { viewKey: ViewKey }) {
  const source = UNREAD_SOURCES[viewKey];
  const { watermarks } = useViewWatermarks();

  // Always called -- hooks cannot be conditional -- but disabled when this
  // view has no list behind it, so it costs nothing for the site-scoped pages
  // that deliberately have no source.
  const list = useQuery({
    queryKey: source?.queryKey ?? ["unread-noop", viewKey],
    queryFn: source ? (source.queryFn as () => Promise<unknown[]>) : async () => [],
    enabled: Boolean(source),
    staleTime: 30_000,
  });

  if (!source) return null;

  const count = countUnread(
    list.data as readonly never[] | undefined,
    source.timestampOf,
    watermarks[viewKey] ?? null,
  );
  if (count === 0) return null;

  return (
    <span
      // The count is in the accessible name, not just the glyph: a screen
      // reader announcing "3 new" is the same information a sighted reader
      // gets from the badge, and this app's rule is that status never travels
      // by colour alone.
      role="status"
      aria-label={`${count} new`}
      className="ml-1.5 inline-flex min-w-[1.15rem] items-center justify-center rounded-full bg-status-critical px-1 text-[0.6875rem] font-semibold leading-[1.15rem] text-white"
    >
      {count > 9 ? "9+" : count}
    </span>
  );
}
