import type { ReactNode } from "react";

export const FIELD =
  "w-full rounded-md border border-hairline bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-series-import focus:ring-2 focus:ring-series-import/25";

/** Centred card for the signed-out pages. No portal chrome — there is no portal yet. */
export function AuthShell({
  title,
  subtitle,
  children,
  footer,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="flex min-h-full flex-col bg-plane">
      <header className="border-b border-hairline bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-3">
          <span className="text-lg font-semibold tracking-tight text-ink">GridSync</span>
        </div>
      </header>

      <main className="flex flex-1 items-start justify-center px-6 py-12">
        <div className={wide ? "w-full max-w-2xl" : "w-full max-w-md"}>
          <div className="rounded-xl border border-hairline bg-surface p-7">
            <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
            {subtitle && <p className="mt-1.5 text-sm text-ink-2">{subtitle}</p>}
            <div className="mt-6">{children}</div>
          </div>
          {footer && (
            <p className="mt-4 text-center text-sm text-ink-2">{footer}</p>
          )}
        </div>
      </main>
    </div>
  );
}

export function SubmitButton({
  busy,
  busyLabel,
  children,
  disabled,
}: {
  busy: boolean;
  busyLabel: string;
  children: ReactNode;
  disabled?: boolean;
}) {
  return (
    <button
      type="submit"
      disabled={busy || disabled}
      className="w-full rounded-md bg-series-import px-4 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
    >
      {busy ? busyLabel : children}
    </button>
  );
}
