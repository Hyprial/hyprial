import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.spec.mjs",
  fullyParallel: true,
  use: {
    baseURL: "http://127.0.0.1:3181",
    viewport: { width: 1440, height: 1050 },
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
      : {},
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node tests/serve-fixtures.mjs",
    url: "http://127.0.0.1:3181/api/capabilities",
    reuseExistingServer: false,
  },
});
