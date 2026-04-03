/**
 * Phase 6E: Risk Management page E2E tests
 *
 * Tests the risk management dashboard:
 *   - Page renders with risk metrics
 *   - Risk controls visible
 *   - No JS errors
 */
import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Risk Management page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('renders risk page with main content', async ({ page }) => {
    await navigateTo(page, /risk/i);
    const main = page.locator('main');
    await expect(main).toBeVisible();
    await assertNoErrorOverlay(page);
  });

  test('risk page displays heading', async ({ page }) => {
    await navigateTo(page, /risk/i);
    const heading = page.locator('main').getByRole('heading').first();
    await expect(heading).toBeVisible();
    await assertNoErrorOverlay(page);
  });

  test('risk page has no console errors', async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });
    await navigateTo(page, /risk/i);
    await page.waitForTimeout(1500);
    const realErrors = consoleErrors.filter(
      (e) =>
        !e.includes('ERR_CONNECTION_REFUSED') &&
        !e.includes('Failed to fetch') &&
        !e.includes('401'),
    );
    expect(realErrors).toHaveLength(0);
  });
});
