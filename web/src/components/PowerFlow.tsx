import { SERIES } from "../lib/series";
import { type Reading, subtractDecimals, sumDecimals } from "../lib/api";

/**
 * Where the household's energy went: panels, house, grid.
 *
 * **A diagram, not a chart.** There are three entities and two directions, and
 * the question is "where did it go" rather than "how much" or "when" -- so the
 * form is a flow, and the magnitudes ride on it as labels. A bar chart of
 * import/export/generation answers a different question, and the page already
 * has one below.
 *
 * Colour follows the entity exactly as everywhere else in the app: import blue,
 * export orange, generation aqua. Every flow is ALSO named in text beside its
 * value, so identity never rests on colour -- which is what makes the aqua
 * legal here at 2.74:1 against this surface.
 *
 * The arithmetic is rule 6, and it is the whole reason this panel can exist:
 *
 *     self-consumption = generation - export
 *
 * An inverter reports only what it made; only the meter at the grid boundary
 * knows the split. So what the house drew is `import + (generation - export)`,
 * and none of it is measured directly -- which is exactly why it is worth
 * drawing.
 *
 * **That equation only holds once the connection is net-metered.** Before the
 * swap the meter is unidirectional: it reports the grid draw and cannot see
 * export at all, so `generation - export` would silently claim the household
 * used every kilowatt-hour its panels made -- including the surplus that was
 * spilled. `netMetered` is what tells the two cases apart. When it is false
 * the panels' output is shown as a figure but no quantity is claimed to have
 * reached the house, and the diagram says so: it is the clearest statement of
 * what the swap actually buys.
 */

/** Values are NUMERIC strings end to end; see rule 5. */
export interface FlowTotals {
  generation: string;
  imported: string;
  exported: string;
  selfUse: string;
  homeTotal: string;
}

export function flowTotals(readings: Reading[]): FlowTotals {
  const generation = sumDecimals(readings.map((r) => r.generation_kwh));
  const imported = sumDecimals(readings.map((r) => r.import_kwh));
  const exported = sumDecimals(readings.map((r) => r.export_kwh));
  // Exact decimal arithmetic, not floats: these are displayed figures, and
  // `subtractDecimals` is what keeps them from turning into 3.8464999999993.
  const selfUse = subtractDecimals(generation, exported);
  return {
    generation,
    imported,
    exported,
    selfUse,
    homeTotal: sumDecimals([imported, selfUse]),
  };
}

const n = (v: string) => Number(v);

function fmt(v: string): string {
  const x = n(v);
  return x >= 100 ? x.toFixed(0) : x.toFixed(1);
}

