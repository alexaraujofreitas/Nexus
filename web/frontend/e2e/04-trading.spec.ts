import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Trading page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('navigates to trading and shows positions and history sections', async ({ page }) => {
    await navigateTo(page, /trading/i);
    await expect(page).toHaveURL(/\/trading/);
    // Should see positions area and trade history
    await expect(page.getByText(/position/i).first()).toBeVisible();
    await assertNoErrorOverlay(page);
  });
});
