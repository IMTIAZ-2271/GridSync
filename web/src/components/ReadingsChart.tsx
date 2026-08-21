import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { CHART_INK, SERIES, type SeriesId } from "../lib/series";
import { toNumber, type Reading } from "../lib/api";

interface Point {
  t: number;
  import_kwh: number;
  export_kwh: number;
  generation_kwh: number;
}

/**
 * Interval energy over the selected window.
 *
 * Readings arrive as exact decimal strings and are parsed to numbers only
 * here, at the plotting boundary -- a pixel position cannot be a decimal
 * string, and nothing downstream of this treats the parsed value as money.
 *
 * The 30-minute intervals are plotted raw rather than rolled up to daily
 * totals. The daily solar arc is the shape that makes a net-metering site
 * legible at a glance -- generation climbing over midday, export tracking it
 * with the household's own draw subtracted out -- and averaging flattens
 * exactly that.
 */
export default function ReadingsChart({
  readings,
  series,
}: {
  readings: Reading[];
  /** Which measures to draw. A site with no panels omits generation. */
  series: SeriesId[];
}) {
  const data: Point[] = readings.map((r) => ({
    t: new Date(r.interval_start).getTime(),
    import_kwh: toNumber(r.import_kwh),
    export_kwh: toNumber(r.export_kwh),
    generation_kwh: toNumber(r.generation_kwh),
  }));

  // One tick per midnight, so the axis reads as days rather than as an
  // arbitrary sampling of timestamps.
  const dayTicks = data
    .filter((p) => {
      const d = new Date(p.t);
      return d.getHours() === 0 && d.getMinutes() === 0;
    })
    .map((p) => p.t);

  return (
    <div className="w-full">
      <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={data}
          margin={{ top: 8, right: 16, bottom: 8, left: 0 }}
        >
          {/* Horizontal only, hairline, solid -- recessive by design. */}
          <CartesianGrid
            stroke={CHART_INK.grid}
            strokeWidth={1}
            vertical={false}
          />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            ticks={dayTicks}
            tickFormatter={(t: number) =>
              new Date(t).toLocaleDateString(undefined, {
                day: "numeric",
                month: "short",
              })
            }
            tick={{ fill: CHART_INK.muted, fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: CHART_INK.axis }}
            minTickGap={0}
          />
          <YAxis
            width={56}
            tick={{ fill: CHART_INK.muted, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v: number) => v.toFixed(1)}
            label={{
              value: "kWh / 30 min",
              angle: -90,
              position: "insideLeft",
              style: { fill: CHART_INK.muted, fontSize: 11 },
            }}
          />

          <Tooltip
            // The crosshair finds the X; the reader aims at a time, never at a
            // 2px line, and gets every series at that instant in one readout.
            cursor={{ stroke: CHART_INK.axis, strokeWidth: 1 }}
            content={<ReadingTooltip series={series} />}
          />

          {series.map((id) => (
            <Line
              key={id}
              type="monotone"
              dataKey={SERIES[id].key}
              name={SERIES[id].label}
              stroke={SERIES[id].hex}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              // 336 points per series: a dot on each would be a solid band.
              dot={false}
              activeDot={{
                r: 4,
                strokeWidth: 2,
                stroke: CHART_INK.surface,
                fill: SERIES[id].hex,
              }}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
      </div>

      {/* Legend + totals. This is the labelled relief for generation's aqua,
          which sits under 3:1 on this surface. */}
      <SeriesKeyStrip data={data} series={series} />
    </div>
  );
}

/**
 * Legend and window totals, under the plot.
 *
 * This is the labelled relief that lets generation's sub-3:1 aqua be used at
 * all -- every series is named in ink beside its own colour key, so nothing is
 * identified by hue alone.
 *
 * It carries each series' total over the window rather than its value at the
 * final point. The last interval is whatever half hour the window happened to
 * end on, and overnight that is 0.000 for both export and generation -- a
 * true number that tells the reader nothing. The total is the figure that
 * describes the week.
 */
function SeriesKeyStrip({
  data,
  series,
}: {
  data: Point[];
  series: SeriesId[];
}) {
  if (!data.length) return null;

  const total = (key: keyof Point) =>
    data.reduce((sum, p) => sum + p[key], 0);

  return (
    <div className="mt-4 flex flex-wrap justify-end gap-x-6 gap-y-2 border-t border-hairline pt-3">
      {series.map((id) => (
        <span key={id} className="inline-flex items-baseline gap-2 text-xs">
          <span
            aria-hidden
            className="h-0.5 w-4 self-center rounded-full"
            style={{ backgroundColor: SERIES[id].hex }}
          />
          <span className="text-ink-2">{SERIES[id].label}</span>
          <span className="tabular font-semibold text-ink">
            {total(SERIES[id].key as keyof Point).toFixed(1)}
          </span>
          <span className="text-ink-muted">kWh</span>
        </span>
      ))}
    </div>
  );
}

interface TooltipProps {
  active?: boolean;
  label?: number;
  series: SeriesId[];
  payload?: { dataKey: string; value: number }[];
}

/**
 * One tooltip listing every series at the hovered instant.
 *
 * Values lead and are the high-contrast element; the series name is secondary.
 * The reader already knows which series they care about -- they want the
 * number.
 */
function ReadingTooltip({ active, label, payload, series }: TooltipProps) {
  if (!active || !payload?.length || label == null) return null;

  const byKey = new Map(payload.map((p) => [p.dataKey, p.value]));

  return (
    <div className="rounded-lg border border-hairline bg-surface px-3 py-2 shadow-md">
      <p className="text-xs text-ink-muted">
        {new Date(label).toLocaleString(undefined, {
          day: "numeric",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
        })}
      </p>
      <ul className="mt-1.5 space-y-1">
        {series.map((id) => (
          <li key={id} className="flex items-center gap-2 text-xs">
            <span
              aria-hidden
              className="h-0.5 w-3 rounded-full"
              style={{ backgroundColor: SERIES[id].hex }}
            />
            <span className="tabular font-semibold text-ink">
              {(byKey.get(SERIES[id].key) ?? 0).toFixed(3)}
            </span>
            <span className="text-ink-muted">{SERIES[id].label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
