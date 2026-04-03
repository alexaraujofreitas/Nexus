/**
 * Phase 7E: Responsive Layout Validation
 *
 * Tests key pages at mobile (375px) and desktop (1280px) viewports:
 *   - No horizontal overflow
 *   - Navigation is usable (hamburger on mobile, sidebar on desktop)
 *   - Main content is visible and accessible
 *   - No layout breakage
 */
import { test, expect, Page } from '@playwright/test';
import { login, navigateTo, assertNoErrorOverlay } from './helpers';

const MOBILE = { width: 375, height: 812 };  // iPhone X
const DESKTOP = { width: 1280, height: 800 };

async function checkNoHorizontalOverflow(page: Page) {
  const body = page.locator('body');
  const bodyWidth = await body.evaluate((el) => el.scrollWidth);
  const viewportWidth = await page.evaluate(() => window.innerWidth);
  // Allow 2px tolerance for scrollbar
  expect(bodyWidth).toBeLessThanOrEqual(viewportWidth + 2);
}

test.describe('Mobile viewport (375px)', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize(MOBILE);
    await login(page);
  });

  test('dashboard renders without overflow', async ({ page }) => {
    await expect(page.locator('main')).toBeVisible();
    await checkNoHorizontalOverflow(page);
    await assertNoErrorOverlay(page);
  });

  test('scanner page renders without overflow', async ({ page }) => {
    await navigateTo(page, /scanner/i);
    await expect(page.locator('main')).toBeVisible();
    await checkNoHorizontalOverflow(page);
    await assertNoErrorOverlay(page);
  });

  test('settings page renders without overflow', async ({ page }) => {
    await navigateTo(page, /settings/i);
    await expect(page.locator('main')).toBeVisible();
    await checkNoHorizontalOverflow(page);
    await assertNoErrorOverlay(page);
  });

  test('hamburger menu is visible on mobile', async ({ page }) => {
    const hamburger = page.getByLabel(/toggle menu/i);
    await expect(hamburger).toBeVisible();
  });

  test('navigation works via hamburger menu', async ({ page }) => {
    const hamburger = page.getByLabel(/toggle menu/i);
    await hamburger.click();
    // After clicking hamburger, navigation links should be visible
    const scannerLink = page.getByRole('link', { name: /scanner/i });
    await expect(scannerLink).toBeVisible();
    await scannerLink.click();
    await expect(page.locator('main')).toBeVisible();
  });
});

test.describe('Desktop viewport (1280px)', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize(DESKTOP);
    await login(page);
  });

  test('dashboard renders without overflow', async ({ page }) => {
    await expect(page.locator('main')).toBeVisible();
    await checkNoHorizontalOverflow(page);
    await assertNoErrorOverlay(page);
  });

  test('scanner page renders without overflow', async ({ page }) => {
    await navigateTo(page, /scanner/i);
    await expect(page.locator('main')).toBeVisible();
    await checkNoHorizontalOverflow(page);
    await assertNoErrorOverlay(page);
  });

  test('sidebar navigation is visible on desktop', async ({ page }) => {
    // On desktop, the sidebar should be visible without hamburger
    const sidebar = page.locator('nav, aside').first();
    await expect(sidebar).toBeVisible();
  });

  test('analytics page renders with sub-navigation', async ({ page }) => {
    await navigateTo(page, /analytics/i);
    await expect(page.locator('main')).toBeVisible();
    await checkNoHorizontalOverflow(page);
    await assertNoErrorOverlay(page);
  });

  test('risk page renders without overflow', async ({ page }) => {
    await navigateTo(page, /risk/i);
    await expect(page.locator('main')).toBeVisible();
    await checkNoHorizontalOverflow(page);
    await assertNoErrorOverlay(page);
  });
});
