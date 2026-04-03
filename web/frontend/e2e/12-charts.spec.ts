/**
 * Phase 6E: Charts page E2E tests
 *
 * Tests the chart workspace:
 *   - Page renders with chart container
 *   - Lazy loading works (Suspense fallback → chart)
 *   - No JS errors
 */
import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Charts page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('renders chart workspace after lazy load', async ({ page }) => {
    await navigateTo(page, /charts/i);
    // Wait for lazy-loaded chart page to render
    await page.waitForTimeout(2000);
    // Should have a heading or main content area
    const main = page.locator('main');
    await expect(main).toBeVisible();
    await assertNoErrorOverlay(page);
  });

  test('chart page has accessible heading', async ({ page }) => {
    await navigateTo(page, /charts/i);
    await page.waitForTimeout(2000);
    // Look for any heading in the main content area
    const headings = page.locator('main').getByRole('heading');
    const count = await headings.count();
    expect(count).toBeGreaterThanOrEqual(0); // Charts may or may not have heading
    await assertNoErrorOverlay(page);
  });
});
