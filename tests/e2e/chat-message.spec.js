const { expect, loginAsAdmin, test } = require("./fixtures");

test("chat waits for message persistence and resends stored messages with new IDs", {
  tag: "@responsive",
}, async ({ page }) => {
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
  const resentAssistantId = "d".repeat(32);
  let acceptedMessageId = "";
  let activeRequest = null;

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
      const requestNumber = requests.length;
      if (requestNumber === 1) {
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
      activeRequest = body;
      await replyReleased;
      activeRequest = null;
      const responseAssistantId = requestNumber === 2 ? assistantId : resentAssistantId;
      await route.fulfill({
        status: 201,
        json: {
          messages: [
            userMessage(body, true, responseAssistantId),
            assistantMessage(body, responseAssistantId),
          ],
          context,
        },
      });
      return;
    }
    if (requests.length) {
      const accepted = requests.find((body) => body.message_id === acceptedMessageId);
      await route.fulfill({
        json: {
          messages: accepted ? [userMessage(accepted)] : [],
          total: accepted ? 1 : 0,
          has_more: false,
          context,
          conversation_operation: activeRequest
            ? {
              busy: true,
              kind: "reply",
              label: "正在等待 AI 回复",
              operation_id: activeRequest.operation_id,
              message_id: activeRequest.message_id,
              started_at: sentAt,
            }
            : { busy: false },
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
  const workspaceMeta = page.locator(
    `[data-workspace-id="${workspaceId}"] .workspace-meta`,
  );
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

  await expect(userRow).toContainText("发送中");
  await expect(page.locator(".message-row.assistant.pending")).toHaveCount(0);
  await expect(page.locator("#chatSendButton")).toHaveAttribute("aria-label", "取消发送");
  await expect(workspaceMeta).toHaveText("正在发送消息");

  acceptedMessageId = userId;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(userRow).toContainText("已发送");
  await expect(page.locator(".message-row.assistant.pending"))
    .toContainText("正在确认需求并整理最终提示词");
  await expect(page.locator("#chatSendButton")).toHaveAttribute("aria-label", "取消等待");
  await expect(workspaceMeta).toHaveText("正在确认需求并整理最终提示词");
  await expect(userRow.getByRole("button", { name: "重新发送" })).toBeDisabled();
  await expect(page.locator(".message-row.user", { hasText: content })).toHaveCount(1);

  releaseReply();
  await expect(page.locator('[data-message-id="' + assistantId + '"]'))
    .toContainText("已按要求调整风格");
  await expect(page.locator(".message-row.user", { hasText: content })).toHaveCount(1);

  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value) => { window.__copiedMessage = value; } },
    });
  });
  const assistantRow = page.locator('[data-message-id="' + assistantId + '"]');
  const copyButton = assistantRow.getByRole("button", { name: "复制消息" });
  await expect(copyButton).toBeVisible();
  await copyButton.click();
  await expect.poll(() => page.evaluate(() => window.__copiedMessage))
    .toBe("已按要求调整风格");
  await userRow.getByRole("button", { name: "复制消息" }).click();
  await expect.poll(() => page.evaluate(() => window.__copiedMessage)).toBe(content);
  const resendButton = userRow.getByRole("button", { name: "重新发送" });
  await expect(resendButton).toBeEnabled();
  await resendButton.click();
  await expect.poll(() => requests.length).toBe(3);
  expect(requests[2].message_id).not.toBe(userId);
  expect(requests[2].content).toBe(content);
  await expect(page.locator(".message-row.user", { hasText: content })).toHaveCount(2);

  function userMessage(body, complete = false, replyMessageId = assistantId) {
    return {
      id: body.message_id,
      role: "user",
      kind: "message",
      content: body.content,
      payload: complete ? { reply_message_id: replyMessageId } : {},
      created_at: sentAt,
      attachments: [],
    };
  }

  function assistantMessage(body, id = assistantId) {
    return {
      id,
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

test("server chat operation does not show an assistant before its message is stored", async ({
  studioPage: page,
}) => {
  const messageId = "b".repeat(32);
  let operationBusy = true;

  await page.route("**/api/workspaces/*/messages*", (route) => route.fulfill({
    json: {
      messages: [],
      conversation_operation: operationBusy
        ? {
          busy: true,
          kind: "reply",
          label: "正在等待 AI 回复",
          operation_id: "c".repeat(32),
          message_id: messageId,
        }
        : { busy: false },
    },
  }));

  await page.reload();
  await expect(page.locator("#workspaceList .workspace-item.active .workspace-meta"))
    .toHaveText("正在发送消息");
  await expect(page.locator(".message-row.assistant")).toHaveCount(0);

  operationBusy = false;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect(page.locator("#workspaceList .workspace-item.active .workspace-meta"))
    .not.toHaveText("正在发送消息");
  await expect(page.locator(".message-row.assistant")).toHaveCount(0);
});
