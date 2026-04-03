import { test, expect } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

test.describe('Validation page', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('navigates to validation and shows health, readiness, and integrity sections', async ({ page }) => {
    await navigateTo(page, /validation/i);
    await expect(page).toHaveURL(/\/validation/);
    await expect(page.getByText(/component health/i)).toBeVisible();
    await expect(page.getByText(/system readiness/i)).toBeVisible();
    await expect(page.getByText(/data integrity/i)).toBeVisible();
    // Run checks button
    await expect(page.getByRole('button', { name: /run checks/i })).toBeVisible();
    await assertNoErrorOverlay(page);
  });
});
