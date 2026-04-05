import { lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import AppShell from './components/layout/AppShell';
import ErrorBoundary from './components/ErrorBoundary';
import Login from './pages/Login';
import Setup from './pages/Setup';
import Dashboard from './pages/Dashboard';
import Scanner from './pages/Scanner';
// Trading page removed — Trades (DemoMonitor) page is the consolidated view
import Intelligence from './pages/Intelligence';
import Risk from './pages/Risk';
import Settings from './pages/Settings';
import Logs from './pages/Logs';
import Analytics from './pages/Analytics';
import MarketRegime from './pages/MarketRegime';
import DemoMonitor from './pages/DemoMonitor';
import Validation from './pages/Validation';
import ExchangeManagement, { AssetManagementPage } from './pages/ExchangeManagement';
import Notifications from './pages/Notifications';
import NotFound from './pages/NotFound';

// Lazy-loaded heavy pages (charts library + backtest polling)
const Charts = lazy(() => import('./pages/Charts'));
const Backtest = lazy(() => import('./pages/Backtest'));

function LazyFallback() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
    </div>
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5000,
    },
  },
});

export default function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/setup" element={<Setup />} />
            <Route element={<AppShell />}>
              <Route path="/" element={<Dashboard />} />
              <Route path="monitor" element={<DemoMonitor />} />
              <Route path="assets" element={<AssetManagementPage />} />
              <Route path="scanner" element={<Scanner />} />
              <Route path="regime" element={<MarketRegime />} />
              <Route path="charts" element={<Suspense fallback={<LazyFallback />}><Charts /></Suspense>} />
              <Route path="intelligence" element={<Intelligence />} />
              <Route path="risk" element={<Risk />} />
              <Route path="analytics" element={<Analytics />} />
              <Route path="settings" element={<Settings />} />
              <Route path="logs" element={<Logs />} />
              <Route path="backtest" element={<Suspense fallback={<LazyFallback />}><Backtest /></Suspense>} />
              <Route path="validation" element={<Validation />} />
              <Route path="exchanges" element={<ExchangeManagement />} />
              <Route path="notifications" element={<Notifications />} />
            </Route>
            <Route path="*" element={<NotFound />} />
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
