/**
 * The GridSync mark: a metering panel with a bolt through it.
 *
 * Drawn rather than imported, for the same reason the palette lives in
 * index.css -- an inline SVG inherits the theme tokens, so the mark cannot
 * drift from the app's own blue when someone retunes it, and there is no
 * asset to lose. It carries a `var(..., #hex)` fallback so it still renders
 * in a context that never loaded the stylesheet.
 *
 * `aria-hidden` throughout: the wordmark beside it is the accessible name, and
 * a screen reader announcing "GridSync GridSync" is worse than silence.
 */
export default function Logo({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <rect
        width="32"
        height="32"
        rx="8.5"
        fill="var(--color-series-import, #2a78d6)"
      />
      {/* Panel mullions, offset off centre. The bolt is centred on both axes,
          so lines through the middle survive only as stubs in the corners --
          these divide the square into thirds and keep a real segment showing
          on every side of it. */}
      <g stroke="#ffffff" strokeOpacity="0.26" strokeWidth="1.2" strokeLinecap="round">
        <path d="M11.5 4.2V27.8" />
        <path d="M20.5 4.2V27.8" />
        <path d="M4.2 11.5H27.8" />
        <path d="M4.2 20.5H27.8" />
      </g>
      <path
        d="M18.4 5.2 L9.1 18.3 H14.7 L13.1 26.8 L22.9 13.4 H17.4 Z"
        fill="#ffffff"
      />
    </svg>
  );
}

/**
 * The tagline, in one place so the sign-in page and the registration page
 * cannot end up making two different promises.
 *
 * It names the boundary the whole system is built on. A household here has
 * panels on one side and the utility's network on the other, and rule 6 puts
 * the one instrument that can tell import from export exactly between them --
 * so the meter at that seam is what every bill, every credit entry and every
 * reading in the database hangs off. The backdrop draws the same sentence.
 */
export const TAGLINE = "Where the roof meets the grid";

/** Mark plus wordmark, for the places that introduce the app rather than navigate it. */
export function Wordmark({
  size = "md",
  className = "",
}: {
  size?: "md" | "lg";
  className?: string;
}) {
  const mark = size === "lg" ? "h-12 w-12" : "h-8 w-8";
  const text = size === "lg" ? "text-4xl" : "text-lg";
  return (
    <span
      className={`inline-flex items-center ${size === "lg" ? "gap-4" : "gap-3"} ${className}`}
    >
      <Logo className={mark} />
      <span className={`${text} font-semibold tracking-[-0.022em] text-ink`}>
        GridSync
      </span>
    </span>
  );
}

/**
 * Wordmark over tagline -- the signed-out lockup.
 *
 * The tagline is set as a letterspaced micro-caps line rather than a sentence.
 * At a sixth of an em it reads as a mark's strapline instead of as the first
 * line of body copy, which is the difference between a brand and a paragraph
 * someone forgot to finish. `text-ink-2` rather than `ink-muted`: the muted
 * grey is ~3:1 on this surface, and this is text.
 *
 * 14px at both sizes -- the floor for the signed-out pages. The lockup scales
 * through the mark and the wordmark instead, which is the honest lever: a
 * strapline shrinking with its logo is what makes it unreadable at the size
 * it is most often seen.
 */
export function BrandLockup({
  size = "md",
  align = "left",
  className = "",
}: {
  size?: "md" | "lg";
  align?: "left" | "center";
  className?: string;
}) {
  const lg = size === "lg";
  return (
    <div
      className={`flex flex-col ${
        align === "center" ? "items-center text-center" : "items-start"
      } ${className}`}
    >
      <Wordmark size={size} />
      <p
        className={`${lg ? "mt-6" : "mt-3.5"} text-[14px] font-medium uppercase tracking-[0.16em] text-ink-2`}
      >
        {TAGLINE}
      </p>
    </div>
  );
}
