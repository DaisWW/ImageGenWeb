const path = require("node:path");
const {
  closeGenerationComposer,
  createWorkspace,
  deleteWorkspace,
  expect,
  loginAsAdmin,
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
    await expect(page.locator("#directGenerationButton")).toBeEnabled({ timeout: 1000 });
    expect(await page.evaluate(() => {
      window.__workspaceLockObserver.disconnect();
      return window.__workspaceLockTransitions;
    })).toBe(0);
  } finally {
    releaseLoads();
  }

  await deleteWorkspace(page, name);
});

test("workspace lifecycle remains usable", { tag: "@responsive" }, async ({
  studioPage: page,
}, testInfo) => {
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
  let promptDraftRequests = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/prompt-drafts")) {
      promptDraftRequests += 1;
    }
  });
  await page.locator("#directGenerationButton").click();
  await expect(page.locator("#generationForm")).toBeVisible();
  await expect(page.locator("#promptInput")).toHaveValue("新工作站无需刷新即可输入");
  await expect(page.locator("#qualitySelect")).toHaveCount(0);
  expect(promptDraftRequests).toBe(0);
  if (page.viewportSize().width >= 640) {
    await expect(page.locator("#generationBackButton")).toBeHidden();
    await closeGenerationComposer(page);
    await expect(page.locator("#generationForm")).toHaveClass(/is-closing/);
    await expect(page.locator("#generationForm")).toHaveCSS("animation-name", "generation-composer-out");
    await expect(page.locator("#generationForm")).toBeHidden();
    await page.locator("#directGenerationButton").click();
    await page.keyboard.press("Escape");
    await expect(page.locator("#generationForm")).toBeHidden();
    await page.locator("#directGenerationButton").click();
  } else {
    await expect(page.locator("#generationBackButton")).toBeVisible();
  }
  await closeGenerationComposer(page);
  await expect(chatInput).toHaveValue("新工作站无需刷新即可输入");

  await chatInput.blur();
  await page.keyboard.press("F2");
  await page.locator("#workspaceNameInput").fill(renamedName);
  await page.locator('#workspaceForm button[type="submit"]').click();
  await expect(page.locator("#workspaceTitle")).toHaveText(renamedName);

  await deleteWorkspace(page, renamedName);
});

test("animation workstation only generates frames from a user-selected master", async ({
  studioPage: page,
}, testInfo) => {
  const workspaceName = `E2E-Animation-${testInfo.project.name}-${Date.now()}`;
  await createWorkspace(page, workspaceName, "animation");

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

  await page.locator("#referenceLibrary").click();
  await expect(page.locator("#libraryTargetLabel")).toHaveText("设为母图");
  await uploadLibraryImage(page, path.resolve("static/assets/starter-ocean-sky-reference.png"));
  await page.locator("#libraryGrid [data-use-library-image]").first().click();
  await expect(page.locator("#libraryDialog")).toBeHidden();
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(1);
  await expect(page.locator("#referenceLimit")).toHaveText("1 / 1");
  await expect(page.locator("#modeSwitch")).toHaveAttribute("data-mode", "img2img");
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "");
  const master = page.locator("#referenceList [data-reference-toggle]");
  await master.click();
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "请先添加并选择一张母图");
  await master.click();
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "");

  await master.click();
  await closeGenerationComposer(page);
  await page.locator("#directGenerationButton").click();
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(1);
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "");

  await closeGenerationComposer(page);
  await deleteWorkspace(page, workspaceName);
});

test("image library keeps the selected animation master when chat has an attachment", async ({
  studioPage: page,
}, testInfo) => {
  const workspaceName = `E2E-Library-${testInfo.project.name}-${Date.now()}`;
  await createWorkspace(page, workspaceName, "animation");

  await page.locator("#chatReferenceButton").click();
  await page.locator('[data-open-library="chat"]').click();
  await expect(page.locator("#libraryTargetLabel")).toHaveText("随消息发送");
  await uploadLibraryImage(page, path.resolve("static/assets/brand-mark-v2.png"));
  const chatImage = page.locator("#libraryGrid .library-card", { hasText: "brand-mark-v2.png" });
  await expect(chatImage).toHaveCount(1);
  await chatImage.locator(".library-use").click();
  await expect(page.locator("#chatReferenceCount")).toHaveText("1");

  await page.locator("#libraryButton").click();
  await expect(page.locator("#libraryTargetLabel")).toHaveText("设为母图");
  await uploadLibraryImage(page, path.resolve("static/assets/starter-ocean-sky-reference.png"));
  const master = page.locator("#libraryGrid .library-card", { hasText: "starter-ocean" });
  await master.locator(".library-use").click();

  await expect(page.locator("#generationForm")).toBeVisible();
  await expect(page.locator("#referenceList .reference-card.selected img"))
    .toHaveAttribute("alt", "starter-ocean-sky-reference.png");
  await expect(page.locator("#referenceLimit")).toHaveText("1 / 1");
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "");

  await closeGenerationComposer(page);
  await deleteWorkspace(page, workspaceName);
});

