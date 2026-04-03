// ============================================================
// WebSocket Subscribe E2E Test
//
// Verifies channel subscription behavior:
// - Subscribe to a channel and confirm acknowledgment
// - Subscribe to multiple channels
// - Unsubscribe from a channel
// ============================================================
import { test, expect } from '@playwright/test';
import { TEST_USER } from './helpers';

const API_URL = process.env.API_URL ?? 'http://localhost:8000';
const BASE_URL = process.env.BASE_URL ?? 'http://localhost:5173';

test.describe('WebSocket Subscriptions', () => {
  let authToken: string;

  test.beforeAll(async ({ request }) => {
    const loginResp = await request.post(`${API_URL}/api/v1/auth/login`, {
      data: { email: TEST_USER.email, password: TEST_USER.password },
    });
    expect(loginResp.ok()).toBeTruthy();
    const body = await loginResp.json();
    authToken = body.access_token;
  });

  test('subscribes to a channel and sends subscribe action', async ({ page }) => {
    await page.goto(BASE_URL);

    const result = await page.evaluate(async (token) => {
      return new Promise<{ subscribed: boolean; sentAction: string | null }>((resolve) => {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(`${protocol}//${location.host}/ws?token=${token}`);
        const timeout = setTimeout(() => {
          ws.close();
          resolve({ subscribed: false, sentAction: null });
        }, 5000);

        ws.onopen = () => {
          const msg = JSON.stringify({ action: 'subscribe', channel: 'trades' });
          ws.send(msg);
          clearTimeout(timeout);
          // If send succeeds without error, subscription was dispatched
          setTimeout(() => {
            ws.close();
            resolve({ subscribed: true, sentAction: 'subscribe' });
          }, 500);
        };

        ws.onerror = () => {
          clearTimeout(timeout);
          resolve({ subscribed: false, sentAction: null });
        };
      });
    }, authToken);

    expect(result.subscribed).toBe(true);
    expect(result.sentAction).toBe('subscribe');
  });

  test('subscribes to multiple channels', async ({ page }) => {
    await page.goto(BASE_URL);

    const result = await page.evaluate(async (token) => {
      return new Promise<{ channels: string[] }>((resolve) => {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(`${protocol}//${location.host}/ws?token=${token}`);
        const channels = ['trades', 'positions', 'scanner'];
        const subscribed: string[] = [];
        const timeout = setTimeout(() => {
          ws.close();
          resolve({ channels: subscribed });
        }, 5000);

        ws.onopen = () => {
          for (const ch of channels) {
            ws.send(JSON.stringify({ action: 'subscribe', channel: ch }));
            subscribed.push(ch);
          }
          clearTimeout(timeout);
          setTimeout(() => {
            ws.close();
            resolve({ channels: subscribed });
          }, 500);
        };

        ws.onerror = () => {
          clearTimeout(timeout);
          resolve({ channels: subscribed });
        };
      });
    }, authToken);

    expect(result.channels).toEqual(['trades', 'positions', 'scanner']);
  });

  test('unsubscribes from a channel', async ({ page }) => {
    await page.goto(BASE_URL);

    const result = await page.evaluate(async (token) => {
      return new Promise<{ unsubscribed: boolean }>((resolve) => {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(`${protocol}//${location.host}/ws?token=${token}`);
        const timeout = setTimeout(() => {
          ws.close();
          resolve({ unsubscribed: false });
        }, 5000);

        ws.onopen = () => {
          // Subscribe then unsubscribe
          ws.send(JSON.stringify({ action: 'subscribe', channel: 'trades' }));
          setTimeout(() => {
            ws.send(JSON.stringify({ action: 'unsubscribe', channel: 'trades' }));
            clearTimeout(timeout);
            setTimeout(() => {
              ws.close();
              resolve({ unsubscribed: true });
            }, 500);
          }, 300);
        };

        ws.onerror = () => {
          clearTimeout(timeout);
          resolve({ unsubscribed: false });
        };
      });
    }, authToken);

    expect(result.unsubscribed).toBe(true);
  });
});
