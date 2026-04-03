import { Page, expect } from '@playwright/test';

/** Default test user credentials for E2E. Must match backend seed. */
export const TEST_USER = {
  email: 'test@nexustrader.dev',
  password: 'TestPass123!',
};

/**
 * Log in via the login page and wait for redirect to dashboard.
 * Stores the JWT in localStorage so subsequent navigations stay authenticated.
 */
export async function login(page: Page) {
  await page.goto('/login');
  await page.getByPlaceholder(/email/i).fill(TEST_USER.email);
  await page.getByPlaceholder(/password/i).fill(TEST_USER.password);
  await page.getByRole('button', { name: /sign in/i }).click();
  // Wait for redirect to dashboard (authenticated shell)
  await expect(page).toHaveURL('/', { timeout: 10_000 });
}

/**
 * Navigate to a page via the sidebar (desktop) or hamburger menu (mobile).
 * On mobile viewports the sidebar is hidden, so we open the hamburger menu first.
 */
export async function navigateTo(page: Page, linkName: RegExp) {
  const hamburger = page.getByLabel(/toggle menu/i);
  if (await hamburger.isVisible()) {
    await hamburger.click();
  }
  await page.getByRole('link', { name: linkName }).click();
}

/**
 * Verify the page loaded without JS errors by checking for no error overlay.
 */
export async function assertNoErrorOverlay(page: Page) {
  const overlay = page.locator('vite-error-overlay');
  await expect(overlay).toHaveCount(0);
}
