const path = require("node:path");
const {
  closeGenerationComposer,
  createWorkspace,
  deleteWorkspace,
  expect,
  loginAsAdmin,
  mockConfiguredImageChannel,
  rectanglesOverlap,
  test,
  uploadLibraryImage,
} = require("./fixtures");

test("deleting the last workspace leaves the workspace list empty", {
  tag: "@responsive",
}, async ({ page }) => {
  await loginAsAdmin(page);
  const username = `e2e-empty-${Date.now()}`;
  const password = "E2eEmptyPass123!";
  await page.evaluate(async ({ username: name, password: secret }) => {
    await window.ImageGen.api("/api/admin/users", {
      method: "POST",
      body: { username: name, password: secret },
    });
  }, { username, password });
  await page.getByRole("button", { name: "退出登录" }).click();
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator("#workspaceList .workspace-item").first()).toBeVisible();

  const workspaceNames = await page.locator("#workspaceList .workspace-copy strong").allTextContents();
  expect(workspaceNames.length).toBeGreaterThan(0);
  for (const name of workspaceNames) await deleteWorkspace(page, name);

  await expect(page.locator("#workspaceList .workspace-item")).toHaveCount(0);
  await expect(page.locator("#workspaceCount")).toHaveText(/^0 \/ /);
  await expect(page.locator("#workspaceTitle")).toHaveText("暂无工作站");
  await expect(page.locator("#chatForm")).toBeHidden();
  await expect(page.locator("#chatInput")).toBeDisabled();

  await page.reload();
  await expect(page.locator("#workspaceList .workspace-item")).toHaveCount(0);
  await expect(page.locator("#workspaceTitle")).toHaveText("暂无工作站");

  await createWorkspace(page, "重新开始");
  await expect(page.locator("#workspaceList .workspace-item")).toHaveCount(1);
  await expect(page.locator("#chatInput")).toBeEnabled();
});

test("new workspace is interactive while history is delayed", async ({ studioPage: page }) => {
  await expect(page.locator("#chatInput")).toBeEditable();
  await page.evaluate(() => {
    window.__workspaceLockTransitions = 0;
    window.__workspaceLockObserver = new MutationObserver((records) => {
      window.__workspaceLockTransitions += records.filter((record) => (
        record.attributeName === "disabled" && record.oldValue === null
      )).length;
    });
    window.__workspaceLockObserver.observe(document.getElementById("chatInput"), {
      attributes: true,
      attributeFilter: ["disabled"],
      attributeOldValue: true,
    });
  });

  let releaseLoads;
  const loadsReleased = new Promise((resolve) => {
    releaseLoads = resolve;
  });
  await page.route("**/api/workspaces/*/messages?*", async (route) => {
    await loadsReleased;
    await route.continue();
  });

  const name = `E2E-Immediate-${Date.now()}`;
  try {
    await createWorkspace(page, name);
    await expect(page.locator("#chatInput")).toBeEditable({ timeout: 1000 });
    await expect(page.locator("#animationParametersButton")).toBeHidden();
    expect(await page.evaluate(() => {
      window.__workspaceLockObserver.disconnect();
      return window.__workspaceLockTransitions;
    })).toBe(0);
  } finally {
    releaseLoads();
  }

  await deleteWorkspace(page, name);
});

