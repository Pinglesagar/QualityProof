import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

// Resolved from this file rather than as a relative path: Playwright reports
// rootDir as the testDir, so a "../.." style path silently lands one level off.
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

/**
 * Example configuration exercising the reporter against the Python demo app.
 *
 * Everything here is the standard Playwright setup a team already runs — a
 * projects matrix, retries, sharding, trace-on-failure — with one extra
 * reporter. Nothing about adopting QualityProof changes how tests execute.
 */
export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: [
    ["list"],
    [
      "@qualityproof/playwright/reporter",
      {
        // The demo driver points this at a scratch project; the default keeps a
        // standalone `npm run example` self-contained.
        outputFile:
          process.env.QUALITYPROOF_EXTERNAL_OUTPUT ??
          resolve(repoRoot, ".qualityproof/external/playwright-run.json"),
        strictAnnotations: false,
      },
    ],
  ],
  use: {
    baseURL: process.env.QUALITYPROOF_BASE_URL ?? "http://127.0.0.1:8765",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    locale: "en-GB",
    timeZoneId: "Europe/London",
  },
  expect: { timeout: 5_000 },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
});
