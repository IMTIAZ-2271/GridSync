/**
 * The horizon the signed-out pages stand on: three days of interval data,
 * import above the line and export below it.
 *
 * This replaced a literal drawing — towers, a pole, a tilted panel — which was
 * the wrong instinct twice over. Pictures of objects at low opacity read as
 * clip art, and this product is not a picture of a substation; it is the
 * measurement. So the backdrop is the measurement: a household load profile
 * with its morning and evening peaks above the line, a solar generation bell
 * below it, and the line itself is the bidirectional meter that separates them
 * (rule 6). That is the tagline, drawn — the roof is under the line, the grid
 * is over it, and the seam is the only straight edge in the composition.
 *
 * Sampled at 48 points a day because that is the interval `device_reading`
 * actually stores. Nothing here is decorative geometry pretending to be data;
 * it is the same curve `backfill_readings()` writes, at the same resolution.
 *
 * Deterministic by construction — pure functions of `t`, no RNG — so the
 * drawing is identical on every render and in every build, which is the same
 * property `setseed(0.42)` buys the seed.
 *
 * Colour follows the entity, as everywhere else: import blue above, export
 * orange below, the meter line in the chart's own axis grey.
 */

// The viewBox is wider than the page it is drawn on, and that is the lever
// that sets how tall the band renders: the SVG scales to the page width, so a
// 6:1 box lands shorter than a 4.8:1 one. Six days of a wide box is both the
// density the drawing wants and the height the composition can spare -- at
// 1440px the band is 240 tall, which keeps the peaks clear of the footer link
// instead of running through it.
const W = 1800;
const H = 300;
const BASE = 196;
// Six days rather than three. Three read as three blobs -- an ornament that
// happened to be curved; six read as a record, which is what this is.
const DAYS = 6;
const PER_DAY = 48; // the meter's reporting interval, not a drawing choice

const IMPORT_AMPLITUDE = 88;
const EXPORT_AMPLITUDE = 68;
// Above the tallest peak, so the poles read as standing behind the record
// rather than growing out of it -- and high enough that their heads reach the
// backdrop's own top fade and dissolve instead of stopping dead.
const POLE_TOP = 46;

/** Unit gaussian, the one shape both profiles are built from. */
const peak = (t: number, centre: number, width: number) =>
  Math.exp(-((t - centre) ** 2) / (2 * width * width));

/**
 * Household consumption across one day, `t` in [0, 1).
 *
 * An overnight floor, a sharp morning peak, a shallow midday shoulder and a
 * tall evening peak — the profile the simulator is specified to generate and
 * the one the seed already writes.
 */
const consumption = (t: number) =>
  0.2 + 0.42 * peak(t, 0.31, 0.055) + 0.13 * peak(t, 0.52, 0.1) + 0.92 * peak(t, 0.8, 0.075);

/**
 * Generation across one day. Clipped rather than merely small at the tails:
 * a panel at midnight produces nothing, and a curve that never quite reaches
 * the line would say otherwise.
 */
const generation = (t: number) => Math.max(0, peak(t, 0.5, 0.135) - 0.08) / 0.92;

const round = (n: number) => Math.round(n * 100) / 100;

function sample(profile: (t: number) => number): number[] {
  const out: number[] = [];
  for (let i = 0; i <= DAYS * PER_DAY; i += 1) {
    out.push(profile((i % PER_DAY) / PER_DAY));
  }
  return out;
}

/**
 * Points to a smooth path, by quadratic midpoints.
 *
 * Each sample becomes a control point and the curve passes through the
 * midpoints between them, so 144 half-hourly readings render as one continuous
 * line instead of a faceted polyline. A chart would join these differently —
 * this is the backdrop, and the honest rendering of interval data lives in
 * `ReadingsChart`.
 */
function smooth(points: [number, number][]): string {
  let d = `M${round(points[0][0])} ${round(points[0][1])}`;
  for (let i = 1; i < points.length - 1; i += 1) {
    const [cx, cy] = points[i];
    const [nx, ny] = points[i + 1];
    d += `Q${round(cx)} ${round(cy)} ${round((cx + nx) / 2)} ${round((cy + ny) / 2)}`;
  }
  const [lx, ly] = points[points.length - 1];
  return `${d}L${round(lx)} ${round(ly)}`;
}

function curve(profile: (t: number) => number, amplitude: number, sign: -1 | 1) {
  const values = sample(profile);
  const max = Math.max(...values);
  const step = W / (values.length - 1);
  const points = values.map(
    (v, i) => [i * step, BASE + sign * (v / max) * amplitude] as [number, number],
  );
  const line = smooth(points);
  return { line, area: `${line}L${W} ${BASE}L0 ${BASE}Z` };
}

const IMPORT = curve(consumption, IMPORT_AMPLITUDE, -1);
const EXPORT = curve(generation, EXPORT_AMPLITUDE, 1);

/** Six-hourly ticks, with the day boundaries drawn longer. */
const TICKS = Array.from({ length: DAYS * 4 + 1 }, (_, i) => ({
  x: (i * W) / (DAYS * 4),
  long: i % 4 === 0,
}));

