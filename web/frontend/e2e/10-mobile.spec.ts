import { test, expect } from '@playwright/test';
import { login, assertNoErrorOverlay } from './helpers';

test.describe('Mobile viewport (375px)', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('hamburger menu opens and all nav items are accessible', async ({ page }) => {
    // Sidebar should be hidden on mobile
    const sidebar = page.locator('nav.w-56');
    await expect(sidebar).toBeHidden();

    // Hamburger button should be visible
    const hamburger = page.getByLabel(/toggle menu/i);
    await expect(hamburger).toBeVisible();
    await hamburger.click();

    // All 11 nav items should appear in the mobile drawer
    const navLinks = page.getByTestId('mobile-drawer').locator('a[href]');
    await expect(navLinks).toHaveCount(11);

    // Navigate to settings via mobile menu
    await page.getByTestId('mobile-drawer').getByRole('link', { name: /settings/i }).click();
    await expect(page).toHaveURL(/\/settings/);
    await expect(page.locator('main').getByText(/settings/i).first()).toBeVisible();
    await assertNoErrorOverlay(page);
  });

  test('pages render without horizontal overflow at 375px', async ({ page }) => {
    // Check dashboard
    const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
    expect(bodyWidth).toBeLessThanOrEqual(375);

    // Navigate to analytics
    const hamburger = page.getByLabel(/toggle menu/i);
    await hamburger.click();
    await page.getByRole('link', { name: /analytics/i }).click();
    await expect(page).toHaveURL(/\/analytics/);

    const analyticsBodyWidth = await page.evaluate(() => document.body.scrollWidth);
    expect(analyticsBodyWidth).toBeLessThanOrEqual(400); // small tolerance for charts
    await assertNoErrorOverlay(page);
  });
});
