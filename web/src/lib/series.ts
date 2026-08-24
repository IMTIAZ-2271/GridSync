/**
 * The three measures, and the colour each one owns everywhere in the app.
 *
 * Categorical slots 1-3, validated as a set against the #fcfcfb surface. The
 * mapping is fixed by entity, not by rank or by how many series a given view
 * happens to show -- hiding generation on a non-solar site must never repaint
 * import or export.
 */
export const SERIES = {
  import: {
    key: "import_kwh",
    label: "Import",
    color: "var(--color-series-import)",
    hex: "#2a78d6",
    hint: "Drawn from the grid",
  },
  export: {
    key: "export_kwh",
    label: "Export",
    color: "var(--color-series-export)",
    hex: "#eb6834",
    hint: "Sent to the grid",
  },
  generation: {
    key: "generation_kwh",
    label: "Generation",
    color: "var(--color-series-generation)",
    hex: "#1baf7a",
    hint: "Produced by the panels",
  },
} as const;

export type SeriesId = keyof typeof SERIES;

export const CHART_INK = {
  grid: "#e1e0d9",
  axis: "#c3c2b7",
  muted: "#898781",
  secondary: "#52514e",
  surface: "#fcfcfb",
};

/**
 * The two views a household reads its own meter through.
 *
 * Consumer requirements 4 and 5 ask for solar readings to have their own
 * interface rather than being a third line on one chart. The split is real, not
 * cosmetic: the two views answer different questions and are measured by
 * different hardware (rule 6 -- only the bidirectional meter knows the
 * import/export split, only the inverter knows generation). Reading them
 * together is what makes a household stare at three overlapping lines and
 * conclude nothing.
 *
 * Export sits on the SOLAR side, not the consumption side. It is what the
 * panels sent out; a house with no panels has none, and putting it beside
 * import would imply the two are a pair to be compared.
 */
export const READING_VIEWS = [
  {
    id: "consumption" as const,
    label: "Consumption",
    series: ["import"] as SeriesId[],
    blurb: "Energy drawn from the grid",
  },
  {
    id: "solar" as const,
    label: "Solar",
    series: ["generation", "export"] as SeriesId[],
    blurb: "Produced by the panels, and what was sent back",
  },
];

export type ReadingViewId = (typeof READING_VIEWS)[number]["id"];
