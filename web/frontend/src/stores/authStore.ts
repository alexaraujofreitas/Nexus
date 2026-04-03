import { create } from 'zustand';
import { login as apiLogin, setup as apiSetup, logout as apiLogout, getMe } from '../api/auth';
import type { LoginRequest, SetupRequest } from '../api/auth';

interface AuthState {
  isAuthenticated: boolean;
  email: string | null;
  isLoading: boolean;
  error: string | null;

  login: (data: LoginRequest) => Promise<void>;
  setup: (data: SetupRequest) => Promise<void>;
  logout: () => Promise<void>;
  checkAuth: () => Promise<void>;
  clearError: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  isAuthenticated: !!localStorage.getItem('access_token'),
  email: null,
  isLoading: false,
  error: null,

  login: async (data) => {
    set({ isLoading: true, error: null });
    try {
      const tokens = await apiLogin(data);
      localStorage.setItem('access_token', tokens.access_token);
      localStorage.setItem('refresh_token', tokens.refresh_token);
      set({ isAuthenticated: true, email: data.email, isLoading: false });
    } catch (err: any) {
      const msg = err.response?.data?.detail || 'Login failed';
      set({ isLoading: false, error: msg });
      throw err;
    }
  },

  setup: async (data) => {
    set({ isLoading: true, error: null });
    try {
      const tokens = await apiSetup(data);
      localStorage.setItem('access_token', tokens.access_token);
      localStorage.setItem('refresh_token', tokens.refresh_token);
      set({ isAuthenticated: true, email: data.email, isLoading: false });
    } catch (err: any) {
      const msg = err.response?.data?.detail || 'Setup failed';
      set({ isLoading: false, error: msg });
      throw err;
    }
  },

  logout: async () => {
    try {
      await apiLogout();
    } catch {
      // Ignore errors during logout
    }
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    set({ isAuthenticated: false, email: null });
  },

  checkAuth: async () => {
    const token = localStorage.getItem('access_token');
    if (!token) {
      set({ isAuthenticated: false });
      return;
    }
    try {
      const me = await getMe();
      set({ isAuthenticated: true, email: me.email });
    } catch {
      set({ isAuthenticated: false });
    }
  },

  clearError: () => set({ error: null }),
}));
