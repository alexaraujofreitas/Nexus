// ============================================================
// Playwright Global Setup
//
// Runs ONCE before all test files. Creates the test user via
// /auth/setup (first-run endpoint). If a user already exists
// (409), that's fine — tests will use /auth/login.
// ============================================================
import { request } from '@playwright/test';
import { TEST_USER } from './helpers';

const API_URL = process.env.API_URL ?? 'http://localhost:8000';

async function globalSetup() {
  const ctx = await request.newContext();

  // Create test user via first-run setup endpoint
  const resp = await ctx.post(`${API_URL}/api/v1/auth/setup`, {
    data: {
      email: TEST_USER.email,
      password: TEST_USER.password,
      display_name: 'E2E Test User',
    },
  });

  if (resp.status() === 201) {
    console.log('[global-setup] Test user created successfully');
  } else if (resp.status() === 409) {
    console.log('[global-setup] Test user already exists — OK');
  } else {
    console.error(`[global-setup] Unexpected status ${resp.status()}: ${await resp.text()}`);
  }

  await ctx.dispose();
}

export default globalSetup;