export default function GridArtwork({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMax meet"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id="ga-import" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--color-series-import, #2a78d6)" stopOpacity="0.11" />
          <stop offset="100%" stopColor="var(--color-series-import, #2a78d6)" stopOpacity="0" />
        </linearGradient>
        <linearGradient id="ga-export" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stopColor="var(--color-series-export, #eb6834)" stopOpacity="0.1" />
          <stop offset="100%" stopColor="var(--color-series-export, #eb6834)" stopOpacity="0" />
        </linearGradient>
        {/* A record has no edges; a chart cropped at the viewport does. */}
        <linearGradient id="ga-edges" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="#000" />
          <stop offset="9%" stopColor="#fff" />
          <stop offset="91%" stopColor="#fff" />
          <stop offset="100%" stopColor="#000" />
        </linearGradient>
        <mask id="ga-fade">
          <rect width={W} height={H} fill="url(#ga-edges)" />
        </mask>
      </defs>

      <g mask="url(#ga-fade)">

      <path d={IMPORT.area} fill="url(#ga-import)" />
      <path d={EXPORT.area} fill="url(#ga-export)" />

      <path
        d={IMPORT.line}
        fill="none"
        stroke="var(--color-series-import, #2a78d6)"
        strokeOpacity="0.42"
        strokeWidth="1.15"
        strokeLinecap="round"
      />
      <path
        d={EXPORT.line}
        fill="none"
        stroke="var(--color-series-export, #eb6834)"
        strokeOpacity="0.36"
        strokeWidth="1.15"
        strokeLinecap="round"
      />

      {/* The meter line. The only straight edge in the drawing, because it is
          the only fixed thing in the arrangement it describes. */}
      <g stroke="var(--color-axis, #c3c2b7)" strokeLinecap="round">
        {/* Poles, standing on the day boundaries.
 
            This is the one place the drawing is allowed to be a thing rather
            than a measurement, and it earns it by being both: the pole IS the
            day tick. A line drawing of a pole *beside* the axis would be
            scenery; a pole that is load-bearing chart furniture is structure.
            Two arms and a hairline mast, nothing else -- the silhouette is
            recognisable at a glance and survives being nearly invisible,
            which a lattice tower's cross-bracing does not. */}
        {TICKS.filter((t) => t.long).map((tick) => (
          <g key={`pole-${tick.x}`} strokeWidth="1" strokeOpacity="0.34">
            <path d={`M${round(tick.x)} ${BASE}V${POLE_TOP}`} />
            <path d={`M${round(tick.x) - 28} ${POLE_TOP + 15}h56`} />
            <path d={`M${round(tick.x) - 19} ${POLE_TOP + 33}h38`} />
          </g>
        ))}
        <path d={`M0 ${BASE}H${W}`} strokeWidth="1" strokeOpacity="0.85" />
        {TICKS.map((tick) => (
          <path
            key={tick.x}
            d={`M${round(tick.x)} ${BASE}v${tick.long ? 10 : 4}`}
            strokeWidth="1"
            strokeOpacity={tick.long ? 0.8 : 0.45}
          />
        ))}
        </g>
      </g>
    </svg>
  );
}

/**
 * A solar module, drawn as a module: cells on a strict grid, sheared once.
 *
 * The tilt is the only gesture — a single skew, the same on every cell, which
 * is what a panel actually is when you look at one from the side of a roof.
 * Everything else is orthogonal and evenly divided, because the thing being
 * evoked is a manufactured object with a repeat, not a landscape. No frame
 * legs, no ground, no sun: those are what turned the first attempt into clip
 * art, and none of them is the panel.
 *
 * It fills the upper field the composition had left empty and balances the
 * record along the bottom. Export orange, because a panel is what export is
 * measured from, and at an alpha where it is texture rather than an object.
 */
export function PanelLattice({ className = "" }: { className?: string }) {
  const COLS = 8;
  const ROWS = 5;
  const CELL_W = 62;
  const CELL_H = 44;
  const SHEAR = 15; // px of lean per row, applied cumulatively upward
  const w = COLS * CELL_W;
  const h = ROWS * CELL_H;

  // x offset for a given y: the whole block leans, so verticals stay parallel.
  const lean = (y: number) => ((h - y) / h) * SHEAR * ROWS;

  const verticals = Array.from({ length: COLS + 1 }, (_, i) => i * CELL_W);
  const horizontals = Array.from({ length: ROWS + 1 }, (_, i) => i * CELL_H);
  const fade = "linear-gradient(115deg, #000 8%, transparent 82%)";

  return (
    <div className={className} style={{ maskImage: fade, WebkitMaskImage: fade }}>
      <svg
        viewBox={`0 0 ${w + SHEAR * ROWS} ${h}`}
        className="h-auto w-full"
        aria-hidden="true"
        focusable="false"
      >
        <g
          fill="none"
          stroke="var(--color-series-export, #eb6834)"
          strokeWidth="1"
          strokeOpacity="0.5"
          strokeLinecap="square"
        >
          {verticals.map((x) => (
            <path key={`v${x}`} d={`M${x + lean(h)} ${h}L${x + lean(0)} 0`} />
          ))}
          {horizontals.map((y) => (
            <path key={`h${y}`} d={`M${lean(y)} ${y}h${w}`} />
          ))}
        </g>
      </svg>
    </div>
  );
}
