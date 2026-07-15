const path = require("node:path");
const { test, expect } = require("@playwright/test");

test("workspace lifecycle remains usable", async ({ page }, testInfo) => {
  await page.goto("/login");
  await page.getByLabel("用户名").fill("e2e-admin");
  await page.getByLabel("密码").fill("E2eStrongPass123!");
  await page.getByRole("button", { name: "进入工作台" }).click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator("#workspaceList .workspace-item").first()).toBeVisible();

  const suffix = `${testInfo.project.name}-${Date.now()}`;
  const createdName = `E2E-${suffix}`;
  const renamedName = `E2E-Renamed-${suffix}`;

  await page.locator("#newWorkspaceButton").click();
  await page.locator("#workspaceNameInput").fill(createdName);
  await page.locator('#workspaceForm button[type="submit"]').click();
  await expect(page.locator("#workspaceTitle")).toHaveText(createdName);

  const chatInput = page.locator("#chatInput");
  await expect(chatInput).toBeEditable();
  await chatInput.fill("新工作站无需刷新即可输入");
  await expect(chatInput).toHaveValue("新工作站无需刷新即可输入");
  let promptDraftRequests = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/prompt-drafts")) {
      promptDraftRequests += 1;
    }
  });
  await page.locator("#directGenerationButton").click();
  await expect(page.locator("#generationForm")).toBeVisible();
  await expect(page.locator("#promptInput")).toHaveValue("新工作站无需刷新即可输入");
  expect(promptDraftRequests).toBe(0);
  await page.locator("#generationBackButton").click();
  await expect(chatInput).toHaveValue("新工作站无需刷新即可输入");

  await chatInput.blur();
  await page.keyboard.press("F2");
  await page.locator("#workspaceNameInput").fill(renamedName);
  await page.locator('#workspaceForm button[type="submit"]').click();
  await expect(page.locator("#workspaceTitle")).toHaveText(renamedName);

  const item = page.locator("#workspaceList .workspace-item", { hasText: renamedName });
  await item.locator("[data-delete-workspace]").click();
  await expect(page.locator("#workspaceDeleteDialog")).toBeVisible();
  await page.locator('#workspaceDeleteForm button[type="submit"]').click();
  await expect(page.locator("#workspaceList")).not.toContainText(renamedName);
});

test("animation workstation only generates frames from a user-selected master", async ({ page }, testInfo) => {
  await page.goto("/login");
  await page.getByLabel("用户名").fill("e2e-admin");
  await page.getByLabel("密码").fill("E2eStrongPass123!");
  await page.getByRole("button", { name: "进入工作台" }).click();

  const workspaceName = `E2E-Animation-${testInfo.project.name}-${Date.now()}`;
  await page.locator("#newWorkspaceButton").click();
  await page.locator("#workspaceNameInput").fill(workspaceName);
  await page.locator('#workspaceKindSwitch [data-workspace-kind="animation"]').click();
  await page.locator('#workspaceForm button[type="submit"]').click();

  await expect(page.locator("#directGenerationButtonLabel")).toHaveText("直接制作帧动画");
  await page.locator("#chatInput").fill("角色原地挥手，固定镜头，无缝循环。");
  await page.locator("#directGenerationButton").click();

  await expect(page.locator("#generationHeadingTitle")).toHaveText("确认帧动画参数");
  await expect(page.locator("#generationHeadingSubtitle")).toContainText("必须先添加并选择 1 张母图");
  await expect(page.locator("#modeSwitch")).toBeHidden();
  await expect(page.locator(".animation-control").first()).toBeVisible();
  await expect(page.locator("#referenceStrip")).toBeVisible();
  await expect(page.locator("#generateButtonLabel")).toHaveText("开始生成帧");
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "请先添加并选择一张母图");
  await expect(page.locator("body")).not.toContainText("生成母图");

  await page.locator("#referenceInput").setInputFiles(
    path.resolve("static/assets/starter-ocean-sky-reference.png"),
  );
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(1);
  await expect(page.locator("#referenceLimit")).toHaveText("1 / 1");
  await expect(page.locator("#modeSwitch")).toHaveAttribute("data-mode", "img2img");
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "");
  const master = page.locator("#referenceList [data-reference-toggle]");
  await master.click();
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "请先添加并选择一张母图");
  await master.click();
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "");

  await page.locator("#generationBackButton").click();
  const workspace = page.locator("#workspaceList .workspace-item", { hasText: workspaceName });
  await workspace.locator("[data-delete-workspace]").click();
  await page.locator('#workspaceDeleteForm button[type="submit"]').click();
});

test("active generation locks prompt reuse without trapping the composer", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("用户名").fill("e2e-admin");
  await page.getByLabel("密码").fill("E2eStrongPass123!");
  await page.getByRole("button", { name: "进入工作台" }).click();

  const workspace = page.locator("#workspaceList .workspace-item.active");
  await expect(workspace).toBeVisible();
  const workspaceId = await workspace.getAttribute("data-workspace-id");
  const createdAt = new Date().toISOString();
  const activeJob = {
    id: "e2e-running-job",
    workspace_id: workspaceId,
    status: "running",
    progress_percent: 35,
    queue_position: null,
    queue_total: 0,
    estimated_end_at: null,
    is_over_estimate: false,
    kind: "image",
    channel_id: "e2e",
    channel: "E2E 渠道",
    mode: "text2img",
    prompt: "正在生成的提示词",
    model: "e2e-model",
    size: "1024x1024",
    quality: "auto",
    output_format: "png",
    compression: null,
    transparent_background: false,
    animation_fps: null,
    animation_loop: null,
    animation_format: null,
    animation_duration_seconds: null,
    requested_count: 1,
    price_per_image_rmb: "0.0300",
    charged_rmb: "0.0000",
    reserved_rmb: "0.0300",
    created_at: createdAt,
    started_at: createdAt,
    completed_at: null,
    succeeded_count: 0,
    failed_count: 0,
    canceled_count: 0,
    can_cancel: true,
    can_retry: false,
    references: [],
    items: [],
    animation_url: null,
    animation_download_url: null,
  };

  await page.route("**/api/generations*", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/generations/active") {
      await route.fulfill({ json: { jobs: [activeJob] } });
      return;
    }
    if (url.pathname === "/api/generations") {
      await route.fulfill({ json: { jobs: [activeJob], queue_total: 0 } });
      return;
    }
    await route.continue();
  });
  await page.route("**/api/workspaces/*/messages?*", async (route) => {
    await route.fulfill({
      json: {
        messages: [{
          id: "e2e-prompt-draft",
          role: "assistant",
          kind: "prompt_draft",
          content: "需求确认",
          payload: {
            summary_zh: "已确认需求",
            prompt: "可复用的生图提示词",
            reference_ids: [],
            language: "zh",
          },
          provider_label: "E2E 助手",
          elapsed_seconds: 1,
          created_at: createdAt,
          attachments: [],
        }],
        total: 1,
        has_more: false,
        context: null,
        conversation_operation: { busy: false },
      },
    });
  });

  await page.reload();
  const reusePrompt = page.locator("[data-use-prompt-draft]");
  await expect(reusePrompt).toBeDisabled();

  await reusePrompt.evaluate((button) => {
    button.disabled = false;
    button.click();
  });
  await expect(page.locator("#generationForm")).toBeVisible();
  await expect(page.locator("#generationBackButton")).toBeEnabled();
  await page.locator("#generationBackButton").click();
  await expect(page.locator("#chatForm")).toBeVisible();
});
