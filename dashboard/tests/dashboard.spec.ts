import { test, expect } from '@playwright/test';

// The product name is "Selective Intelligence" (index.html <title> and the
// Welcome heading). These two specs still asserted "Defense Analysis" and had
// been failing since the rename — the chain in TESTING_RESULTS.md is only a
// single pass/fail signal if its last stage is actually checking the product
// that ships.
test('has title', async ({ page }) => {
  await page.goto('http://localhost:5173');
  await expect(page).toHaveTitle(/Selective Intelligence/);
});

test('shows welcome screen elements', async ({ page }) => {
  await page.goto('http://localhost:5173');
  
  // Verify heading
  await expect(page.getByRole('heading', { name: 'Selective Intelligence', exact: true })).toBeVisible();
  
  // Verify login form elements
  await expect(page.getByRole('button', { name: 'Sign In', exact: true })).toBeVisible();
  await expect(page.getByPlaceholder('Enter your username')).toBeVisible();
  await expect(page.getByPlaceholder('••••••••')).toBeVisible();
});
