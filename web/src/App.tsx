import { Navigate, Route, Routes } from "react-router-dom";

import Layout from "./components/Layout";
import RequireAuth from "./auth/RequireAuth";
import { HOME_FOR_ROLE, useAuth } from "./auth/AuthContext";
import Login from "./routes/Login";
import Register from "./routes/Register";
import CustomerOverview from "./routes/CustomerOverview";
import CustomerBills from "./routes/CustomerBills";
import CustomerDevices from "./routes/CustomerDevices";
import CustomerMeters from "./routes/CustomerMeters";
import CustomerIssues from "./routes/CustomerIssues";
import CustomerSettings from "./routes/CustomerSettings";
import CustomerSolar from "./routes/CustomerSolar";
import CustomerVisits from "./routes/CustomerVisits";
import WorkerOrders from "./routes/WorkerOrders";
import WorkerIssues from "./routes/WorkerIssues";
import GovernmentByArea from "./routes/GovernmentByArea";
import GovernmentAgreements from "./routes/GovernmentAgreements";
import GovernmentWorkers from "./routes/GovernmentWorkers";
import SupplierSites from "./routes/SupplierSites";
import SupplierApplications from "./routes/SupplierApplications";
import SupplierDispatch from "./routes/SupplierDispatch";
import SupplierEquipment from "./routes/SupplierEquipment";

/** Send "/" to the signed-in role's portal, or to the login page. */
function Home() {
  const { account, isLoading } = useAuth();
  if (isLoading) return null;
  return <Navigate to={account ? HOME_FOR_ROLE[account.role] : "/login"} replace />;
}

/**
 * Routes are grouped by portal and mirror the `routes` array in portals.ts --
 * that file drives the nav, this one drives what renders. Keep them in step.
 *
 * Everything below RequireAuth needs a valid token; /login and /register are
 * the only public pages.
 */
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />

      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route index element={<Home />} />

          <Route path="customer">
            <Route index element={<CustomerOverview />} />
            <Route path="bills" element={<CustomerBills />} />
            <Route path="meters" element={<CustomerMeters />} />
            <Route path="devices" element={<CustomerDevices />} />
            <Route path="issues" element={<CustomerIssues />} />
            <Route path="solar" element={<CustomerSolar />} />
            <Route path="visits" element={<CustomerVisits />} />
            <Route path="settings" element={<CustomerSettings />} />
          </Route>

          <Route path="worker">
            <Route index element={<WorkerOrders />} />
            <Route path="issues" element={<WorkerIssues />} />
          </Route>

          <Route path="government">
            <Route index element={<GovernmentByArea />} />
            <Route path="agreements" element={<GovernmentAgreements />} />
            <Route path="workers" element={<GovernmentWorkers />} />
          </Route>

          <Route path="supplier">
            <Route index element={<SupplierSites />} />
            <Route path="dispatch" element={<SupplierDispatch />} />
            <Route path="applications" element={<SupplierApplications />} />
            <Route path="equipment" element={<SupplierEquipment />} />
          </Route>
        </Route>
      </Route>

      <Route path="*" element={<Home />} />
    </Routes>
  );
}
