import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Logs page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('navigates to logs and renders filter controls', async ({ page }) => {
    await navigateTo(page, /logs/i);
    await expect(page).toHaveURL(/\/logs/);
    // Level dropdown and search input should be visible
    await expect(page.locator('select').first()).toBeVisible();
    await expect(page.getByPlaceholder(/search/i)).toBeVisible();
    await assertNoErrorOverlay(page);
  });
});
