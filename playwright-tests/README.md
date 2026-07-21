# Playwright UI tests — HR Assistant

Real-browser end-to-end tests that drive `../HR_Assistant.html`: they type each
question, click **Send**, and assert the rendered answer **and** the MCP tool chips.

## Run
```bash
cd playwright-tests
npm run setup        # installs @playwright/test + downloads Chromium
npm test             # runs all tests headless
# npm run test:headed   # watch it drive the UI in a real browser
# npm run report        # open the HTML report (incl. a full-page screenshot)
```

## What it checks
- The app loads (input, Send button, suggestion chips, welcome message).
- 8 query types return the right answer text **and** call the right MCP tools:
  details, manager, direct reports, reporting chain, colleagues, department count,
  department list, and the not-found case.
- Produces a full-page screenshot of a live conversation (see the HTML report).

> Note: these download a real Chromium the first time (~150 MB). In a locked-down
> CI/sandbox without that network access, run `app_logic_test.cjs` in the parent
> folder instead — it tests the app's query brain + renderer with `node`, no browser.
