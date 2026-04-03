import { create } from 'zustand';

type WSStatus = 'disconnected' | 'connecting' | 'connected';

interface WSState {
  status: WSStatus;
  ws: WebSocket | null;
  subscriptions: Set<string>;
  lastMessage: Record<string, any>;

  connect: () => void;
  disconnect: () => void;
  subscribe: (channel: string) => void;
  unsubscribe: (channel: string) => void;
}

let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
let reconnectDelay = 1000;
const MAX_RECONNECT_DELAY = 30000;

export const useWSStore = create<WSState>((set, get) => ({
  status: 'disconnected',
  ws: null,
  subscriptions: new Set(),
  lastMessage: {},

  connect: () => {
    const token = localStorage.getItem('access_token');
    if (!token) return;

    set({ status: 'connecting' });

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws?token=${token}`);

    ws.onopen = () => {
      set({ status: 'connected', ws });
      reconnectDelay = 1000; // Reset backoff

      // Re-subscribe to channels
      const { subscriptions } = get();
      subscriptions.forEach((channel) => {
        ws.send(JSON.stringify({ action: 'subscribe', channel }));
      });
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);

        // Handle server heartbeat ping
        if (msg.type === 'ping') {
          ws.send(JSON.stringify({ action: 'pong' }));
          return;
        }

        // Handle channel data
        if (msg.channel) {
          set((state) => ({
            lastMessage: { ...state.lastMessage, [msg.channel]: msg.data },
          }));
        }

        // Handle token expired
        if (msg.code === 'TOKEN_EXPIRED') {
          get().disconnect();
          window.location.href = '/login';
        }
      } catch {
        // Ignore parse errors
      }
    };

    ws.onclose = () => {
      set({ status: 'disconnected', ws: null });

      // Exponential backoff reconnect with jitter
      const jitter = Math.random() * 1000;
      reconnectTimeout = setTimeout(() => {
        if (localStorage.getItem('access_token')) {
          get().connect();
        }
      }, reconnectDelay + jitter);
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
    };

    ws.onerror = () => {
      ws.close();
    };
  },

  disconnect: () => {
    if (reconnectTimeout) {
      clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
    }
    const { ws } = get();
    if (ws) {
      ws.close();
    }
    set({ status: 'disconnected', ws: null });
  },

  subscribe: (channel) => {
    set((state) => {
      const subs = new Set(state.subscriptions);
      subs.add(channel);
      state.ws?.send(JSON.stringify({ action: 'subscribe', channel }));
      return { subscriptions: subs };
    });
  },

  unsubscribe: (channel) => {
    set((state) => {
      const subs = new Set(state.subscriptions);
      subs.delete(channel);
      state.ws?.send(JSON.stringify({ action: 'unsubscribe', channel }));
      return { subscriptions: subs };
    });
  },
}));
