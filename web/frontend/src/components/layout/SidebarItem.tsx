import { NavLink, useLocation } from 'react-router-dom';
import { cn } from '../../lib/utils';
import type { MenuItem } from './menuConfig';

interface SidebarItemProps {
  item: MenuItem;
  onClick?: () => void;
  collapsed?: boolean;
}

export default function SidebarItem({ item, onClick, collapsed }: SidebarItemProps) {
  // DEV guard: fail loudly if someone bypasses the TypeScript interface
  if (process.env.NODE_ENV !== 'production' && !item.icon) {
    throw new Error(
      `[SidebarItem] Menu item "${item.label}" is missing an icon. ` +
      'All menu items MUST include an icon — see menuConfig.ts.',
    );
  }

  const Icon = item.icon;
  const location = useLocation();
  const isActive = item.path === '/'
    ? location.pathname === '/'
    : location.pathname.startsWith(item.path);

  return (
    <li>
      <NavLink
        to={item.path}
        end
        onClick={onClick}
        title={collapsed ? item.label : undefined}
        className={cn(
          'flex items-center rounded-lg text-sm font-medium transition-colors duration-150',
          collapsed ? 'justify-center px-2 py-2' : 'gap-3 px-3 py-2',
          isActive
            ? 'bg-blue-50 text-blue-700'
            : 'text-gray-500 hover:bg-gray-50 hover:text-gray-900',
        )}
      >
        <Icon
          size={18}
          strokeWidth={1.75}
          className={cn(
            'shrink-0 transition-colors duration-150',
            isActive ? 'text-blue-600' : 'text-gray-400',
          )}
        />
        {!collapsed && (
          <span className={cn('whitespace-nowrap overflow-hidden', isActive ? 'font-semibold' : undefined)}>
            {item.label}
          </span>
        )}
      </NavLink>
    </li>
  );
}
