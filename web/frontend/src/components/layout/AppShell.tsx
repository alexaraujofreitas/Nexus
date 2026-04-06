import { useEffect } from 'react';
import { Outlet, Navigate } from 'react-router-dom';
import { useAuthStore } from '../../stores/authStore';
import { useWSStore } from '../../stores/wsStore';
import Sidebar from './Sidebar';
import Header from './Header';

export default function AppShell() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const connect = useWSStore((s) => s.connect);

  // Connect WebSocket once at the app shell level (all authenticated pages)
  useEffect(() => { connect(); }, [connect]);

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return (
    <div className="h-screen flex flex-col overflow-hidden">
      <Header />
      <div className="flex flex-1 min-h-0">
        <Sidebar />
        <main className="flex-1 p-4 md:p-6 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
