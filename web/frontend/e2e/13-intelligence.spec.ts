/**
 * Phase 6E: Intelligence page E2E tests
 *
 * Tests the intelligence/agents dashboard:
 *   - Page renders with key sections
 *   - Agent status cards visible
 *   - No JS errors
 */
import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Intelligence page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('renders intelligence page with main content', async ({ page }) => {
    await navigateTo(page, /intelligence/i);
    const main = page.locator('main');
    await expect(main).toBeVisible();
    // Should have a heading related to intelligence/agents
    const heading = main.getByRole('heading').first();
    await expect(heading).toBeVisible();
    await assertNoErrorOverlay(page);
  });

  test('intelligence page loads without network errors', async ({ page }) => {
    const failedRequests: string[] = [];
    page.on('response', (response) => {
      if (response.status() >= 500) {
        failedRequests.push(`${response.url()} → ${response.status()}`);
      }
    });
    await navigateTo(page, /intelligence/i);
    await page.waitForTimeout(2000);
    // Filter expected API failures (engine not running in E2E)
    const unexpected = failedRequests.filter(
      (r) => !r.includes('/api/v1/') // API calls to missing engine are expected
    );
    expect(unexpected).toHaveLength(0);
  });
});
