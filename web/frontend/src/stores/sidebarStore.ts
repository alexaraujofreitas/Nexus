import { create } from 'zustand';

interface SidebarState {
  collapsed: boolean;
  toggle: () => void;
  collapse: () => void;
  expand: () => void;
}

export const useSidebarStore = create<SidebarState>((set) => ({
  collapsed: false,
  toggle: () => set((s) => ({ collapsed: !s.collapsed })),
  collapse: () => set({ collapsed: true }),
  expand: () => set({ collapsed: false }),
}));
