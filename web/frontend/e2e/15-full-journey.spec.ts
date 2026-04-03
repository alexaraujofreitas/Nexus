/**
 * Phase 7D: Real-World User Journey E2E
 *
 * Tests a complete user workflow through the application:
 *   login → dashboard → scanner → charts → trading → analytics → risk → settings → logout
 *
 * This validates that all pages are reachable and functional
 * in a realistic user flow, not just in isolation.
 */
import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Full user journey', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('complete navigation flow: dashboard → scanner → charts → trading → analytics → risk → settings', async ({ page }) => {
    // 1. Dashboard (landing page after login)
    await expect(page.locator('main')).toBeVisible();
    await assertNoErrorOverlay(page);

    // 2. Scanner
    await navigateTo(page, /scanner/i);
    await expect(page.locator('main')).toBeVisible();
    await assertNoErrorOverlay(page);

    // 3. Charts
    await navigateTo(page, /chart/i);
    await expect(page.locator('main')).toBeVisible();
    await assertNoErrorOverlay(page);

    // 4. Trading
    await navigateTo(page, /trading/i);
    await expect(page.locator('main')).toBeVisible();
    await assertNoErrorOverlay(page);

    // 5. Analytics
    await navigateTo(page, /analytics/i);
    await expect(page.locator('main')).toBeVisible();
    await assertNoErrorOverlay(page);

    // 6. Risk
    await navigateTo(page, /risk/i);
    await expect(page.locator('main')).toBeVisible();
    await assertNoErrorOverlay(page);

    // 7. Settings
    await navigateTo(page, /settings/i);
    await expect(page.locator('main')).toBeVisible();
    await assertNoErrorOverlay(page);
  });

  test('no console errors during full navigation', async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });

    // Navigate through all pages
    const pages = [/scanner/i, /chart/i, /trading/i, /analytics/i, /risk/i, /settings/i, /log/i];
    for (const pageName of pages) {
      await navigateTo(page, pageName);
      await page.waitForTimeout(500);
    }

    // Filter out expected network errors (no backend in E2E)
    const realErrors = consoleErrors.filter(
      (e) =>
        !e.includes('ERR_CONNECTION_REFUSED') &&
        !e.includes('Failed to fetch') &&
        !e.includes('401') &&
        !e.includes('WebSocket') &&
        !e.includes('net::'),
    );
    expect(realErrors).toHaveLength(0);
  });

  test('404 page renders for unknown routes', async ({ page }) => {
    await page.goto('/this-page-does-not-exist');
    await expect(page.getByText(/not found/i)).toBeVisible();
  });
});
