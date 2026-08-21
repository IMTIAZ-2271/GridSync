import type { ReactNode } from "react";

/** Hairline-ringed panel. The one container shape used across the app. */
export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-hairline bg-surface ${className}`}
    >
      {children}
    </section>
  );
}

export function CardHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-hairline px-5 py-4">
      <div>
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {subtitle && <p className="mt-0.5 text-xs text-ink-muted">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

/**
 * Stat tile.
 *
 * Label in sentence case, value proportional (not tabular -- a standalone
 * figure set in tabular digits reads loose at this size), optional footnote.
 */
export function Stat({
  label,
  value,
  unit,
  footnote,
  accent,
}: {
  label: string;
  value: string;
  unit?: string;
  footnote?: ReactNode;
  /** Optional colour key dot; identity rides the dot, never the text. */
  accent?: string;
}) {
  return (
    <Card className="px-5 py-4">
      <div className="flex items-center gap-2">
        {accent && (
          <span
            aria-hidden
            className="size-2 shrink-0 rounded-full"
            style={{ backgroundColor: accent }}
          />
        )}
        <p className="text-xs font-medium text-ink-2">{label}</p>
      </div>
      <p className="mt-2 flex items-baseline gap-1.5">
        <span className="text-3xl font-semibold tracking-tight text-ink">
          {value}
        </span>
        {unit && <span className="text-sm text-ink-muted">{unit}</span>}
      </p>
      {footnote && <div className="mt-2 text-xs text-ink-muted">{footnote}</div>}
    </Card>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden />;
}

export function StatSkeleton() {
  return (
    <Card className="px-5 py-4">
      <Skeleton className="h-3 w-24" />
      <Skeleton className="mt-3 h-8 w-32" />
      <Skeleton className="mt-3 h-3 w-40" />
    </Card>
  );
}

/** Shown when a request succeeded and there is genuinely nothing to show. */
export function EmptyState({
  title,
  hint,
  icon = "○",
}: {
  title: string;
  hint?: string;
  icon?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      <span aria-hidden className="text-2xl text-axis">
        {icon}
      </span>
      <p className="mt-3 text-sm font-medium text-ink-2">{title}</p>
      {hint && <p className="mt-1 max-w-sm text-xs text-ink-muted">{hint}</p>}
    </div>
  );
}

/**
 * Failure state. Distinct from empty on purpose -- "no bills yet" and "we
 * could not reach the server" must never look the same to the reader.
 */
export function ErrorState({ error }: { error: Error }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      <span aria-hidden className="text-2xl text-status-critical">
        !
      </span>
      <p className="mt-3 text-sm font-medium text-ink-2">Could not load this</p>
      <p className="mt-1 max-w-md text-xs text-ink-muted">{error.message}</p>
    </div>
  );
}

const BADGE_TONES: Record<string, string> = {
  neutral: "bg-hairline text-ink-2",
  good: "bg-status-good/12 text-status-good-text",
  warning: "bg-status-warning/20 text-ink-2",
  serious: "bg-status-serious/20 text-ink-2",
  critical: "bg-status-critical/12 text-status-critical",
};

/**
 * Status pill.
 *
 * Always carries its own text -- status is never communicated by colour alone,
 * which matters most for `warning`, whose amber is sub-3:1 on this surface.
 */
export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: keyof typeof BADGE_TONES | string;
}) {
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ${
        BADGE_TONES[tone] ?? BADGE_TONES.neutral
      }`}
    >
      {children}
    </span>
  );
}

/** Small colour key beside a label. Text stays in ink; the dot carries hue. */
export function SeriesKey({
  color,
  label,
}: {
  color: string;
  label: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-2">
      <span
        aria-hidden
        className="h-0.5 w-3 rounded-full"
        style={{ backgroundColor: color }}
      />
      {label}
    </span>
  );
}