test("active generation locks prompt reuse without trapping the composer", {
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
  if (page.viewportSize().width >= 640) {
    await expect(page.locator("#generationBackButton")).toBeHidden();
  } else {
    await expect(page.locator("#generationBackButton")).toBeEnabled();
  }
  await closeGenerationComposer(page);
  await expect(page.locator("#chatForm")).toBeVisible();
});

test("image library exposes retry after its initial load fails", async ({ studioPage: page }) => {
  let attempts = 0;
  await page.route("**/api/library-images?*", async (route) => {
    attempts += 1;
    if (attempts === 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "图库暂时不可用", code: "library_unavailable" }),
      });
      return;
    }
    await route.fulfill({
      json: {
        images: [],
        total: 0,
        has_more: false,
        max_count: 200,
        max_bytes: 2147483648,
      },
    });
  });

  await page.locator("#libraryButton").click();
  await expect(page.locator("#libraryError")).toBeVisible();
  await expect(page.locator("#libraryUploadButton")).toBeDisabled();
  await page.locator("#libraryRetryButton").click();

  await expect(page.locator("#libraryEmpty")).toBeVisible();
  await expect(page.locator("#libraryUploadButton")).toBeEnabled();
  expect(attempts).toBe(2);
});

test("image library pagination keeps its server offset after merging a duplicate", async ({
  studioPage: page,
}) => {
  const image = (index) => ({
    id: `library-${index}`,
    name: `library-${index}.png`,
    url: "/static/assets/brand-mark-v2.png",
    thumbnail_url: "/static/assets/brand-mark-v2.png",
  });
  const images = Array.from({ length: 61 }, (_value, index) => image(index));
  const requestedOffsets = [];
  await page.route("**/api/library-images**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname !== "/api/library-images") {
      await route.continue();
      return;
    }
    if (request.method() === "POST") {
      await route.fulfill({ json: { images: [images[60]], added_count: 0 } });
      return;
    }
    const offset = Number(url.searchParams.get("offset") || 0);
    requestedOffsets.push(offset);
    const pageImages = offset === 0 ? images.slice(0, 60) : images.slice(offset);
    await route.fulfill({
      json: {
        images: pageImages,
        total: images.length,
        has_more: offset + pageImages.length < images.length,
        max_count: 200,
        max_bytes: 2147483648,
      },
    });
  });

  await page.locator("#libraryButton").click();
  await expect(page.locator("#libraryGrid .library-card")).toHaveCount(60);
  await uploadLibraryImage(page, path.resolve("static/assets/brand-mark-v2.png"));
  await expect(page.locator("#libraryGrid .library-card")).toHaveCount(61);
  await expect(page.locator("#libraryLoadMoreButton")).toBeVisible();
  await page.locator("#libraryLoadMoreButton").click();

  await expect(page.locator("#libraryPagination")).toBeHidden();
  expect(requestedOffsets).toEqual([0, 60]);
});

test("latest toast does not cover the generation composer", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  await page.locator("#directGenerationButton").click();
  await expect(page.locator("#generationForm")).toBeVisible();

  await page.evaluate(() => {
    window.ImageGen.toast("较早的提示", "info");
    window.ImageGen.toast("图库原图累计不能超过 2 GiB", "error");
  });
  const toast = page.locator("#toastRegion .toast");
  const heading = page.locator("#generationForm .generation-composer-heading");
  await expect(toast).toHaveCount(1);
  await expect(toast).toContainText("图库原图累计不能超过 2 GiB");

  const toastBox = await toast.boundingBox();
  const headingBox = await heading.boundingBox();
  expect(toastBox).not.toBeNull();
  expect(headingBox).not.toBeNull();
  expect(toastBox.y + toastBox.height).toBeLessThanOrEqual(headingBox.y);
});
