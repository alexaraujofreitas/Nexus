import { cn } from '../../lib/utils';
import { useSidebarStore } from '../../stores/sidebarStore';
import { ChevronsLeft, ChevronsRight } from 'lucide-react';

interface SidebarHeaderProps {
  collapsed?: boolean;
}

export default function SidebarHeader({ collapsed }: SidebarHeaderProps) {
  const toggle = useSidebarStore((s) => s.toggle);

  return (
    <div className={cn(
      'flex items-center pt-4 pb-3 border-b border-gray-100 mb-2',
      collapsed ? 'justify-center px-2' : 'justify-between px-4',
    )}>
      <div className={cn('flex items-center', collapsed ? '' : 'gap-3')}>
        {/* Logo mark */}
        <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-blue-500 to-cyan-400 flex items-center justify-center shadow-[0_0_12px_rgba(59,130,246,0.25)] shrink-0">
          <span className="text-sm font-bold text-white tracking-tight select-none">
            NT
          </span>
        </div>

        {!collapsed && (
          <p className="text-sm font-semibold text-gray-900 whitespace-nowrap">NexusTrader</p>
        )}
      </div>

      {/* Collapse / Expand toggle */}
      {!collapsed && (
        <button
          onClick={toggle}
          className="p-1.5 rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-600 transition-colors"
          title="Collapse sidebar"
        >
          <ChevronsLeft size={16} strokeWidth={1.75} />
        </button>
      )}
      {collapsed && (
        <button
          onClick={toggle}
          className="mt-2 p-1.5 rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-600 transition-colors"
          title="Expand sidebar"
        >
          <ChevronsRight size={16} strokeWidth={1.75} />
        </button>
      )}
    </div>
  );
}
