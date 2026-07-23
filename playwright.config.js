const { defineConfig, devices } = require("@playwright/test");

const port = 18765;
const localBrowser = process.env.CI ? {} : { channel: "chrome" };

module.exports = defineConfig({
  testDir: "tests/e2e",
  globalSetup: require.resolve("./tests/e2e/server"),
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop-chromium", use: { ...devices["Desktop Chrome"], ...localBrowser } },
    {
      name: "mobile-chromium",
      grep: /@responsive/,
      use: { ...devices["Pixel 7"], ...localBrowser },
    },
  ],
});
