const { test, expect } = require("@playwright/test");

test("an in-flight chat message is rendered once and submitted once", async ({ page }, testInfo) => {
  let targetWorkspaceId = "";
  let postCount = 0;
  let releasePost;
  const postRelease = new Promise((resolve) => {
    releasePost = resolve;
  });
  const sentAt = new Date().toISOString();
  const content = "改成宫崎骏风格";
  const context = {
    compacted: false,
    estimated_context_tokens: 0,
    max_context_tokens: 32000,
  };
  const userMessage = {
    id: "e2e-user-message",
    role: "user",
    kind: "message",
    content,
    payload: {},
    provider_id: "e2e-chat",
    provider_label: "E2E 助手",
    model: "e2e-model",
    input_tokens: null,
    output_tokens: null,
    elapsed_seconds: null,
    created_at: sentAt,
    attachments: [],
  };
  const assistantMessage = {
    ...userMessage,
    id: "e2e-assistant-message",
    role: "assistant",
    content: "已按要求调整风格。",
    input_tokens: 10,
    output_tokens: 8,
    elapsed_seconds: 0.5,
  };

  await page.route("**/api/chat-models", (route) => route.fulfill({
    json: {
      version: "e2e-chat-models",
      models: [{
        id: "e2e-chat",
        label: "E2E 助手",
        enabled: true,
        configured: true,
        model: "e2e-model",
        reasoning_effort: "",
      }],
    },
  }));
  await page.route("**/api/workspaces/*/messages*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const workspaceId = url.pathname.split("/")[3];
    if (!targetWorkspaceId || workspaceId !== targetWorkspaceId) {
      await route.continue();
      return;
    }
    if (request.method() === "POST") {
      postCount += 1;
      await postRelease;
      await route.fulfill({
        status: 201,
        json: { messages: [userMessage, assistantMessage], context },
      });
      return;
    }
    if (postCount > 0) {
      await route.fulfill({
        json: {
          messages: [userMessage],
          total: 1,
          has_more: false,
          context,
          conversation_operation: {
            busy: true,
            kind: "reply",
            label: "正在等待 AI 回复",
            started_at: sentAt,
          },
        },
      });
      return;
    }
    await route.continue();
  });

  await page.goto("/login");
  await page.getByLabel("用户名").fill("e2e-admin");
  await page.getByLabel("密码").fill("E2eStrongPass123!");
  await page.getByRole("button", { name: "进入工作台" }).click();

  const workspaceName = `E2E-Chat-${testInfo.project.name}-${Date.now()}`;
  await page.locator("#newWorkspaceButton").click();
  await page.locator("#workspaceNameInput").fill(workspaceName);
  await page.locator('#workspaceForm button[type="submit"]').click();
  await expect(page.locator("#workspaceTitle")).toHaveText(workspaceName);
  targetWorkspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");

  const chatInput = page.locator("#chatInput");
  await expect(chatInput).toBeEditable();
  await expect(page.locator("#chatModelSelect")).toHaveValue("e2e-chat");
  await chatInput.fill(content);
  await page.locator("#chatForm").evaluate((form) => {
    form.requestSubmit();
    form.requestSubmit();
  });
  await expect(page.locator(".message-row.user", { hasText: content })).toHaveCount(1);
  await expect.poll(() => postCount).toBe(1);

  const otherWorkspace = page.locator("#workspaceList .workspace-item:not(.active)").first();
  await otherWorkspace.locator("[data-select-workspace]").click();
  await expect(page.locator("#workspaceTitle")).not.toHaveText(workspaceName);
  await page.locator("#workspaceList .workspace-item", { hasText: workspaceName })
    .locator("[data-select-workspace]").click();
  await expect(page.locator("#workspaceTitle")).toHaveText(workspaceName);
  await expect(page.locator(".message-row.user", { hasText: content })).toHaveCount(1);

  releasePost();
  await expect(page.locator(".message-row.assistant", { hasText: assistantMessage.content }))
    .toHaveCount(1);
  await expect(page.locator(".message-row.user", { hasText: content })).toHaveCount(1);
  expect(postCount).toBe(1);

  const workspace = page.locator("#workspaceList .workspace-item", { hasText: workspaceName });
  await workspace.locator("[data-delete-workspace]").click();
  await page.locator('#workspaceDeleteForm button[type="submit"]').click();
  await expect(page.locator("#workspaceList")).not.toContainText(workspaceName);
});
