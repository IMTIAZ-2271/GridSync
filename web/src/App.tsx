import { Navigate, Route, Routes } from "react-router-dom";

import Layout from "./components/Layout";
import CustomerOverview from "./routes/CustomerOverview";
import CustomerReadings from "./routes/CustomerReadings";
import CustomerBills from "./routes/CustomerBills";
import CustomerIssues from "./routes/CustomerIssues";
import WorkerOrders from "./routes/WorkerOrders";
import WorkerIssues from "./routes/WorkerIssues";
import GovernmentByArea from "./routes/GovernmentByArea";
import GovernmentAgreements from "./routes/GovernmentAgreements";
import SupplierSites from "./routes/SupplierSites";
import SupplierAnalytics from "./routes/SupplierAnalytics";

/**
 * Routes are grouped by portal and mirror the `routes` array in portals.ts --
 * that file drives the nav, this one drives what renders. Keep them in step.
 */
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        {/* Supplier is the fleet-wide view, so it is the most useful landing. */}
        <Route index element={<Navigate to="/supplier" replace />} />

        <Route path="customer">
          <Route index element={<CustomerOverview />} />
          <Route path="readings" element={<CustomerReadings />} />
          <Route path="bills" element={<CustomerBills />} />
          <Route path="issues" element={<CustomerIssues />} />
        </Route>

        <Route path="worker">
          <Route index element={<WorkerOrders />} />
          <Route path="issues" element={<WorkerIssues />} />
        </Route>

        <Route path="government">
          <Route index element={<GovernmentByArea />} />
          <Route path="agreements" element={<GovernmentAgreements />} />
        </Route>

        <Route path="supplier">
          <Route index element={<SupplierSites />} />
          <Route path="analytics" element={<SupplierAnalytics />} />
        </Route>

        <Route path="*" element={<Navigate to="/supplier" replace />} />
      </Route>
    </Routes>
  );
}
