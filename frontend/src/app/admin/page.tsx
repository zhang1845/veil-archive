import AdminConsole from "@/components/admin/AdminConsole";
import AdminAccess from "@/components/admin/AdminAccess";

export default function AdminPage() {
  return (
    <AdminAccess>
      <AdminConsole />
    </AdminAccess>
  );
}
