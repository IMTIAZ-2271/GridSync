/**
 * Headless render check.
 *
 * Renders the real customer pages to a string against the live API, so a crash
 * in component logic or a bad field name shows up here instead of as a blank
 * screen in the demo. Recharts' ResponsiveContainer measures the DOM and so
 * draws nothing under SSR -- this checks the page around the chart, not the
 * plotted geometry.
 *
 * Run with the API up:  node smoke.mjs
 */
import { renderToString } from "react-dom/server";
import { createElement as h } from "react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import CustomerOverview from "./src/routes/CustomerOverview.tsx";
import CustomerBills from "./src/routes/CustomerBills.tsx";
import CustomerIssues from "./src/routes/CustomerIssues.tsx";

const API = "http://127.0.0.1:8000";

// The client calls relative /api paths; point them at the running server.
const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) =>
  realFetch(url.startsWith("/") ? `${API}${url}` : url, init);

const sites = await (await fetch("/api/sites")).json();
const solar = sites.find((s) => s.has_solar);
console.log(`demo site: ${solar.label} (${solar.district}), solar=${solar.has_solar}`);

const PAGES = {
  CustomerOverview,
  CustomerBills,
  CustomerIssues,
};

let failed = false;

for (const [name, Page] of Object.entries(PAGES)) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  // Prime the cache the way the live page would, so the render sees data
  // rather than only its loading state.
  await client.prefetchQuery({
    queryKey: ["sites"],
    queryFn: () => fetch("/api/sites").then((r) => r.json()),
  });
  await client.prefetchQuery({
    queryKey: ["sites", solar.site_id, "summary"],
    queryFn: () =>
      fetch(`/api/sites/${solar.site_id}/summary`).then((r) => r.json()),
  });
  await client.prefetchQuery({
    queryKey: ["sites", solar.site_id, "readings", 7],
    queryFn: () =>
      fetch(`/api/sites/${solar.site_id}/readings?days=7`).then((r) => r.json()),
  });
  await client.prefetchQuery({
    queryKey: ["sites", solar.site_id, "bills"],
    queryFn: () =>
      fetch(`/api/sites/${solar.site_id}/bills`).then((r) => r.json()),
  });
  await client.prefetchQuery({
    queryKey: ["issues"],
    queryFn: () => fetch("/api/issues").then((r) => r.json()),
  });

  try {
    const html = renderToString(
      h(
        QueryClientProvider,
        { client },
        h(
          MemoryRouter,
          { initialEntries: [`/customer?site=${solar.site_id}`] },
          h(Page, null),
        ),
      ),
    );
    const text = html
      .replace(/<[^>]+>/g, " ")
      .replace(/&[a-z]+;/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    console.log(`\n--- ${name} (${html.length} bytes) ---`);
    console.log(text.slice(0, 700));
  } catch (err) {
    failed = true;
    console.error(`\n--- ${name} FAILED ---`);
    console.error(err);
  }
}

process.exit(failed ? 1 : 0);
