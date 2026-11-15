import { devices, expect, test, type Page } from '@playwright/test';

// The phone profiles run in Chromium so the suite needs one browser; the
// viewport, touch, and user agent still match each phone.
const phones = {
  'iPhone 13': devices['iPhone 13'],
  'Pixel 7': devices['Pixel 7'],
};

async function addManualItem(page: Page, name: string, price: string) {
  const nameField = page.getByRole('textbox', { name: 'Manual item name' });
  await expect(page.getByText('Add an item manually', { exact: true })).toBeVisible();
  if (!(await nameField.isVisible())) {
    await page.getByText('Add an item manually', { exact: true }).click();
  }
  await nameField.fill(name);
  await page.getByRole('textbox', { name: 'Manual item line price' }).fill(price);
  await page.getByRole('button', { name: 'Add item manually' }).click();
  await expect(page.getByText(`Added ${name} — quantity`)).toBeVisible();
}

for (const [phone, profile] of Object.entries(phones)) {
  test.describe(phone, () => {
    const { defaultBrowserType: _browser, ...phoneProfile } = profile;
    test.use(phoneProfile);

    test('corrects an item on a phone without the item table', async ({ page }) => {
      await page.goto('/');
      await page.getByRole('button', { name: 'Enter manually' }).click();
      await addManualItem(page, 'Coffee beans', '12.99');
      await addManualItem(page, 'Tex', '0.00');

      const pageWidth = await page.evaluate(() => [
        document.documentElement.scrollWidth,
        window.innerWidth,
      ]);
      expect(pageWidth[0]).toBeLessThanOrEqual(pageWidth[1]);

      await page.getByText('Edit one item (phone-friendly)').click();
      const itemName = page.getByRole('textbox', { name: 'Item name', exact: true });
      // iPhone Safari zooms into any text box smaller than 16px.
      await expect(itemName).toHaveCSS('font-size', '16px');

      await itemName.fill('Ground Coffee');
      await page
        .getByRole('textbox', { name: 'Line price (charged for the whole row)' })
        .fill('16.97');
      await page.getByRole('button', { name: 'Apply changes to this item' }).click();

      await expect(
        page.getByText('Updated row 1: Ground Coffee, $16.97.'),
      ).toBeVisible();
    });
  });
}
