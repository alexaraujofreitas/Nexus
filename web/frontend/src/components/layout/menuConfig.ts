import {
  LayoutDashboard,
  Monitor,
  Boxes,
  Radar,
  TrendingUp,
  LineChart,
  BrainCircuit,
  ShieldAlert,
  BarChart3,
  FlaskConical,
  CheckCircle2,
  FileText,
  Bell,
  Settings,
  Plug,
  type LucideIcon,
} from 'lucide-react';

// RULE: All menu items MUST include an icon.
// Do NOT add items without icon mapping.
// Every entry must satisfy the MenuItem interface — TypeScript will
// enforce the icon field at compile time.

export interface MenuItem {
  /** Display label shown in the sidebar */
  label: string;
  /** lucide-react icon component — REQUIRED, never omit */
  icon: LucideIcon;
  /** Route path (must match a <Route> in App.tsx) */
  path: string;
}

/**
 * Centralized navigation menu definition.
 *
 * Adding a new page:
 *   1. Import the icon from lucide-react above
 *   2. Add an entry here with label, icon, and path
 *   3. Add the matching <Route> in App.tsx
 *
 * The TypeScript interface guarantees every item has an icon.
 * The runtime guard in SidebarItem.tsx will throw in development
 * if an icon is somehow missing.
 */
export const menuItems: MenuItem[] = [
  { label: 'Dashboard',        icon: LayoutDashboard,  path: '/' },
  { label: 'Trades',            icon: Monitor,          path: '/monitor' },
  { label: 'Asset Management', icon: Boxes,            path: '/assets' },
  { label: 'Scanner',          icon: Radar,            path: '/scanner' },
  { label: 'Market Regime',    icon: TrendingUp,       path: '/regime' },
  { label: 'Charts',           icon: LineChart,        path: '/charts' },
  { label: 'Intelligence',     icon: BrainCircuit,     path: '/intelligence' },
  { label: 'Risk',             icon: ShieldAlert,      path: '/risk' },
  { label: 'Analytics',        icon: BarChart3,        path: '/analytics' },
  { label: 'Backtest',         icon: FlaskConical,     path: '/backtest' },
  { label: 'Validation',       icon: CheckCircle2,     path: '/validation' },
  { label: 'Logs',             icon: FileText,         path: '/logs' },
  { label: 'Notifications',    icon: Bell,             path: '/notifications' },
  { label: 'Settings',         icon: Settings,         path: '/settings' },
  { label: 'Exchanges',        icon: Plug,             path: '/exchanges' },
];
