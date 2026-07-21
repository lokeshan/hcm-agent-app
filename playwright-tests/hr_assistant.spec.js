// Real-browser UI test for the HR Assistant prototype.
//   npm install && npx playwright install chromium && npx playwright test
const { test, expect } = require('@playwright/test');
const path = require('path');
const { pathToFileURL } = require('url');

const APP = pathToFileURL(path.resolve(__dirname, '..', 'HR_Assistant.html')).href;

const CASES = [
  { q: 'Get me the details for Jane Doe',        tools: ['search_workers', 'get_worker'],                       want: ['Jane Doe', 'Senior Software Engineer', 'jane.doe@example.com', 'Michael Chen'] },
  { q: 'Who does Jane Doe report to?',           tools: ['search_workers', 'get_assignment'],                   want: ['reports to', 'Michael Chen'] },
  { q: 'Who reports to Priya Nair?',             tools: ['search_workers', 'get_direct_reports'],               want: ['manages 2 people', 'Michael Chen', 'Ahmed Khan'] },
  { q: 'Reporting chain above Jane Doe',         tools: ['search_workers', 'get_management_chain'],             want: ['Michael Chen', 'Priya Nair', 'Robert King'] },
  { q: 'Who works with Sara Williams?',          tools: ['search_workers', 'get_assignment', 'list_by_department'], want: ['Human Resources', 'Linda Gomez'] },
  { q: 'How many people are in Engineering?',    tools: ['list_by_department'],                                 want: ['3', 'Engineering'] },
  { q: 'Show me everyone in Finance',            tools: ['list_by_department'],                                 want: ['David Smith', 'Emily Turner'] },
  { q: 'Find someone called Bob Marley',         tools: ['search_workers'],                                     want: ["couldn't find", 'Bob Marley'] },
];

test.describe('HR Assistant — browser UI', () => {
  test('app loads with input, send button and suggestions', async ({ page }) => {
    await page.goto(APP);
    await expect(page.locator('#q')).toBeVisible();
    await expect(page.locator('#send')).toBeVisible();
    await expect(page.locator('.suggest button').first()).toBeVisible();
    await expect(page.locator('.row.bot .bubble').first()).toContainText('HR Assistant');
  });

  for (const c of CASES) {
    test(`answers: ${c.q}`, async ({ page }) => {
      await page.goto(APP);
      await page.fill('#q', c.q);
      await page.click('#send');

      // the final (non-typing) assistant bubble, once the delayed response renders
      const answer = page.locator('.row.bot:not(.typing)').last();
      await expect(answer.locator('.bubble')).toContainText(c.want[0], { timeout: 6000 });

      const text = await answer.locator('.bubble').innerText();
      for (const frag of c.want) expect(text).toContain(frag);

      const chip = await answer.locator('.chip').innerText();
      for (const t of c.tools) expect(chip).toContain(t);
    });
  }

  test('captures a screenshot of a conversation', async ({ page }, testInfo) => {
    await page.goto(APP);
    for (const q of ['Get me the details for Jane Doe', 'Who reports to Priya Nair?', 'Reporting chain above Jane Doe']) {
      await page.fill('#q', q);
      await page.click('#send');
      await expect(page.locator('.row.bot:not(.typing)').last().locator('.chip')).toBeVisible({ timeout: 6000 });
    }
    await page.screenshot({ path: testInfo.outputPath('conversation.png'), fullPage: true });
    await testInfo.attach('conversation', { path: testInfo.outputPath('conversation.png'), contentType: 'image/png' });
  });
});
