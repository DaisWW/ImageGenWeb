const { expect, test } = require("./fixtures");

test("admin generation media preview, pan and wrap without horizontal scrolling", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const imageUrl = "/static/assets/starter-ocean-sky-reference.png";
  const thumbnailUrl = "/static/assets/brand-mark-v2.png";
  const createdAt = new Date().toISOString();
  const references = Array.from({ length: 20 }, (_, index) => ({
    id: `e2e-reference-${index}`,
    name: `垫图 ${index + 1}`,
    url: imageUrl,
    width: 512,
    height: 512,
  }));
  const items = Array.from({ length: 2 }, (_, index) => ({
    id: `e2e-output-${index}`,
    position: index,
    status: "succeeded",
    thumbnail_url: thumbnailUrl,
    image_url: imageUrl,
    width: 1024,
    height: 1024,
  }));
  const job = {
    id: "e2e-admin-layout-job",
    status: "succeeded",
    user: { id: 1, username: "e2e-admin", display_name: "E2E Admin" },
    created_at: createdAt,
    channel: "E2E 渠道",
    model: "e2e-image",
    prompt: "验证大量垫图与生成结果的布局",
    size: "1024x1024",
    quality: "high",
    succeeded_count: items.length,
    requested_count: items.length,
    charged_rmb: "0.0600",
    reserved_rmb: "0.0000",
    queue_position: null,
    can_cancel: false,
    references,
    items,
  };

  await page.route((url) => url.pathname === "/api/admin/generation-filters", (route) => (
    route.fulfill({ json: { users: [], workspaces: [] } })
  ));
  await page.route((url) => url.pathname === "/api/admin/generations", (route) => (
    route.fulfill({
      json: { jobs: [job], running_images: 0, queued_images: 0, queue_total: 0 },
    })
  ));

  await page.goto("/admin");
  await page.getByRole("button", { name: "生成记录", exact: true }).click();

  const card = page.locator(`[data-job-id="${job.id}"]`);
  await expect(card.locator(".admin-reference")).toHaveCount(20);
  await expect(card.locator(".admin-output")).toHaveCount(2);

  await card.locator(".admin-output").first().click();
  await expect(page.locator("#imageViewerDialog")).toBeVisible();
  await expect(page.locator("#imageViewerTitle"))
    .toHaveText("放大预览第 1 张生成结果 · E2E 渠道");
  await expect(page.locator("#imageViewerImage")).toHaveAttribute("src", imageUrl);
  await expect.poll(() => page.locator("#imageViewerImage")
    .evaluate((image) => image.naturalWidth)).toBeGreaterThan(0);
  const stage = page.locator("#imageViewerStage");
  await page.locator("#imageViewerZoomIn").click();
  await expect(stage).toHaveClass(/is-zoomed/);
  const stageBox = await stage.boundingBox();
  const transformBeforePan = await page.locator("#imageViewerImage")
    .evaluate((image) => image.style.transform);
  await page.mouse.move(stageBox.x + stageBox.width / 2, stageBox.y + stageBox.height / 2);
  await page.mouse.down();
  await expect(stage).toHaveClass(/is-dragging/);
  await page.mouse.move(stageBox.x + stageBox.width / 2 + 80, stageBox.y + stageBox.height / 2 + 40);
  await page.mouse.up();
  await expect(stage).not.toHaveClass(/is-dragging/);
  const transformAfterPan = await page.locator("#imageViewerImage")
    .evaluate((image) => image.style.transform);
  expect(transformAfterPan).not.toBe(transformBeforePan);
  await page.locator('[data-close-dialog="imageViewerDialog"]').click();

  await card.locator(".admin-reference").first().click();
  await expect(page.locator("#imageViewerDialog")).toBeVisible();
  await expect(page.locator("#imageViewerTitle")).toHaveText("放大预览 垫图 1");
  await expect(page.locator("#imageViewerImage")).toHaveAttribute("src", imageUrl);
  await page.locator('[data-close-dialog="imageViewerDialog"]').click();

  const layout = await card.evaluate((element) => {
    const stack = element.querySelector(".admin-media-stack");
    const referenceGroup = element.querySelector(".admin-reference-group");
    const referenceGrid = referenceGroup.querySelector(".admin-media-grid");
    const outputGroup = element.querySelector(".admin-output-group");
    const referenceBox = referenceGroup.getBoundingClientRect();
    const outputBox = outputGroup.getBoundingClientRect();
    const rows = new Set([...referenceGrid.children].map((tile) => (
      Math.round(tile.getBoundingClientRect().top)
    )));
    return {
      referenceRows: rows.size,
      outputTop: outputBox.top,
      referenceBottom: referenceBox.bottom,
      stackScrollWidth: stack.scrollWidth,
      stackClientWidth: stack.clientWidth,
      gridScrollWidth: referenceGrid.scrollWidth,
      gridClientWidth: referenceGrid.clientWidth,
    };
  });

  expect(layout.referenceRows).toBeGreaterThan(1);
  expect(layout.outputTop).toBeGreaterThanOrEqual(layout.referenceBottom);
  expect(layout.stackScrollWidth).toBeLessThanOrEqual(layout.stackClientWidth);
  expect(layout.gridScrollWidth).toBeLessThanOrEqual(layout.gridClientWidth);
});