export default function PowerFlow({
  totals,
  hasSolar,
  netMetered,
  caption,
}: {
  totals: FlowTotals;
  /** The household has panels at all. */
  hasSolar: boolean;
  /** Those panels sit behind a bidirectional meter, so the split is measured. */
  netMetered: boolean;
  caption: string;
}) {
  const { generation, imported, exported } = totals;

  // Only a net-metered connection can say how much of the generation reached
  // the house. Without it, what the meter knows is the import -- and claiming
  // more would be inventing a measurement.
  const selfUse = netMetered ? totals.selfUse : null;
  const homeTotal = netMetered ? totals.homeTotal : imported;

  // A flow with nothing in it is drawn faint rather than hidden. A missing
  // limb reads as a broken diagram; a pale one reads as "nothing went this
  // way", which is the fact.
  const live = (v: string) => (n(v) > 0 ? 1 : 0.22);

  // Net across the grid boundary: positive when the household supplied more
  // than it took. Exact decimal arithmetic, because it is displayed.
  const net = subtractDecimals(exported, imported);
  const netExporting = n(net) >= 0;
  const netAbs = netExporting ? net : subtractDecimals(imported, exported);

  // The no-solar case crops to the bottom band rather than drawing an empty
  // top half. A diagram with a hole where the panels would be reads as a
  // missing component, not as "you have no panels".
  const viewBox = hasSolar ? "0 0 720 258" : "0 150 720 108";

  return (
    <figure className="m-0">
      <svg
        viewBox={viewBox}
        className="w-full"
        role="img"
        aria-label={
          `Energy flow over ${caption}. ` +
          (hasSolar && netMetered && selfUse
            ? `Panels generated ${fmt(generation)} kWh, of which ` +
              `${fmt(selfUse)} kWh was used in the home and ` +
              `${fmt(exported)} kWh was exported to the grid. `
            : hasSolar
              ? `Panels generated ${fmt(generation)} kWh. This connection is ` +
                "not net-metered, so how much of it the home used is not measured. "
              : "") +
          `The home drew ${fmt(imported)} kWh from the grid, using ` +
          `${fmt(homeTotal)} kWh in total.`
        }
      >
        <defs>
          {/* One marker per hue: an arrowhead inherits nothing useful from
              the path it terminates, so each has to be coloured explicitly. */}
          {(
            [
              ["gen", SERIES.generation.hex],
              ["exp", SERIES.export.hex],
              ["imp", SERIES.import.hex],
            ] as const
          ).map(([id, hex]) => (
            <marker
              key={id}
              id={`flow-${id}`}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 9 5 L 0 9 z" fill={hex} />
            </marker>
          ))}
        </defs>

        {hasSolar && (
          <>
            {/* Panels -> home and panels -> grid leave the same stem and turn
                down into the top of their node, so the two branches read as
                one quantity being split -- which is what generation is. */}
            <path
              d="M 360 74 L 360 108 Q 360 120 372 120 L 603 120 Q 615 120 615 132 L 615 162"
              fill="none"
              stroke={SERIES.generation.hex}
              strokeWidth="2"
              strokeLinecap="round"
              // Dashed when the quantity is unknown rather than zero. A solid
              // line with no number beside it would read as a measured flow
              // whose label was forgotten.
              strokeDasharray={netMetered ? undefined : "5 4"}
              markerEnd="url(#flow-gen)"
              opacity={netMetered ? live(selfUse ?? "0") : 0.5}
            />
            {netMetered && (
              <path
                d="M 360 74 L 360 108 Q 360 120 348 120 L 117 120 Q 105 120 105 132 L 105 162"
                fill="none"
                stroke={SERIES.export.hex}
                strokeWidth="2"
                strokeLinecap="round"
                markerEnd="url(#flow-exp)"
                opacity={live(exported)}
              />
            )}
          </>
        )}
        {/* Grid -> home. Always drawn: the grid connection is the one thing a
            household never lacks. */}
        <path
          d="M 205 206 L 512 206"
          fill="none"
          stroke={SERIES.import.hex}
          strokeWidth="2"
          strokeLinecap="round"
          markerEnd="url(#flow-imp)"
          opacity={live(imported)}
        />

        {hasSolar && (
          <Node
            x={360}
            y={42}
            title="Panels"
            value={`${fmt(generation)} kWh`}
            hint="generated"
          />
        )}
        <Node
          x={105}
          y={206}
          title="Grid"
          value={`${fmt(netMetered ? netAbs : imported)} kWh`}
          hint={
            !netMetered ? "supplied" : netExporting ? "net supplied" : "net taken"
          }
        />
        <Node
          x={615}
          y={206}
          title="Home"
          value={`${fmt(homeTotal)} kWh`}
          hint="used"
        />

        {/* Labels sit ABOVE their lines, never across them. Every flow is
            named in words as well as figures: that is the secondary encoding
            the palette check requires, and it is how a reader tells self-use
            from export without consulting a key. */}
        {hasSolar && netMetered && selfUse !== null && (
          <>
            <FlowLabel
              x={430}
              y={104}
              text="used at home"
              value={`${fmt(selfUse)} kWh`}
            />
            <FlowLabel
              x={290}
              y={104}
              text="exported"
              value={`${fmt(exported)} kWh`}
              anchor="end"
            />
          </>
        )}
        {hasSolar && !netMetered && (
          <FlowLabel
            x={430}
            y={104}
            text="your meter cannot measure this yet"
            value="not measured"
          />
        )}
        <FlowLabel
          x={358}
          y={172}
          text="imported"
          value={`${fmt(imported)} kWh`}
          anchor="middle"
        />
      </svg>
      <figcaption className="mt-1 text-xs text-ink-muted">{caption}</figcaption>
    </figure>
  );
}

function Node({
  x,
  y,
  title,
  value,
  hint,
}: {
  x: number;
  y: number;
  title: string;
  value: string;
  hint: string;
}) {
  const w = 190;
  const h = 76;
  return (
    <g transform={`translate(${x - w / 2} ${y - h / 2})`}>
      <rect
        width={w}
        height={h}
        rx="12"
        fill="var(--color-surface)"
        stroke="var(--color-hairline)"
      />
      <text
        x={w / 2}
        y="26"
        textAnchor="middle"
        className="fill-ink-muted"
        style={{ fontSize: 12, letterSpacing: "0.06em", textTransform: "uppercase" }}
      >
        {title}
      </text>
      <text
        x={w / 2}
        y="50"
        textAnchor="middle"
        className="fill-ink"
        style={{ fontSize: 20, fontWeight: 600 }}
      >
        {value}
      </text>
      <text
        x={w / 2}
        y="66"
        textAnchor="middle"
        className="fill-ink-muted"
        style={{ fontSize: 11 }}
      >
        {hint}
      </text>
    </g>
  );
}

function FlowLabel({
  x,
  y,
  text,
  value,
  anchor = "start",
}: {
  x: number;
  y: number;
  text: string;
  value: string;
  anchor?: "start" | "middle" | "end";
}) {
  // Caption above, value below, and the pair sits clear of the line rather
  // than across it -- the first render had the stroke running through the
  // words.
  return (
    <g>
      <text
        x={x}
        y={y}
        textAnchor={anchor}
        className="fill-ink-muted"
        style={{ fontSize: 11 }}
      >
        {text}
      </text>
      <text
        x={x}
        y={y + 15}
        textAnchor={anchor}
        className="fill-ink-2"
        style={{ fontSize: 13, fontWeight: 600 }}
      >
        {value}
      </text>
    </g>
  );
}
