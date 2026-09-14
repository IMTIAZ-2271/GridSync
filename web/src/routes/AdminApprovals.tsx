import GovernmentSupplierRegistrations from "./GovernmentSupplierRegistrations";
import GovernmentWorkers from "./GovernmentWorkers";

/**
 * Both registration queues, every district.
 *
 * The same components an official uses: the API returns every district's
 * applications to an admin, and each decision an admin takes here is recorded
 * in the audit log by the route itself.
 */
export default function AdminApprovals() {
  return (
    <div className="space-y-6">
      <GovernmentWorkers />
      <GovernmentSupplierRegistrations />
    </div>
  );
}
