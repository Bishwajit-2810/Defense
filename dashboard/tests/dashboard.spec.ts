import { test, expect } from '@playwright/test';

test('has title', async ({ page }) => {
  await page.goto('http://localhost:5173');
  await expect(page).toHaveTitle(/Defense Analysis/);
});

test('shows welcome screen elements', async ({ page }) => {
  await page.goto('http://localhost:5173');
  
  // Verify heading
  await expect(page.getByRole('heading', { name: 'Defense Analysis', exact: true })).toBeVisible();
  
  // Verify login form elements
  await expect(page.getByRole('button', { name: 'Sign In', exact: true })).toBeVisible();
  await expect(page.getByPlaceholder('Enter your username')).toBeVisible();
  await expect(page.getByPlaceholder('••••••••')).toBeVisible();
});
