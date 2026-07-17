const { createWorkspace, expect, test } = require("./fixtures");

test("switching workspaces shows the latest conversation message", {
  tag: "@responsive",
}, async ({ studioPage: page }, testInfo) => {
  const target = page.locator("#workspaceList .workspace-item.active");
  const targetId = await target.getAttribute("data-workspace-id");
  const targetName = await target.locator(".workspace-copy strong").textContent();
  const temporaryName = `E2E-Scroll-${testInfo.project.name}-${Date.now()}`;
  const messages = Array.from({ length: 16 }, (_, index) => ({
    id: `scroll-history-${index}`,
    role: index % 2 ? "assistant" : "user",
    kind: "message",
    content: `历史消息 ${index + 1}：用于验证切换工作站后的对话滚动位置。`,
    created_at: new Date(index * 1000).toISOString(),
  }));
  const latest = messages.at(-1);

  await createWorkspace(page, temporaryName);
  const temporaryId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");
  await page.route(`**/api/workspaces/${targetId}/messages?*`, (route) => route.fulfill({
    json: { messages },
  }));

  try {
    await page.locator(`[data-workspace-id="${targetId}"] [data-select-workspace]`).click();
    await expect(page.locator("#workspaceTitle")).toHaveText(targetName);
    const latestRow = page.locator(`[data-message-id="${latest.id}"]`);
    await expect(latestRow).toContainText(latest.content);
    await expect(latestRow).toBeInViewport();
  } finally {
    await page.evaluate((workspaceId) => window.ImageGen.api(`/api/workspaces/${workspaceId}`, {
      method: "DELETE",
    }), temporaryId);
  }
});
