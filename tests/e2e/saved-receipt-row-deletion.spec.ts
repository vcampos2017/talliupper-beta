import { expect, test } from '@playwright/test';

test('deletes an unwanted saved row and keeps it deleted after reload', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /TalliUpper/ })).toBeVisible({
    timeout: 45_000,
  });

  await page.getByRole('tab', { name: 'Saved receipts' }).click();
  await expect(page.getByText('Playwright Demo Store', { exact: true })).toBeVisible();

  await page.getByText('Delete a saved item row', { exact: true }).click();
  const confirmDeletion = page.getByRole('checkbox', {
    name: 'Delete row 1: Rewards information',
  });
  await page
    .getByText('Delete row 1: Rewards information', { exact: true })
    .click();
  await expect(confirmDeletion).toBeChecked();
  await page.getByRole('button', { name: 'Delete selected item row' }).click();

  await expect(
    page.getByText('Deleted row 1: Rewards information. 2 saved item(s) remain.'),
  ).toBeVisible();

  await page.reload();
  await page.getByRole('tab', { name: 'Saved receipts' }).click();
  await page.getByText('Delete a saved item row', { exact: true }).click();

  await expect(
    page.getByRole('checkbox', { name: 'Delete row 1: Coffee beans' }),
  ).toBeVisible();
  await expect(page.getByText('Rewards information', { exact: true })).toHaveCount(0);

  const downloadPromise = page.waitForEvent('download');
  await page
    .getByRole('button', { name: 'Download this receipt as compact PDF' })
    .click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/playwright-demo-store.*\.pdf$/i);
});
