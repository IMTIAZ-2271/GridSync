import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import Layout from "./components/Layout";
import RequireAuth from "./auth/RequireAuth";
import { HOME_FOR_ROLE, useAuth } from "./auth/AuthContext";
import Login from "./routes/Login";
import Register from "./routes/Register";
import ConsumerOverview from "./routes/ConsumerOverview";
import ConsumerBills from "./routes/ConsumerBills";
import ConsumerDevices from "./routes/ConsumerDevices";
import ConsumerMeters from "./routes/ConsumerMeters";
import ConsumerIssues from "./routes/ConsumerIssues";
import ConsumerSettings from "./routes/ConsumerSettings";
import ConsumerApplications from "./routes/ConsumerApplications";
import ConsumerVisits from "./routes/ConsumerVisits";
import WorkerOrders from "./routes/WorkerOrders";
import WorkerIssues from "./routes/WorkerIssues";
import GovernmentByArea from "./routes/GovernmentByArea";
import GovernmentAgreements from "./routes/GovernmentAgreements";
import GovernmentNetMetering from "./routes/GovernmentNetMetering";
import GovernmentWorkers from "./routes/GovernmentWorkers";
import GovernmentSupplierRegistrations from "./routes/GovernmentSupplierRegistrations";
import GovernmentMeterApplications from "./routes/GovernmentMeterApplications";
import SupplierSites from "./routes/SupplierSites";
import SupplierApplications from "./routes/SupplierApplications";
import SupplierDispatch from "./routes/SupplierDispatch";
import SupplierIssues from "./routes/SupplierIssues";
import SupplierEquipment from "./routes/SupplierEquipment";

/**
 * The consumer portal lived at /customer until 2026-08-27. Redirect the whole
 * subtree rather than letting an old bookmark fall through to Home, so a link
 * to /customer/bills still opens the bills page.
 */
function LegacyConsumerPath() {
  const { pathname, search } = useLocation();
  return (
    <Navigate to={pathname.replace(/^\/customer/, "/consumer") + search} replace />
  );
}

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

          <Route path="consumer">
            <Route index element={<ConsumerOverview />} />
            <Route path="bills" element={<ConsumerBills />} />
            <Route path="meters" element={<ConsumerMeters />} />
            <Route path="devices" element={<ConsumerDevices />} />
            <Route path="issues" element={<ConsumerIssues />} />
            <Route path="applications" element={<ConsumerApplications />} />
            {/* The tab was called "Solar" until 2026-08-27. Kept as a
                redirect so a bookmark still lands somewhere. */}
            <Route
              path="solar"
              element={<Navigate to="/consumer/applications" replace />}
            />
            <Route path="visits" element={<ConsumerVisits />} />
            <Route path="settings" element={<ConsumerSettings />} />
          </Route>

          <Route path="customer/*" element={<LegacyConsumerPath />} />

          <Route path="worker">
            <Route index element={<WorkerOrders />} />
            <Route path="issues" element={<WorkerIssues />} />
          </Route>

          <Route path="government">
            <Route index element={<GovernmentByArea />} />
            <Route path="agreements" element={<GovernmentAgreements />} />
            <Route path="net-metering" element={<GovernmentNetMetering />} />
            <Route path="workers" element={<GovernmentWorkers />} />
            <Route
              path="supplier-registrations"
              element={<GovernmentSupplierRegistrations />}
            />
            <Route
              path="meter-applications"
              element={<GovernmentMeterApplications />}
            />
          </Route>

          <Route path="supplier">
            <Route index element={<SupplierSites />} />
            <Route path="dispatch" element={<SupplierDispatch />} />
            <Route path="applications" element={<SupplierApplications />} />
            <Route path="issues" element={<SupplierIssues />} />
            <Route path="equipment" element={<SupplierEquipment />} />
          </Route>
        </Route>
      </Route>

      <Route path="*" element={<Home />} />
    </Routes>
  );
}
