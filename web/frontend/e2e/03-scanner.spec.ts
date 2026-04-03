import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Scanner page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('navigates to scanner and renders controls', async ({ page }) => {
    await navigateTo(page, /scanner/i);
    await expect(page).toHaveURL(/\/scanner/);
    // Scan trigger button should exist
    await expect(page.getByRole('button', { name: /scan/i }).first()).toBeVisible();
    await assertNoErrorOverlay(page);
  });
});
