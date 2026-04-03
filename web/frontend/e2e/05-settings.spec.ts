import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Settings page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('navigates to settings and shows all 4 tabs', async ({ page }) => {
    await navigateTo(page, /settings/i);
    await expect(page).toHaveURL(/\/settings/);
    // All 4 tabs should be visible
    await expect(page.getByRole('button', { name: /risk/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /strategy/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /execution/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /api keys/i })).toBeVisible();
    // Save button
    await expect(page.getByRole('button', { name: /save/i })).toBeVisible();
    await assertNoErrorOverlay(page);
  });

  test('switches between settings tabs', async ({ page }) => {
    await navigateTo(page, /settings/i);
    await page.getByRole('button', { name: /strategy/i }).click();
    // Model toggles should appear
    await expect(page.getByText(/model toggles/i)).toBeVisible();
    await page.getByRole('button', { name: /api keys/i }).click();
    await expect(page.getByText(/cryptopanic/i)).toBeVisible();
  });
});