test("direct generation bypasses AI conversation", { tag: "@responsive" }, async ({
  studioPage: page,
}) => {
  await mockConfiguredImageChannel(page);
  await page.route("**/api/chat-models", (route) => route.fulfill({
    json: { version: "e2e-no-chat-models", models: [] },
  }));
  const workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");
  await page.evaluate(async ({ id, draftId }) => {
    await window.ImageGen.api("/api/workspaces/" + id, {
      method: "PATCH",
      body: { settings: { prompt_draft_id: draftId } },
    });
  }, { id: workspaceId, draftId: "d".repeat(32) });
  await page.reload();

  let aiRequests = 0;
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() === "POST"
      && (pathname.includes("/messages") || pathname.includes("/prompt-drafts"))) {
      aiRequests += 1;
    }
  });
  await page.route("**/api/generations", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ status: 409, json: { error: "E2E 已接收直接生成请求" } });
      return;
    }
    await route.continue();
  });

  await expect(page.locator("#chatSendButton")).toBeDisabled();
  await expect(page.locator("#directGenerationButton")).toBeVisible();
  await expect(page.locator("#directGenerationButton")).toBeEnabled();
  await page.locator("#chatInput").fill("一张雨夜霓虹街道的电影感照片");
  const settingsSaved = page.waitForResponse((response) => (
    response.request().method() === "PATCH"
      && new URL(response.url()).pathname === "/api/workspaces/" + workspaceId
  ));
  await page.locator("#directGenerationButton").click();

  await expect(page.locator("#generationForm")).toBeVisible();
  await expect(page.locator("#promptInput"))
    .toHaveValue("一张雨夜霓虹街道的电影感照片");
  await expect(page.locator("#promptReviewStatus")).toContainText("可直接编辑提示词");
  await page.locator('#modeSwitch [data-mode="text2img"]').click();
  await settingsSaved;
  const savedPromptDraftId = await page.evaluate(async (id) => {
    const data = await window.ImageGen.api("/api/workspaces");
    return data.workspaces.find((workspace) => workspace.id === id).settings.prompt_draft_id;
  }, workspaceId);
  expect(savedPromptDraftId).toBe("");

  await page.reload();
  await expect(page.locator("#promptReviewStatus")).toContainText("可直接编辑提示词");
  await expect(page.locator("#generationForm")).toBeHidden();
  await page.locator("#chatInput").fill("一张雨夜霓虹街道的电影感照片");
  await page.locator("#directGenerationButton").click();
  await expect(page.locator("#generationForm")).toBeVisible();

  const generationRequest = page.waitForRequest((request) => (
    request.method() === "POST" && new URL(request.url()).pathname === "/api/generations"
  ));
  await page.locator("#generateButton").click();
  const body = (await generationRequest).postDataJSON();
  expect(body.prompt).toBe("一张雨夜霓虹街道的电影感照片");
  expect(body.prompt_draft_id).toBe("");
  expect(aiRequests).toBe(0);
});

test("workspace lifecycle remains usable", { tag: "@responsive" }, async ({
  studioPage: page,
}, testInfo) => {
  await mockConfiguredImageChannel(page);
  await page.reload();
  await expect(page.locator("#workspaceList .workspace-item").first()).toBeVisible();

  const suffix = `${testInfo.project.name}-${Date.now()}`;
  const createdName = `E2E-${suffix}`;
  const renamedName = `E2E-Renamed-${suffix}`;

  await createWorkspace(page, createdName);

  await page.locator("#libraryButton").click();
  await expect(page.locator("#libraryDialog")).toBeVisible();
  await expect(page.locator("#libraryTargetLabel")).toHaveText("随消息发送");
  await uploadLibraryImage(page, path.resolve("static/assets/starter-ocean-sky-reference.png"));
  const libraryImage = page.locator("#libraryGrid .library-card", {
    hasText: "starter-ocean-sky-reference.png",
  }).first();
  await expect(libraryImage).toBeVisible();
  await libraryImage.locator("[data-use-library-image]").click();
  await expect(page.locator("#libraryDialog")).toBeHidden();
  await expect(page.locator("#chatReferenceCount")).toHaveText("1");

  const chatInput = page.locator("#chatInput");
  await expect(chatInput).toBeEditable();
  await chatInput.fill("新工作站无需刷新即可输入");
  await expect(chatInput).toHaveValue("新工作站无需刷新即可输入");
  await expect(page.locator("#animationParametersButton")).toBeHidden();
  await expect(page.locator("#promptGalleryLink"))
    .toHaveAttribute("href", "https://gpt-image2.canghe.ai/");
  await expect(page.locator("#promptGalleryLink")).toHaveAttribute("target", "_blank");

  await chatInput.blur();
  await page.keyboard.press("F2");
  await page.locator("#workspaceNameInput").fill(renamedName);
  await page.locator('#workspaceForm button[type="submit"]').click();
  await expect(page.locator("#workspaceTitle")).toHaveText(renamedName);

  await deleteWorkspace(page, renamedName);
});

test("composer close fallback keeps workspace switching interactive", {
  tag: "@responsive",
}, async ({ studioPage: page }, testInfo) => {
  const workspaceName = `E2E-Close-${testInfo.project.name}-${Date.now()}`;
  await createWorkspace(page, workspaceName, "animation");
  const target = page.locator("#workspaceList .workspace-item:not(.active)").first();
  const targetName = await target.locator(".workspace-copy strong").textContent();

  await page.locator("#animationParametersButton").click();
  await expect(page.locator("#generationForm")).toBeVisible();
  await page.locator("#generationForm").evaluate((form) => {
    form.style.animationName = "none";
  });
  await closeGenerationComposer(page);

  await expect(page.locator("#generationBackdrop")).toBeHidden({ timeout: 100 });
  await target.locator("[data-select-workspace]").click({ timeout: 1000 });
  await expect(page.locator("#workspaceTitle")).toHaveText(targetName);
  await expect(page.locator("#generationForm")).toBeHidden();

  await page.locator("#workspaceList .workspace-item", { hasText: workspaceName })
    .locator("[data-select-workspace]").click();
  await deleteWorkspace(page, workspaceName);
});

