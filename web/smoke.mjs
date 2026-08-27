/**
 * Headless render check, one pass per role.
 *
 * Signs in with each demo account, prefetches what that role's landing page
 * reads, and renders it to a string against the live API. Catches crashes in
 * component logic and mismatched field names, which is otherwise a blank
 * screen in the browser.
 *
 * Recharts' ResponsiveContainer measures the DOM, so it draws nothing under
 * SSR -- this checks the page around the chart, not the plotted geometry.
 *
 *   node smoke.mjs      (with the API up on :8000)
 */
import { renderToString } from "react-dom/server";
import { createElement as h } from "react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { setToken } from "./src/lib/api.ts";
import ConsumerOverview from "./src/routes/ConsumerOverview.tsx";
import ConsumerBills from "./src/routes/ConsumerBills.tsx";
import ConsumerIssues from "./src/routes/ConsumerIssues.tsx";
import WorkerOrders from "./src/routes/WorkerOrders.tsx";
import GovernmentAgreements from "./src/routes/GovernmentAgreements.tsx";
import SupplierSites from "./src/routes/SupplierSites.tsx";

const API = "http://127.0.0.1:8000";
const PASSWORD = "demo1234";

const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) =>
  realFetch(typeof url === "string" && url.startsWith("/") ? `${API}${url}` : url, init);

let token = null;
const get = (path) =>
  fetch(`/api${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  }).then((r) => r.json());

async function signIn(email) {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password: PASSWORD }),
  });
  if (!res.ok) throw new Error(`login ${email} -> ${res.status}`);
  const body = await res.json();
  token = body.access_token;
  setToken(token); // so the components' own fetches carry it too
  return body.account;
}

function strip(html) {
  return html
    .replace(/<[^>]+>/g, " ")
    .replace(/&[a-z]+;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

const CASES = [
  {
    email: "consumer1@demo.com",
    pages: { ConsumerOverview, ConsumerBills, ConsumerIssues },
    async prime(qc) {
      const sites = await get("/sites");
      const id = sites[0].site_id;
      qc.setQueryData(["sites"], sites);
      qc.setQueryData(["sites", id, "summary"], await get(`/sites/${id}/summary`));
      qc.setQueryData(["sites", id, "readings", 7], await get(`/sites/${id}/readings?days=7`));
      qc.setQueryData(["sites", id, "bills"], await get(`/sites/${id}/bills`));
      qc.setQueryData(["issues"], await get("/issues"));
      return `owns ${sites.length} site(s): ${sites.map((s) => s.label).join(", ")}`;
    },
  },
  {
    email: "worker1@demo.com",
    pages: { WorkerOrders },
    async prime(qc) {
      const wos = await get("/work-orders");
      qc.setQueryData(["work-orders"], wos);
      qc.setQueryData(["issues"], await get("/issues"));
      return `${wos.length} assigned work orders`;
    },
  },
  {
    email: "gov1@demo.com",
    pages: { GovernmentAgreements },
    async prime(qc) {
      const pend = await get("/agreements/pending");
      qc.setQueryData(["agreements", "pending"], pend);
      qc.setQueryData(["analytics", "by-area"], await get("/analytics/by-area"));
      return `${pend.length} pending agreements`;
    },
  },
  {
    email: "supplier1@demo.com",
    pages: { SupplierSites },
    async prime(qc) {
      const sites = await get("/sites");
      qc.setQueryData(["sites"], sites);
      return `sees ${sites.length} sites`;
    },
  },
];

let failed = false;

for (const c of CASES) {
  const account = await signIn(c.email);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const note = await c.prime(qc);
  console.log(`\n### ${c.email}  role=${account.role}  ${note}`);

  for (const [name, Page] of Object.entries(c.pages)) {
    try {
      const html = renderToString(
        h(
          QueryClientProvider,
          { client: qc },
          h(MemoryRouter, { initialEntries: ["/"] }, h(Page, null)),
        ),
      );
      console.log(`  ${name}: ${strip(html).slice(0, 260)}`);
    } catch (err) {
      failed = true;
      console.error(`  ${name} FAILED: ${err.message}`);
    }
  }
}

setToken(null);
process.exit(failed ? 1 : 0);
