import SidebarHeader from './SidebarHeader';
import SidebarItem from './SidebarItem';
import { menuItems } from './menuConfig';
import { useSidebarStore } from '../../stores/sidebarStore';
import { cn } from '../../lib/utils';

export default function Sidebar() {
  const collapsed = useSidebarStore((s) => s.collapsed);

  return (
    <nav
      className={cn(
        'shrink-0 border-r border-gray-200 bg-white hidden md:flex md:flex-col',
        'h-full overflow-hidden',
        'transition-[width] duration-200 ease-in-out',
        collapsed ? 'w-[68px]' : 'w-56',
      )}
    >
      <SidebarHeader collapsed={collapsed} />

      <ul className="flex-1 space-y-px px-2 pb-2 overflow-y-auto overflow-x-hidden min-h-0">
        {menuItems.map((item) => (
          <SidebarItem key={item.path} item={item} collapsed={collapsed} />
        ))}
      </ul>
    </nav>
  );
}