test("active generation locks prompt reuse and cancellation unlocks immediately", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
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
    quality: "high",
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
  const canceledJob = {
    ...activeJob,
    status: "canceled",
    progress_percent: 100,
    reserved_rmb: "0.0000",
    completed_at: createdAt,
    canceled_count: 1,
    can_cancel: false,
  };
  let canceled = false;

  await page.route(`**/api/generations/${activeJob.id}/cancel`, async (route) => {
    canceled = true;
    await route.fulfill({ json: { job: canceledJob } });
  });
  await page.route("**/api/generations*", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/generations/active") {
      await route.fulfill({ json: { jobs: canceled ? [] : [activeJob] } });
      return;
    }
    if (url.pathname === "/api/generations") {
      await route.fulfill({ json: { jobs: [canceled ? canceledJob : activeJob], queue_total: 0 } });
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
  if (page.viewportSize().width >= 640) {
    await expect(page.locator("#generationBackButton")).toBeHidden();
  } else {
    await expect(page.locator("#generationBackButton")).toBeEnabled();
  }
  await closeGenerationComposer(page);
  await expect(page.locator("#chatForm")).toBeVisible();

  const jobCard = page.locator(`[data-job-id="${activeJob.id}"]`);
  await expect(jobCard.locator("[data-job-status-label]")).toHaveText("生成中");
  await jobCard.getByRole("button", { name: "取消" }).click();
  await expect(jobCard.locator("[data-job-status-label]")).toHaveText("已取消");
  await expect(jobCard.getByRole("button", { name: "取消" })).toBeHidden();
  await expect(reusePrompt).toBeEnabled();
  await expect(page.locator("#chatInput")).toBeEditable();
  await expect(page.getByText("取消中", { exact: true })).toHaveCount(0);
});

test("failed generation shows the provider reason once", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");
  const createdAt = new Date().toISOString();
  const reason = "渠道错误：请上传需要转换的照片";
  const failedJob = {
    id: "e2e-failed-job",
    workspace_id: workspaceId,
    status: "failed",
    progress_percent: 100,
    queue_position: null,
    queue_total: 0,
    estimated_end_at: null,
    is_over_estimate: false,
    kind: "image",
    channel: "E2E 渠道",
    prompt: "把这张照片转换成涂鸦插画",
    model: "e2e-image",
    size: "1024x1024",
    quality: "low",
    requested_count: 2,
    charged_rmb: "0.0000",
    created_at: createdAt,
    succeeded_count: 0,
    can_cancel: false,
    can_retry: false,
    transparent_background: false,
    animation_url: null,
    items: [0, 1].map((position) => ({
      id: `e2e-failed-item-${position}`,
      position,
      status: "failed",
      error: reason,
      image_url: null,
      thumbnail_url: null,
    })),
  };

  await page.route("**/api/generations*", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/generations/active") {
      await route.fulfill({ json: { jobs: [] } });
      return;
    }
    if (url.pathname === "/api/generations") {
      await route.fulfill({ json: { jobs: [failedJob], queue_total: 0 } });
      return;
    }
    await route.continue();
  });

  await page.reload();
  const jobCard = page.locator(`[data-job-id="${failedJob.id}"]`);
  await expect(jobCard.locator("[data-job-error]")).toBeVisible();
  await expect(jobCard.locator("[data-job-error-message]")).toHaveText(reason);
  await expect(jobCard.getByText(reason, { exact: true })).toHaveCount(1);
});

test("latest toast does not cover the generation composer", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const workspaceName = `E2E-Toast-Animation-${Date.now()}`;
  await createWorkspace(page, workspaceName, "animation");
  await page.locator("#animationParametersButton").click();
  await expect(page.locator("#generationForm")).toBeVisible();

  await page.evaluate(() => {
    window.ImageGen.toast("较早的提示", "info");
    window.ImageGen.toast("图库原图累计不能超过 2 GiB", "error");
  });
  const toast = page.locator("#toastRegion .toast");
  await expect(toast).toHaveCount(1);
  await expect(toast).toContainText("图库原图累计不能超过 2 GiB");

  const toastBox = await toast.boundingBox();
  const composerBox = await page.locator("#generationForm").boundingBox();
  expect(toastBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  expect(rectanglesOverlap(toastBox, composerBox)).toBe(false);
  await closeGenerationComposer(page);
  await deleteWorkspace(page, workspaceName);
});
