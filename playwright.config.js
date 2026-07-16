const path = require("node:path");
const { existsSync } = require("node:fs");
const { defineConfig, devices } = require("@playwright/test");

const port = 18765;
const dataDir = path.resolve(".ui-test-data", `playwright-${process.pid}`);
const localBrowser = process.env.CI ? {} : { channel: "chrome" };
const venvPython = path.resolve(
  ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
const defaultServerCommand = `${existsSync(venvPython) ? JSON.stringify(venvPython) : "python"} app.py`;

module.exports = defineConfig({
  testDir: "tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: process.env.E2E_SERVER_COMMAND || defaultServerCommand,
    url: `http://127.0.0.1:${port}/health/live`,
    timeout: 120000,
    reuseExistingServer: false,
    env: {
      ...process.env,
      ADMIN_USERNAME: "e2e-admin",
      ADMIN_PASSWORD: "E2eStrongPass123!",
      AUTO_CREATE_DB: "true",
      CONFIG_ENCRYPTION_KEY: "e2e-config-key",
      IMAGEGEN_DATA_DIR: dataDir,
      IMAGE_STORAGE_PATH: path.join(dataDir, "files"),
      IMAGE_WEB_HOST: "127.0.0.1",
      IMAGE_WEB_PORT: String(port),
      SECRET_KEY: "e2e-secret-key",
    },
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
