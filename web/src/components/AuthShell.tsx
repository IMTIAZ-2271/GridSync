import type { ReactNode } from "react";

import GridArtwork, { PanelLattice } from "./GridArtwork";
import { BrandLockup } from "./Logo";

/**
 * Field label. Micro-caps and letterspaced, matching the tagline's treatment,
 * so the whole signed-out surface is set in one voice rather than two.
 *
 * 14px is the floor for every piece of text on the signed-out pages, and this
 * is the one that had been breaking it at 11. Micro-caps at 14 need less
 * letterspacing than at 11 to read as the same device, hence the tighter
 * track -- the effect is a function of size, not a constant.
 */
export const LABEL =
  "text-[14px] font-medium uppercase tracking-[0.07em] text-ink-2";

export const FIELD =
  "w-full rounded-lg border border-hairline bg-surface px-3.5 py-2.5 text-[16px] text-ink outline-none transition-colors placeholder:text-ink-muted focus:border-series-import focus:ring-4 focus:ring-series-import/12";

/**
 * The signed-out backdrop: graph paper under two soft washes, standing on the
 * network the app bills for.
 *
 * All of it is the app's own palette rather than decoration bought in from
 * somewhere else -- the ruled grid is the chart surface every portal page
 * carries, and the two washes are the import blue and export orange that the
 * whole product is about. They sit at a tenth of their strength, so the page
 * reads as paper with light on it rather than as a coloured page.
 *
 * The grid is masked away from the edges. A ruled surface running under the
 * card and off the viewport looks like a screenshot of something larger; one
 * that fades out looks like a sheet.
 *
 * `GridArtwork` sits on the bottom edge, faded out upward so it emerges from
 * the paper rather than being pasted onto it. It scales rather than crops, so
 * it survives a phone as a slim band -- which the literal drawing it replaced
 * did not, and that was the first sign the literal drawing was wrong.
 */
function Backdrop() {
  // Centred high on purpose: the artwork owns the lower band, and two ruled
  // things competing over the same inch is what makes a backdrop look busy.
  const fade =
    "radial-gradient(ellipse 68% 52% at 50% 30%, #000 22%, transparent 78%)";
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      <div
        className="absolute inset-0 opacity-45"
        style={{
          backgroundImage:
            "linear-gradient(to right, var(--color-hairline) 1px, transparent 1px)," +
            "linear-gradient(to bottom, var(--color-hairline) 1px, transparent 1px)",
          backgroundSize: "72px 72px",
          maskImage: fade,
          WebkitMaskImage: fade,
        }}
      />
      <div
        className="absolute -right-40 -top-48 h-[40rem] w-[40rem] rounded-full"
        style={{
          background:
            "radial-gradient(circle, rgba(235, 104, 52, 0.14), transparent 68%)",
        }}
      />
      <div
        className="absolute -bottom-64 -left-48 h-[38rem] w-[38rem] rounded-full"
        style={{
          background:
            "radial-gradient(circle, rgba(42, 120, 214, 0.13), transparent 68%)",
        }}
      />
      {/* Upper field: the module. Placed opposite the record rather than beside
          it, so the two motifs balance the composition instead of stacking. */}
      <PanelLattice className="absolute left-20 top-20 hidden w-[25rem] opacity-90 lg:block" />
      <div
        className="absolute inset-x-0 bottom-0"
        style={{
          maskImage: "linear-gradient(to top, #000 74%, transparent 100%)",
          WebkitMaskImage: "linear-gradient(to top, #000 74%, transparent 100%)",
        }}
      >
        <GridArtwork className="h-auto w-full" />
      </div>
    </div>
  );
}

/**
 * Centred card for the signed-out pages. No portal chrome — there is no portal
 * yet, and the top bar this used to carry was one more rule across a page
 * whose whole job is a five-field form.
 *
 * `aside` is a brand column shown beside the card from `lg` up; below that
 * width it is dropped rather than stacked, and the compact lockup above the
 * card carries the identity on its own. A phone opening a sign-in page does
 * not want to scroll past a pitch to reach the password field.
 */
export function AuthShell({
  title,
  subtitle,
  children,
  footer,
  wide = false,
  aside,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  aside?: ReactNode;
}) {
  const frame = aside
    ? "grid w-full max-w-5xl gap-x-20 gap-y-12 lg:grid-cols-[1fr_minmax(0,25rem)] lg:items-center"
    : wide
      ? "w-full max-w-2xl"
      : "w-full max-w-md";

  return (
    <div className="relative flex min-h-full flex-col overflow-hidden bg-plane">
      <Backdrop />

      <main className="relative flex flex-1 items-center justify-center px-6 py-12 sm:py-16">
        <div className={frame}>
          {aside && <div className="hidden lg:block">{aside}</div>}

          <div className="w-full">
            <BrandLockup
              align={aside ? "left" : "center"}
              className={aside ? "mb-9 lg:hidden" : "mb-9"}
            />

            {/* The white hairline along the top edge is what separates a card
                from a rectangle: it reads as the sheet catching the light the
                two washes behind it are casting. */}
            <div
              className="rounded-2xl border border-hairline bg-surface/80 p-8 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_1px_2px_rgba(11,11,11,0.04),0_22px_50px_-22px_rgba(11,11,11,0.19)] backdrop-blur-xl sm:p-9"
              style={{
                backgroundImage:
                  "linear-gradient(180deg, rgba(255,255,255,0.55), rgba(255,255,255,0.10))",
              }}
            >
              <h1 className="text-[18px] font-semibold leading-tight tracking-[-0.01em] text-ink">
                {title}
              </h1>
              {subtitle && <p className="mt-2.5 text-[15px] leading-relaxed text-ink-2">{subtitle}</p>}
              <div className="mt-8">{children}</div>
            </div>

            {footer && <p className="mt-6 text-center text-[15px] text-ink-2">{footer}</p>}
          </div>
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
      // The lift is cast in the button's own hue rather than in grey, so it
      // reads as the blue sitting above the page instead of as a drop shadow
      // borrowed from somewhere else. It is dropped on :disabled -- a control
      // that cannot be pressed should not look like it is floating.
      className="w-full rounded-lg bg-series-import px-4 py-3 text-[16px] font-medium tracking-[0.01em] text-white shadow-[0_1px_2px_rgba(42,120,214,0.28),0_10px_22px_-10px_rgba(42,120,214,0.62)] transition-all hover:brightness-[1.07] focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-series-import/25 disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none"
    >
      {busy ? busyLabel : children}
    </button>
  );
}
