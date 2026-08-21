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
