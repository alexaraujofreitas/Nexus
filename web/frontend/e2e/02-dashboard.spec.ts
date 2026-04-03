import { test, expect } from '@playwright/test';
import { login, assertNoErrorOverlay } from './helpers';

test.describe('Dashboard page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('renders dashboard with key sections', async ({ page }) => {
    await expect(page.locator('main').getByRole('heading', { name: /dashboard/i })).toBeVisible();
    // Engine status indicator (colored dot) should be visible in header
    await expect(page.locator('header [data-testid="engine-status"], header span.rounded-full').first()).toBeVisible();
    await assertNoErrorOverlay(page);
  });
});
