const { expect, loginAsAdmin, test } = require("./fixtures");

test("chat delivery uses stable IDs and retries the same message", async ({ page }) => {
  const content = "改成宫崎骏风格";
  const sentAt = new Date().toISOString();
  const context = {
    compacted: false,
    estimated_context_tokens: 0,
    max_context_tokens: 32000,
  };
  let workspaceId = "";
  let releaseReply;
  const replyReleased = new Promise((resolve) => {
    releaseReply = resolve;
  });
  const requests = [];
  const assistantId = "a".repeat(32);

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
    const targetWorkspaceId = new URL(request.url()).pathname.split("/")[3];
    if (!workspaceId || targetWorkspaceId !== workspaceId) {
      await route.continue();
      return;
    }
    if (request.method() === "POST") {
      const body = request.postDataJSON();
      requests.push(body);
      if (requests.length === 1) {
        await route.fulfill({
          status: 500,
          json: {
            error: "服务器内部错误",
            code: "internal_error",
            error_id: "e2e-internal-error-id",
          },
        });
        return;
      }
      await replyReleased;
      await route.fulfill({
        status: 201,
        json: {
          messages: [userMessage(body, true), assistantMessage(body)],
          context,
        },
      });
      return;
    }
    if (requests.length) {
      const body = requests.at(-1);
      await route.fulfill({
        json: {
          messages: [userMessage(body)],
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

  await loginAsAdmin(page);
  workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");

  await expect(page.locator("#chatModelSelect")).toHaveValue("e2e-chat");
  await page.locator("#chatInput").fill(content);
  await page.locator("#chatForm").evaluate((form) => {
    form.requestSubmit();
    form.requestSubmit();
  });

  await expect.poll(() => requests.length).toBe(1);
  const userId = requests[0].message_id;
  expect(userId).toMatch(/^[a-f0-9]{32}$/);
  const userRow = page.locator('[data-message-id="' + userId + '"]');
  await expect(userRow).toContainText("发送失败");
  await expect(userRow).toContainText("错误原因：服务器内部错误（错误 ID：e2e-internal-error-id）");
  await expect(userRow.getByRole("button", { name: "重试发送" })).toBeVisible();
  await expect(page.locator("#toastRegion .toast")).toHaveCount(0);

  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(userRow).toContainText("发送失败");
  await expect(userRow.getByRole("button", { name: "重试发送" })).toBeVisible();

  await userRow.getByRole("button", { name: "重试发送" }).click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1].message_id).toBe(userId);

  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(userRow).not.toContainText("发送中");
  await expect(page.locator(".message-row.assistant.pending"))
    .toContainText("正在确认需求并整理最终提示词");
  await expect(page.locator(".message-row.user", { hasText: content })).toHaveCount(1);

  releaseReply();
  await expect(page.locator('[data-message-id="' + assistantId + '"]'))
    .toContainText("已按要求调整风格");
  await expect(page.locator(".message-row.user", { hasText: content })).toHaveCount(1);

  function userMessage(body, complete = false) {
    return {
      id: body.message_id,
      role: "user",
      kind: "message",
      content: body.content,
      payload: complete ? { reply_message_id: assistantId } : {},
      created_at: sentAt,
      attachments: [],
    };
  }

  function assistantMessage(body) {
    return {
      id: assistantId,
      role: "assistant",
      kind: "message",
      content: "已按要求调整风格",
      payload: { reply_to_message_id: body.message_id },
      provider_label: "E2E 助手",
      created_at: sentAt,
      attachments: [],
    };
  }
});
