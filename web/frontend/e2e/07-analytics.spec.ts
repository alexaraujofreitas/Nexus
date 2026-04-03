import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Analytics page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('navigates to analytics and shows metric cards and chart containers', async ({ page }) => {
    await navigateTo(page, /analytics/i);
    await expect(page).toHaveURL(/\/analytics/);
    // Metric cards should be visible
    await expect(page.getByText(/win rate/i).first()).toBeVisible();
    await expect(page.getByText(/profit factor/i).first()).toBeVisible();
    // Equity curve section
    await expect(page.getByText(/equity curve/i)).toBeVisible();
    await assertNoErrorOverlay(page);
  });
});
