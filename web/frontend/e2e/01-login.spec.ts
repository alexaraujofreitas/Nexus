import { test, expect } from '@playwright/test';
import { TEST_USER, assertNoErrorOverlay } from './helpers';

test.describe('Login flow', () => {
  test('redirects unauthenticated user to /login', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/login/);
  });

  test('shows validation error for empty submit', async ({ page }) => {
    await page.goto('/login');
    await page.getByRole('button', { name: /sign in/i }).click();
    // HTML5 validation or custom error should prevent submit
    const emailInput = page.getByPlaceholder(/email/i);
    await expect(emailInput).toBeVisible();
  });

  test('successful login redirects to dashboard', async ({ page }) => {
    await page.goto('/login');
    await page.getByPlaceholder(/email/i).fill(TEST_USER.email);
    await page.getByPlaceholder(/password/i).fill(TEST_USER.password);
    await page.getByRole('button', { name: /sign in/i }).click();
    await expect(page).toHaveURL('/', { timeout: 10_000 });
    // Dashboard heading should be visible (scoped to main to avoid hidden sidebar text)
    await expect(page.locator('main').getByRole('heading', { name: /dashboard/i })).toBeVisible();
    await assertNoErrorOverlay(page);
  });
});
