import { useState } from 'react';
import { useAuthStore } from '../../stores/authStore';
import { useWSStore } from '../../stores/wsStore';
import { cn } from '../../lib/utils';
import { menuItems } from './menuConfig';
import SidebarItem from './SidebarItem';

export default function Header() {
  const { email, logout } = useAuthStore();
  const wsStatus = useWSStore((s) => s.status);
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <>
      <header className="h-14 border-b border-gray-200 bg-white flex items-center justify-between px-4 shrink-0">
        <div className="flex items-center gap-3">
          {/* Mobile hamburger */}
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className="md:hidden p-2 -ml-2 rounded-md text-gray-500 hover:bg-gray-100 min-h-[44px] min-w-[44px] flex items-center justify-center"
            aria-label="Toggle menu"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              {menuOpen ? (
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
              )}
            </svg>
          </button>

          {/* Mobile title */}
          <h1 className="text-base font-semibold text-gray-900 md:hidden">NexusTrader</h1>

          {/* Engine status */}
          <div className="flex items-center gap-1.5">
            <span className={cn(
              'w-2 h-2 rounded-full',
              wsStatus === 'connected' ? 'bg-green-500' : wsStatus === 'connecting' ? 'bg-yellow-500' : 'bg-red-400',
            )} />
            <span className="text-xs text-gray-500 hidden sm:inline">
              Engine {wsStatus === 'connected' ? 'Connected' : wsStatus === 'connecting' ? 'Connecting' : 'Disconnected'}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <span className="text-sm text-gray-500 hidden sm:inline">{email}</span>
          <button
            onClick={logout}
            className="text-sm text-gray-500 hover:text-gray-700 px-2 py-1 rounded-md hover:bg-gray-100 transition-colors min-h-[44px]"
          >
            Sign out
          </button>
        </div>
      </header>

      {/* Mobile slide-down menu */}
      {menuOpen && (
        <div data-testid="mobile-drawer" className="md:hidden bg-white border-b border-gray-200 shadow-sm">
          <ul className="py-1 px-2">
            {menuItems.map((item) => (
              <SidebarItem
                key={item.path}
                item={item}
                onClick={() => setMenuOpen(false)}
              />
            ))}
          </ul>
        </div>
      )}
    </>
  );
}
