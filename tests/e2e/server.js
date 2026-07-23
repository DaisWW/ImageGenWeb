const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const { existsSync } = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function request(url, options = {}) {
  return new Promise((resolve, reject) => {
    const call = http.request(url, options, (response) => {
      response.resume();
      response.on("end", () => resolve(response.statusCode || 0));
    });
    call.on("error", reject);
    call.setTimeout(5_000, () => call.destroy(new Error("E2E server request timed out")));
    call.end();
  });
}

module.exports = async (config) => {
  const root = path.dirname(config.configFile);
  const baseURL = new URL(config.projects[0].use.baseURL);
  const port = baseURL.port;
  const dataDir = path.join(root, ".ui-test-data", `playwright-${process.pid}`);
  const venvPython = path.join(
    root,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const token = randomBytes(32).toString("hex");
  const child = spawn(existsSync(venvPython) ? venvPython : "python", [
    path.join(root, "tests/e2e/server.py"),
  ], {
    cwd: root,
    env: {
      ...process.env,
      ADMIN_USERNAME: "e2e-admin",
      ADMIN_PASSWORD: "E2eStrongPass123!",
      AUTO_CREATE_DB: "true",
      CONFIG_ENCRYPTION_KEY: "e2e-config-key",
      E2E_SHUTDOWN_TOKEN: token,
      IMAGEGEN_DATA_DIR: dataDir,
      IMAGE_STORAGE_PATH: path.join(dataDir, "files"),
      IMAGE_WEB_HOST: baseURL.hostname,
      IMAGE_WEB_PORT: port,
      PYTHONPATH: [root, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      SECRET_KEY: "e2e-secret-key",
    },
    stdio: ["ignore", "inherit", "inherit"],
    windowsHide: true,
  });
  let launchError = null;
  child.once("error", (error) => {
    launchError = error;
  });

  const healthURL = new URL("/health/live", baseURL);
  const deadline = Date.now() + 120_000;
  let ready = false;
  while (Date.now() < deadline) {
    if (launchError) throw launchError;
    if (child.exitCode !== null) throw new Error(`E2E server exited with code ${child.exitCode}`);
    try {
      if (await request(healthURL) === 200) {
        ready = true;
        break;
      }
    } catch (_error) {
      // The server is still starting.
    }
    await delay(250);
  }
  if (!ready) {
    child.kill();
    throw new Error("E2E server did not become ready");
  }

  return async () => {
    try {
      await request(new URL("/__e2e_shutdown", baseURL), {
        method: "POST",
        headers: { "X-E2E-Shutdown-Token": token },
      });
    } catch (_error) {
      child.kill();
    }
    if (child.exitCode === null) {
      const closed = new Promise((resolve) => child.once("exit", resolve));
      await Promise.race([closed, delay(5_000)]);
      if (child.exitCode === null) child.kill();
    }
  };
};
