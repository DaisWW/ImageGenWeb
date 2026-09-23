const { expect, test } = require("./fixtures");

test("new image channels enable local repaint masks by default", async ({ studioPage: page }) => {
  const channel = {
    id: "masked-channel",
    label: "蒙版渠道",
    enabled: true,
    configured: true,
    base_url: "https://relay.example",
    has_api_key: true,
    api_key_hint: "test****cret",
    send_user_identifier: true,
    models: [{ id: "gpt-image-2", label: "GPT Image 2", enabled: true }],
    price_rmb: "0.0600",
    capabilities: {
      modes: ["text2img", "img2img"],
      supports_mask: false,
      max_reference_images: 16,
      max_reference_image_mb: 10,
      max_reference_total_mb: 40,
      formats: ["png", "jpeg", "webp"],
    },
    limits: {
      max_concurrency: 2,
      timeout_seconds: 600,
      estimated_seconds: 180,
      failure_window_seconds: 120,
      failure_threshold: 3,
      circuit_breaker_seconds: 300,
      half_open_max_probes: 1,
    },
  };
  const config = {
    version: 1,
    revision: "e2e-channel-revision",
    managed: true,
    source: "database",
    last_error: null,
    queue: { max_channel_attempts: 2, history_retention_days: 30, stale_running_minutes: 20 },
    channels: [channel],
  };

  await page.route("**/api/admin/channels", (route) => route.fulfill({ json: { config } }));
  await page.goto("/admin");
  await page.getByRole("button", { name: "渠道与模型", exact: true }).click();
  await expect(page.locator("#channelTableBody tr")).toHaveCount(1);

  await page.getByRole("button", { name: "新增渠道", exact: true }).click();
  const maskCheckbox = page.locator('#channelForm input[name="supports_mask"]');
  await expect(maskCheckbox).toBeChecked();
  await page.getByRole("button", { name: "取消", exact: true }).click();

  await page.locator('[data-edit-channel="masked-channel"]').click();
  await expect(maskCheckbox).not.toBeChecked();
});

test("channel list order is saved by dragging rows", async ({ studioPage: page }) => {
  const channel = {
    id: "first-channel",
    label: "第一渠道",
    enabled: true,
    configured: true,
    models: [{ id: "gpt-image-2", label: "GPT Image 2", enabled: true }],
    price_rmb: "0.0600",
    capabilities: {
      modes: ["text2img"],
      supports_mask: false,
      max_reference_images: 0,
      max_reference_image_mb: 10,
      max_reference_total_mb: 40,
      formats: ["png"],
    },
    limits: { max_concurrency: 2 },
  };
  const secondChannel = { ...channel, id: "second-channel", label: "第二渠道" };
  const config = {
    version: 1,
    revision: "e2e-channel-order",
    managed: true,
    source: "database",
    last_error: null,
    queue: { max_channel_attempts: 2, history_retention_days: 30, stale_running_minutes: 20 },
    channels: [channel, secondChannel],
  };
  const savedPayloads = [];

  await page.route("**/api/admin/channels", async (route) => {
    if (route.request().method() === "PUT") {
      const payload = route.request().postDataJSON();
      savedPayloads.push(payload);
      await route.fulfill({ json: { config: { ...config, revision: "e2e-channel-order-saved", channels: payload.channels } } });
      return;
    }
    await route.fulfill({ json: { config } });
  });
  await page.goto("/admin");
  await page.getByRole("button", { name: "渠道与模型", exact: true }).click();
  const rows = page.locator("#channelTableBody tr");
  await expect(rows).toHaveCount(2);

  await rows.nth(0).dragTo(rows.nth(1));

  await expect.poll(() => savedPayloads.length).toBe(1);
  expect(savedPayloads[0].channels.map((item) => item.id)).toEqual(["second-channel", "first-channel"]);
  expect(savedPayloads[0].channels.every((item) => !("priority" in item))).toBe(true);
});
