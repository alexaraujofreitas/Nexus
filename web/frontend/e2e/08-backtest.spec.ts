import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Backtest page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('navigates to backtest and shows config panel with run button', async ({ page }) => {
    await navigateTo(page, /backtest/i);
    await expect(page).toHaveURL(/\/backtest/);
    // Config panel should show symbol toggles and run button
    await expect(page.getByText(/configuration/i)).toBeVisible();
    await expect(page.getByRole('button', { name: /run backtest/i })).toBeVisible();
    // Date inputs
    await expect(page.locator('input[type="date"]').first()).toBeVisible();
    await assertNoErrorOverlay(page);
  });
});
