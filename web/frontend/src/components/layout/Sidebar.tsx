import { NavLink } from 'react-router-dom';
import { cn } from '../../lib/utils';

const navItems = [
  { label: 'Dashboard', path: '/' },
  { label: 'Scanner', path: '/scanner' },
  { label: 'Charts', path: '/charts' },
  { label: 'Trading', path: '/trading' },
  { label: 'Intelligence', path: '/intelligence' },
  { label: 'Risk', path: '/risk' },
  { label: 'Analytics', path: '/analytics' },
  { label: 'Backtest', path: '/backtest' },
  { label: 'Validation', path: '/validation' },
  { label: 'Logs', path: '/logs' },
  { label: 'Settings', path: '/settings' },
];

export default function Sidebar() {
  return (
    <nav className="w-56 shrink-0 border-r border-gray-200 bg-white hidden md:block">
      <div className="p-4">
        <h2 className="text-lg font-semibold text-gray-900">NexusTrader</h2>
        <p className="text-xs text-gray-400 mt-0.5">Web Dashboard</p>
      </div>
      <ul className="mt-2 space-y-0.5 px-2">
        {navItems.map((item) => (
          <li key={item.path}>
            <NavLink
              to={item.path}
              end
              className={({ isActive }) =>
                cn(
                  'block px-3 py-2 rounded-md text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-blue-50 text-blue-700'
                    : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900',
                )
              }
            >
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
