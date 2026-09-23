const {
  expect,
  mockConfiguredChatModel,
  mockConfiguredImageChannel,
  test,
} = require("./fixtures");

test("image detail actions explain their purpose", async ({ studioPage: page }) => {
  const titles = await page.locator("#imageDialog .detail-actions [title]").evaluateAll(
    (actions) => Object.fromEntries(actions.map((action) => [action.id, action.title])),
  );

  expect(titles).toEqual({
    detailUiKit: "把完整游戏界面作为结构和风格参考，先拆解组件树，再逐个重建可开发的原子资源。",
    detailSlice: "识别规则排列的图集网格；确认行列和切片后，可下载或存入图库。",
    detailBackgroundRemoval: "使用一个或多个模型生成透明背景候选并比较结果。",
    detailSaveLibrary: "将当前生成图保存到工作站图库，便于以后作为参考图复用。",
    detailMaskEdit: "框选需要修改的区域，系统会自动生成蒙版并将当前图设为唯一垫图。",
    detailReuse: "将当前图加入参考图，并回到创作区描述需要改变的内容。",
    detailDownload: "下载当前生成结果的原始文件。",
  });
});

test("background removal compares parallel model results and selects the best", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");
  const createdAt = new Date().toISOString();
  const itemId = "e2e-background-removal-item";
  const runId = "e2e-background-removal-run";
  const lucidaResultId = "e2e-background-removal-lucida";
  const alternateResultId = "e2e-background-removal-alternate";
  const sourceUrl = "/static/assets/starter-ocean-sky-reference.png";
  const lucidaUrl = "/static/assets/brand-mark-v2.png";
  const alternateUrl = "/static/assets/starter-ocean-sky-reference.png";
  const completedJob = {
    id: "e2e-background-removal-job",
    workspace_id: workspaceId,
    status: "succeeded",
    progress_percent: 100,
    queue_position: null,
    queue_total: 0,
    estimated_end_at: null,
    is_over_estimate: false,
    kind: "image",
    channel_id: "e2e",
    channel: "E2E 渠道",
    mode: "text2img",
    prompt: "带纯色背景的商品图标",
    model: "e2e-image",
    size: "1024x1024",
    quality: "high",
    workflow: { generation_stage: "final" },
    output_format: "png",
    compression: 90,
    transparent_background: false,
    requested_count: 1,
    price_per_image_rmb: "0.0300",
    charged_rmb: "0.0300",
    reserved_rmb: "0.0000",
    created_at: createdAt,
    started_at: createdAt,
    completed_at: createdAt,
    succeeded_count: 1,
    failed_count: 0,
    canceled_count: 0,
    can_cancel: false,
    references: [],
    items: [{
      id: itemId,
      position: 0,
      status: "succeeded",
      progress_percent: 100,
      started_at: createdAt,
      completed_at: createdAt,
      estimated_seconds: 1,
      estimated_end_at: createdAt,
      elapsed_seconds: 1.1,
      charged_rmb: "0.0300",
      error: null,
      width: 512,
      height: 512,
      bytes: 2048,
      image_url: sourceUrl,
      thumbnail_url: sourceUrl,
      download_url: sourceUrl,
    }],
  };
  const models = [
    {
      id: "lucida",
      label: "Lucida",
      enabled: true,
      configured: true,
      model: "lucida",
      max_concurrency: 1,
    },
    {
      id: "alternate",
      label: "备选模型",
      enabled: true,
      configured: true,
      model: "alternate-v1",
      max_concurrency: 2,
    },
  ];
  const lucidaResult = {
    id: lucidaResultId,
    model_id: "lucida",
    model_label: "Lucida",
    status: "succeeded",
    selected: false,
    elapsed_seconds: 0.8,
    error: null,
    image_url: lucidaUrl,
    thumbnail_url: lucidaUrl,
    download_url: `/api/background-removal-results/${lucidaResultId}/download`,
  };
  const alternateRunning = {
    id: alternateResultId,
    model_id: "alternate",
    model_label: "备选模型",
    status: "running",
    selected: false,
    elapsed_seconds: null,
    error: null,
    image_url: null,
    thumbnail_url: null,
    download_url: null,
  };
  const alternateResult = {
    ...alternateRunning,
    status: "succeeded",
    elapsed_seconds: 1.6,
    image_url: alternateUrl,
    thumbnail_url: alternateUrl,
    download_url: `/api/background-removal-results/${alternateResultId}/download`,
  };
  const runningRun = {
    id: runId,
    status: "running",
    selected_result_id: null,
    results: [lucidaResult, alternateRunning],
  };
  const completedRun = {
    ...runningRun,
    status: "succeeded",
    results: [lucidaResult, alternateResult],
  };
  const selectedRun = {
    ...completedRun,
    selected_result_id: alternateResultId,
    results: [lucidaResult, { ...alternateResult, selected: true }],
  };
  let submittedModelIds = null;
  let submitted = false;
  let releaseCompleted;
  const completedAvailable = new Promise((resolve) => {
    releaseCompleted = resolve;
  });
  let selectedResultId = null;
  let transientPollFailures = 0;

  await page.route("**/api/generations*", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/generations/active") {
      await route.fulfill({ json: { jobs: [] } });
      return;
    }
    if (url.pathname === "/api/generations") {
      await route.fulfill({ json: { jobs: [completedJob], queue_total: 0 } });
      return;
    }
    await route.continue();
  });
  await page.route(`**/api/generation-items/${itemId}/background-removal`, async (route) => {
    if (route.request().method() === "POST") {
      submittedModelIds = route.request().postDataJSON().model_ids;
      submitted = true;
      await route.fulfill({ status: 202, json: { run: runningRun } });
      return;
    }
    if (!submitted) {
      await route.fulfill({ json: { models, run: null } });
      return;
    }
    if (transientPollFailures === 0) {
      transientPollFailures += 1;
      await route.fulfill({ status: 503, json: { error: "临时不可用" } });
      return;
    }
    await completedAvailable;
    await route.fulfill({ json: { models, run: completedRun } });
  });
  await page.route(`**/api/background-removal-results/${alternateResultId}/select`, async (route) => {
    selectedResultId = alternateResultId;
    await route.fulfill({ json: { run: selectedRun } });
  });

  await page.reload();
  await page.locator(`[data-item-id="${itemId}"]`).click();
  await page.locator("#detailBackgroundRemoval").click();
  await expect(page.locator("#backgroundRemovalDialog")).toBeVisible();
  const modelOptions = page.locator("#backgroundRemovalModelList .background-removal-model-option");
  await expect(modelOptions).toHaveCount(2);
  await expect(modelOptions.nth(0)).toContainText("Lucida");
  await expect(modelOptions.nth(0)).not.toContainText("推荐");
  await expect(modelOptions.nth(0).locator("input")).toBeChecked();
  await expect(modelOptions.nth(1).locator("input")).not.toBeChecked();
  await modelOptions.nth(1).locator("input").check();
  await expect(page.locator("#backgroundRemovalModelSummary")).toHaveText("已选择 2 个");

  await page.locator("#backgroundRemovalStart").click();
  await expect(page.locator("#backgroundRemovalResultSummary")).toHaveText("1 / 2 个完成");
  expect(submittedModelIds).toEqual(["lucida", "alternate"]);
  await expect(page.locator("#backgroundRemovalPreviewLabel")).toHaveText("Lucida");
  await expect(page.locator("#backgroundRemovalPreviewImage")).toHaveAttribute("src", lucidaUrl);
  await expect(page.locator("#backgroundRemovalResultList")).toContainText("处理中");

  await page.getByRole("button", { name: "白色背景" }).click();
  await expect(page.locator("#backgroundRemovalPreviewPane")).toHaveAttribute("data-background", "white");
  await page.getByRole("button", { name: "黑色背景" }).click();
  await expect(page.locator("#backgroundRemovalPreviewPane")).toHaveAttribute("data-background", "black");

  releaseCompleted();
  await expect(page.locator("#backgroundRemovalResultSummary")).toHaveText("2 / 2 个完成");
  await expect(page.locator("#backgroundRemovalStatus")).toHaveText("已完成");
  expect(transientPollFailures).toBe(1);
  await page.locator(`[data-background-removal-result="${alternateResultId}"]`).click();
  await expect(page.locator("#backgroundRemovalPreviewLabel")).toHaveText("备选模型");
  await expect(page.locator("#backgroundRemovalPreviewImage")).toHaveAttribute("src", alternateUrl);
  await expect(page.getByTitle("下载 备选模型 结果")).toHaveAttribute(
    "href",
    `/api/background-removal-results/${alternateResultId}/download`,
  );
  await expect(page.locator("#backgroundRemovalDownloadAll")).toHaveAttribute(
    "href",
    `/api/background-removal-runs/${runId}/download`,
  );

  await page.locator("#backgroundRemovalSelectBest").click();
  await expect(page.locator("#backgroundRemovalSelectionSummary"))
    .toHaveText("最佳结果：备选模型");
  await expect(page.locator("#backgroundRemovalResultList .background-removal-best"))
    .toHaveText("最佳");
  expect(selectedResultId).toBe(alternateResultId);

  await page.locator(`[data-background-removal-result="${lucidaResultId}"]`).click();
  await expect(page.locator("#backgroundRemovalPreviewImage")).toHaveAttribute("src", lucidaUrl);

  const layout = await page.evaluate(() => {
    const dialog = document.getElementById("backgroundRemovalDialog");
    const preview = document.getElementById("backgroundRemovalPreviewPane");
    const previewFrame = document.getElementById("backgroundRemovalPreview");
    const previewImage = document.getElementById("backgroundRemovalPreviewImage");
    const results = dialog.querySelector(".background-removal-results-pane");
    const selectBest = document.getElementById("backgroundRemovalSelectBest");
    const downloadAll = document.getElementById("backgroundRemovalDownloadAll");
    const box = dialog.getBoundingClientRect();
    const previewBox = preview.getBoundingClientRect();
    const previewFrameBox = previewFrame.getBoundingClientRect();
    const previewImageBox = previewImage.getBoundingClientRect();
    const resultsBox = results.getBoundingClientRect();
    const selectBox = selectBest.getBoundingClientRect();
    const downloadBox = downloadAll.getBoundingClientRect();
    const overlaps = (first, second) => (
      first.left < second.right && first.right > second.left
      && first.top < second.bottom && first.bottom > second.top
    );
    return {
      dialogFitsViewport: box.left >= 0 && box.right <= window.innerWidth
        && box.top >= 0 && box.bottom <= window.innerHeight,
      noHorizontalOverflow: dialog.scrollWidth <= dialog.clientWidth + 1,
      panesDoNotOverlap: !overlaps(previewBox, resultsBox),
      previewImageFits: previewImageBox.left >= previewFrameBox.left - 1
        && previewImageBox.right <= previewFrameBox.right + 1
        && previewImageBox.top >= previewFrameBox.top - 1
        && previewImageBox.bottom <= previewFrameBox.bottom + 1,
      actionsDoNotOverlap: !overlaps(selectBox, downloadBox),
      modelLabelsFit: [...document.querySelectorAll(".background-removal-model-option")]
        .every((option) => option.scrollWidth <= option.clientWidth + 1),
    };
  });
  expect(layout).toEqual({
    dialogFitsViewport: true,
    noHorizontalOverflow: true,
    panesDoNotOverlap: true,
    previewImageFits: true,
    actionsDoNotOverlap: true,
    modelLabelsFit: true,
  });
});

test("late reference response updates the original workspace cache", async ({
  studioPage: page,
}) => {
  const result = await page.evaluate(async () => {
    const workspace = { id: "original-workspace", assets: [] };
    let renders = 0;
    await window.ImageGenStudio.StudioApp.prototype.applyReferenceAsset.call({
      activeWorkspace: { id: "new-workspace" },
      renderWorkspaceList: () => { renders += 1; },
    }, { id: "late-asset" }, { workspace });
    return { assetIds: workspace.assets.map((asset) => asset.id), renders };
  });

  expect(result).toEqual({ assetIds: ["late-asset"], renders: 1 });
});

test("detail reference actions share one request without enabling unavailable actions", async ({
  studioPage: page,
}) => {
  const result = await page.evaluate(async () => {
    const originalApi = window.ImageGenStudio.UI.api;
    let release;
    let calls = 0;
    const pending = new Promise((resolve) => { release = resolve; });
    window.ImageGenStudio.UI.api = async () => {
      calls += 1;
      await pending;
      return { asset: { id: "shared-reference" } };
    };
    const detailReuse = document.createElement("button");
    const detailUiKit = document.createElement("button");
    const detailMaskEdit = document.createElement("button");
    const target = {
      detailItemId: "detail-item",
      detailJobId: "detail-job",
      activeWorkspace: { id: "workspace" },
      jobs: [],
      detailReferenceBusy: false,
      el: {
        detailReuse,
        detailUiKit,
        detailMaskEdit,
        imageDialog: document.createElement("dialog"),
      },
      applyReferenceAsset: async () => {},
    };
    try {
      const first = window.ImageGenStudio.StudioApp.prototype.useDetailAsReference.call(target);
      const second = window.ImageGenStudio.StudioApp.prototype.useDetailAsReference.call(target);
      const disabledDuringRequest = detailReuse.disabled
        && detailUiKit.disabled
        && detailMaskEdit.disabled;
      release();
      await Promise.all([first, second]);
      return {
        calls,
        disabledDuringRequest,
        eligibleActionsDisabledAfterRequest: detailReuse.disabled
          || detailUiKit.disabled,
        maskEditDisabledAfterRequest: detailMaskEdit.disabled,
      };
    } finally {
      window.ImageGenStudio.UI.api = originalApi;
    }
  });

  expect(result).toEqual({
    calls: 1,
    disabledDuringRequest: true,
    eligibleActionsDisabledAfterRequest: false,
    maskEditDisabledAfterRequest: true,
  });
});

test("mask editing skips a text-only channel that only advertises the mask flag", async ({
  studioPage: page,
}) => {
  const result = await page.evaluate(() => {
    const fallback = {
      id: "img2img-mask",
      capabilities: { supports_mask: true, modes: ["img2img"] },
    };
    const target = {
      currentChannel: () => ({
        id: "text-only-mask",
        capabilities: { supports_mask: true, modes: ["text2img"] },
      }),
      maskCapableChannels: () => [fallback],
      el: { channelSelect: { value: "text-only-mask" } },
      applyChannel: () => {},
    };
    const selected = window.ImageGenStudio.StudioApp.prototype.ensureMaskCapableChannel.call(target);
    return { id: selected?.id || "", selectedValue: target.el.channelSelect.value };
  });

  expect(result).toEqual({ id: "img2img-mask", selectedValue: "img2img-mask" });
});

test("local repaint creates a PNG mask and submits only its source image", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  await mockConfiguredImageChannel(page);
  const workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");
  const createdAt = new Date().toISOString();
  const itemId = "e2e-mask-item";
  const sourceUrl = "/static/assets/brand-mark-v2.png";
  const referenceAsset = {
    id: "e2e-mask-source",
    name: "mask-source.png",
    url: sourceUrl,
    thumbnail_url: sourceUrl,
    mime_type: "image/png",
    bytes: 2048,
    width: 512,
    height: 512,
    created_at: createdAt,
  };
  const completedJob = {
    id: "e2e-mask-source-job",
    workspace_id: workspaceId,
    status: "succeeded",
    progress_percent: 100,
    queue_position: null,
    queue_total: 0,
    estimated_end_at: null,
    is_over_estimate: false,
    kind: "image",
    channel_id: "e2e",
    channel: "E2E 渠道",
    mode: "text2img",
    prompt: "需要局部修改的品牌图",
    model: "e2e-image",
    size: "1024x1024",
    quality: "high",
    workflow: { generation_stage: "final" },
    output_format: "png",
    compression: 90,
    transparent_background: false,
    requested_count: 1,
    price_per_image_rmb: "0.0300",
    charged_rmb: "0.0300",
    reserved_rmb: "0.0000",
    created_at: createdAt,
    started_at: createdAt,
    completed_at: createdAt,
    succeeded_count: 1,
    failed_count: 0,
    canceled_count: 0,
    can_cancel: false,
    can_retry: false,
    has_mask: false,
    mask_target_asset_id: null,
    references: [],
    items: [{
      id: itemId,
      position: 0,
      status: "succeeded",
      progress_percent: 100,
      started_at: createdAt,
      completed_at: createdAt,
      estimated_seconds: 1,
      estimated_end_at: createdAt,
      elapsed_seconds: 1,
      charged_rmb: "0.0300",
      error: null,
      width: 512,
      height: 512,
      bytes: 2048,
      image_url: sourceUrl,
      thumbnail_url: sourceUrl,
      download_url: sourceUrl,
    }],
  };
  let submitted = null;

  await page.route("**/api/generations*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "POST" && url.pathname === "/api/generations") {
      const contentType = request.headers()["content-type"] || "";
      const multipart = await new Request(request.url(), {
        method: "POST",
        headers: { "Content-Type": contentType },
        body: request.postDataBuffer(),
      }).formData();
      const payload = JSON.parse(String(multipart.get("payload")));
      const mask = multipart.get("mask");
      const maskBytes = Buffer.from(await mask.arrayBuffer());
      submitted = {
        contentType,
        payload,
        mask: {
          name: mask.name,
          type: mask.type,
          size: mask.size,
          signature: maskBytes.subarray(0, 8).toString("hex"),
        },
      };
      await route.fulfill({
        status: 202,
        json: {
          job: {
            ...completedJob,
            id: "e2e-masked-submission",
            status: "canceled",
            progress_percent: 0,
            prompt: payload.prompt,
            mode: "img2img",
            model: payload.model,
            size: payload.size,
            quality: payload.quality,
            output_format: payload.output_format,
            compression: payload.compression,
            transparent_background: payload.transparent_background,
            requested_count: payload.batch_count,
            charged_rmb: "0.0000",
            succeeded_count: 0,
            canceled_count: payload.batch_count,
            has_mask: true,
            mask_target_asset_id: referenceAsset.id,
            references: [referenceAsset],
            items: [],
          },
        },
      });
      return;
    }
    if (url.pathname === "/api/generations/active") {
      await route.fulfill({ json: { jobs: [] } });
      return;
    }
    if (url.pathname === "/api/generations") {
      await route.fulfill({ json: { jobs: [completedJob], queue_total: 0 } });
      return;
    }
    await route.continue();
  });
  await page.route(`**/api/generation-items/${itemId}/reference`, (route) => route.fulfill({
    status: 201,
    json: { asset: referenceAsset },
  }));

  await page.reload();
  await page.locator(`[data-item-id="${itemId}"]`).click();
  await expect(page.locator("#detailMaskEdit")).toBeEnabled();
  await page.locator("#detailMaskEdit").click();
  await expect(page.locator("#maskEditorDialog")).toBeVisible();
  await expect(page.locator("#maskEditorLoading")).toBeHidden();

  const canvas = page.locator("#maskEditorCanvas");
  const bounds = await canvas.boundingBox();
  expect(bounds).not.toBeNull();
  await page.mouse.move(bounds.x + bounds.width * 0.25, bounds.y + bounds.height * 0.25);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width * 0.7, bounds.y + bounds.height * 0.7);
  await page.mouse.up();
  await expect(page.locator("#maskEditorStatus")).toContainText("已选择约");
  await expect(page.locator("#maskEditorApply")).toBeEnabled();
  await page.locator("#maskEditorApply").click();

  await expect(page.locator("#maskEditorDialog")).toBeHidden();
  await expect(page.locator("#generationForm")).toBeVisible();
  await expect(page.locator("#maskEditNotice")).toContainText("局部重绘");
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(1);
  await expect(page.locator("#referenceList .reference-card.selected img"))
    .toHaveAttribute("alt", referenceAsset.name);
  await page.locator("#promptInput").fill("将框选区域改为蓝色，保持其余内容不变");
  await page.locator("#generateButton").click();

  await expect.poll(() => submitted).not.toBeNull();
  expect(submitted.contentType).toContain("multipart/form-data; boundary=");
  expect(submitted.payload.prompt).toBe("将框选区域改为蓝色，保持其余内容不变");
  expect(submitted.payload.reference_ids).toEqual([referenceAsset.id]);
  expect(submitted.payload.mask_target_asset_id).toBe(referenceAsset.id);
  expect(submitted.mask).toEqual({
    name: "mask.png",
    type: "image/png",
    size: expect.any(Number),
    signature: "89504e470d0a1a0a",
  });
  expect(submitted.mask.size).toBeGreaterThan(100);
});

test("image detail keeps its reference through multi-turn refinement", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  await mockConfiguredImageChannel(page);
  const workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");
  const createdAt = new Date().toISOString();
  const jobId = "e2e-detail-job";
  const itemId = "e2e-detail-item";
  const imageUrl = "/static/assets/brand-mark-v2.png";
  const referenceAsset = {
    id: "e2e-result-reference",
    name: "result.png",
    url: imageUrl,
    thumbnail_url: imageUrl,
    mime_type: "image/png",
    bytes: 2048,
    width: 512,
    height: 512,
    created_at: createdAt,
  };
  const completedJob = {
    id: jobId,
    workspace_id: workspaceId,
    status: "succeeded",
    progress_percent: 100,
    queue_position: null,
    queue_total: 0,
    estimated_end_at: null,
    is_over_estimate: false,
    kind: "image",
    channel_id: "e2e",
    channel: "E2E 渠道",
    mode: "text2img",
    prompt: "银色运动鞋商业主视觉",
    model: "e2e-image",
    size: "1024x1024",
    quality: "high",
    workflow: {
      creative_direction_label: "商品与电商",
      template_label: "商品商业视觉",
      generation_stage: "final",
      canvas_resolution: "conversation",
    },
    output_format: "png",
    compression: 90,
    transparent_background: false,
    requested_count: 1,
    price_per_image_rmb: "0.0300",
    charged_rmb: "0.0300",
    reserved_rmb: "0.0000",
    created_at: createdAt,
    started_at: createdAt,
    completed_at: createdAt,
    succeeded_count: 1,
    failed_count: 0,
    canceled_count: 0,
    can_cancel: false,
    references: [],
    items: [{
      id: itemId,
      position: 0,
      status: "succeeded",
      progress_percent: 100,
      started_at: createdAt,
      completed_at: createdAt,
      estimated_seconds: 1,
      estimated_end_at: createdAt,
      elapsed_seconds: 1.2,
      charged_rmb: "0.0300",
      error: null,
      width: 512,
      height: 512,
      bytes: 2048,
      image_url: imageUrl,
      thumbnail_url: imageUrl,
      download_url: imageUrl,
    }],
  };

  await mockConfiguredChatModel(page);
  await page.route("**/api/generations*", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/generations/active") {
      await route.fulfill({ json: { jobs: [] } });
      return;
    }
    if (url.pathname === "/api/generations") {
      await route.fulfill({ json: { jobs: [completedJob], queue_total: 0 } });
      return;
    }
    await route.continue();
  });
  await page.route(`**/api/generation-items/${itemId}/reference`, (route) => route.fulfill({
    status: 201,
    json: { asset: referenceAsset },
  }));
  let chatRound = 0;
  const sentAttachmentIds = [];
  const sentGenerationReferenceIds = [];
  const sentClarificationReplyIds = [];
  let promptDraftRequests = 0;
  await page.route("**/api/workspaces/*/messages", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    chatRound += 1;
    const body = route.request().postDataJSON();
    sentAttachmentIds.push(body.attachment_ids);
    sentGenerationReferenceIds.push(body.generation_reference_ids);
    sentClarificationReplyIds.push(body.clarification_reply_to_id);
    const ready = chatRound === 2;
    const assistant = ready
      ? {
        id: "e2e-refine-prompt-draft",
        role: "assistant",
        kind: "prompt_draft",
        content: "需求确认",
        payload: {
          status: "ready",
          summary_zh: "保留原图主体和构图，只把背景改成纯白色。",
          prompt: "参考图 1 保持主体和构图不变，只将背景改为纯白色。",
          language: "zh",
          generation_mode: "img2img",
          reference_ids: [referenceAsset.id],
          creative_direction: "other",
          template_id: "custom",
          style_tags: [],
          scene_tags: [],
          selection_reason: "沿用上一张生成结果进行单点修改。",
          hard_checks: ["主体和构图不变", "背景为纯白色"],
          quality_hint: "medium",
          reply_to_message_id: body.message_id,
        },
        provider_label: "E2E 助手",
        created_at: createdAt,
        attachments: [referenceAsset],
      }
      : {
        id: `e2e-refine-assistant-${chatRound}`,
        role: "assistant",
        kind: "message",
        content: "已保留主体，背景还需要确认。",
        payload: {
          status: "needs_clarification",
          reference_ids: [referenceAsset.id],
          generation_mode: "img2img",
          reply_to_message_id: body.message_id,
        },
        provider_label: "E2E 助手",
        created_at: createdAt,
        attachments: [],
      };
    await route.fulfill({
      status: 201,
      json: {
        messages: [{
          id: body.message_id,
          role: "user",
          kind: "message",
          content: body.content,
          payload: {
            reply_message_id: assistant.id,
            generation_reference_ids: body.generation_reference_ids,
          },
          created_at: createdAt,
          attachments: chatRound === 1 ? [referenceAsset] : [],
        }, assistant],
        context: {
          compacted: false,
          estimated_context_tokens: 100,
          max_context_tokens: 32000,
        },
      },
    });
  });
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/prompt-drafts")) {
      promptDraftRequests += 1;
    }
  });

  await page.reload();
  await page.locator(`[data-item-id="${itemId}"]`).click();
  await expect(page.locator("#imageDialog")).toBeVisible();
  await expect(page.locator("#detailList")).toContainText("请求参数");
  await expect(page.locator("#detailList")).toContainText("实际图片");
  await expect(page.locator("#detailList")).toContainText("采用对话画幅");
  await expect(page.locator("#detailList")).toContainText("商品商业视觉");
  await page.locator("#detailImage").click();
  await expect(page.locator("#imageViewerDialog")).toBeVisible();
  await page.locator("#imageViewerZoomSlider").fill("2");
  await expect(page.locator("#imageViewerZoomLabel")).toHaveText("200%");
  await page.locator('#imageViewerDialog [data-close-dialog="imageViewerDialog"]').click();
  await expect(page.locator("#imageDialog")).toBeVisible();

  if (page.viewportSize().width > 920) {
    const layoutState = await page.evaluate(() => {
      const dialog = document.getElementById("imageDialog");
      const layout = dialog.querySelector(".image-dialog-layout");
      const preview = dialog.querySelector(".image-dialog-preview");
      const info = dialog.querySelector(".image-dialog-info");
      const scroller = dialog.querySelector(".image-dialog-scroll");
      const close = dialog.querySelector('[data-close-dialog="imageDialog"]');
      const download = document.getElementById("detailDownload");
      const before = preview.getBoundingClientRect();
      scroller.scrollTop = scroller.scrollHeight;
      const after = preview.getBoundingClientRect();
      const dialogBox = dialog.getBoundingClientRect();
      const closeBox = close.getBoundingClientRect();
      const downloadBox = download.getBoundingClientRect();
      return {
        dialogFitsViewport: dialogBox.height <= window.innerHeight * 0.9,
        bodyOverflow: getComputedStyle(scroller).overflowY,
        infoOverflow: getComputedStyle(info).overflowY,
        layoutOverflow: getComputedStyle(layout).overflowY,
        previewStayedFixed: Math.abs(before.top - after.top) < 1
          && Math.abs(before.bottom - after.bottom) < 1,
        closeVisible: closeBox.top >= dialogBox.top && closeBox.bottom <= dialogBox.bottom,
        actionsVisible: downloadBox.top >= dialogBox.top && downloadBox.bottom <= dialogBox.bottom,
      };
    });
    expect(layoutState).toEqual({
      dialogFitsViewport: true,
      bodyOverflow: "auto",
      infoOverflow: "hidden",
      layoutOverflow: "hidden",
      previewStayedFixed: true,
      closeVisible: true,
      actionsVisible: true,
    });
  }

  const uiKitSize = await page.locator("#sizeInput").inputValue();
  await page.locator("#detailUiKit").click();
  await expect(page.locator("#imageDialog")).toBeHidden();
  await expect(page.locator("#chatInput")).toHaveValue(/不要抠取、分割或复制原图像素/);
  await expect(page.locator("#chatInput")).toHaveValue(/模块 → 原子资源/);
  await expect(page.locator("#chatReferenceCount")).toHaveText("1");
  await expect(page.locator("#modeSwitch")).toHaveCount(0);
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(1);
  await expect(page.locator("#creativeDirectionSelect")).toHaveValue("game_ui");
  await expect(page.locator("#formatSelect")).toHaveValue("png");
  await expect(page.locator("#sizeInput")).toHaveValue(uiKitSize);
  await expect(page.locator("#transparentBackground")).toBeDisabled();
  await expect(page.locator("#batchCount")).toHaveValue("1");

  await page.locator(`[data-item-id="${itemId}"]`).click();
  await page.locator("#detailReuse").click();
  await expect(page.locator("#imageDialog")).toBeHidden();
  await expect(page.locator("#chatInput")).toHaveValue("请基于这张图继续调整：");
  await expect(page.locator("#chatReferenceCount")).toHaveText("1");

  await page.locator("#chatForm").evaluate((form) => form.requestSubmit());
  await expect(page.locator(".message-row.assistant", { hasText: "背景还需要确认" }))
    .toBeVisible();
  await page.getByRole("button", { name: "继续回答" }).click();
  await page.locator("#chatInput").fill("背景使用纯白色，可以生成了。");
  await page.locator("#chatForm").evaluate((form) => form.requestSubmit());

  const draft = page.locator(".prompt-draft-content").last();
  await expect(draft).toContainText("只把背景改成纯白色");
  expect(sentAttachmentIds).toEqual([[referenceAsset.id], []]);
  expect(sentGenerationReferenceIds).toEqual([[referenceAsset.id], [referenceAsset.id]]);
  expect(sentClarificationReplyIds).toEqual(["", "e2e-refine-assistant-1"]);
  expect(promptDraftRequests).toBe(0);
  await draft.getByRole("button", { name: "使用此提示词生图" }).click();
  await expect(page.locator("#referenceList .reference-card.selected img"))
    .toHaveAttribute("alt", referenceAsset.name);
});

test("smart slicer detects, adjusts, selects and exports atlas tiles", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");
  const createdAt = new Date().toISOString();
  const itemId = "e2e-slice-item";
  const imageUrl = "/static/assets/starter-ocean-sky-reference.png";
  const completedJob = {
    id: "e2e-slice-job",
    workspace_id: workspaceId,
    status: "succeeded",
    progress_percent: 100,
    queue_position: null,
    queue_total: 0,
    estimated_end_at: null,
    is_over_estimate: false,
    kind: "image",
    channel_id: "e2e",
    channel: "E2E 渠道",
    mode: "text2img",
    prompt: "六张素材，2×3 规则图集",
    model: "e2e-image",
    size: "248x137",
    quality: "high",
    workflow: {},
    output_format: "png",
    compression: 90,
    transparent_background: false,
    requested_count: 1,
    price_per_image_rmb: "0.0300",
    charged_rmb: "0.0300",
    reserved_rmb: "0.0000",
    created_at: createdAt,
    started_at: createdAt,
    completed_at: createdAt,
    succeeded_count: 1,
    failed_count: 0,
    canceled_count: 0,
    can_cancel: false,
    references: [],
    items: [{
      id: itemId,
      position: 0,
      status: "succeeded",
      progress_percent: 100,
      started_at: createdAt,
      completed_at: createdAt,
      estimated_seconds: 1,
      estimated_end_at: createdAt,
      elapsed_seconds: 1,
      charged_rmb: "0.0300",
      error: null,
      width: 248,
      height: 137,
      bytes: 2048,
      image_url: imageUrl,
      thumbnail_url: imageUrl,
      download_url: imageUrl,
    }],
  };
  const analysis = {
    width: 248,
    height: 137,
    detected: true,
    confidence: "high",
    rows: 2,
    columns: 3,
    boxes: [],
  };
  const exported = [];
  const referenceAsset = {
    id: "e2e-slice-reference",
    name: "slice_01_83x69.png",
    url: imageUrl,
    mime_type: "image/png",
    bytes: 512,
    width: 83,
    height: 69,
    position: 0,
  };

  await page.route("**/api/generations*", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/generations/active") {
      await route.fulfill({ json: { jobs: [] } });
      return;
    }
    if (url.pathname === "/api/generations") {
      await route.fulfill({ json: { jobs: [completedJob], queue_total: 0 } });
      return;
    }
    await route.continue();
  });
  let analysisAttempts = 0;
  let releaseFirstAnalysis;
  let markFirstAnalysisStarted;
  const firstAnalysisStarted = new Promise((resolve) => {
    markFirstAnalysisStarted = resolve;
  });
  await page.route(
    "**/api/generation-items/" + itemId + "/slice-analysis",
    async (route) => {
      analysisAttempts += 1;
      if (analysisAttempts === 1) {
        markFirstAnalysisStarted();
        await new Promise((resolve) => {
          releaseFirstAnalysis = resolve;
        });
        await route.fulfill({
          status: 503,
          json: { error: "E2E 延迟的切图分析失败", code: "slice_analysis_failed" },
        });
        return;
      }
      await route.fulfill({ json: { analysis } });
    },
  );
  await page.route("**/api/generation-items/" + itemId + "/slice-export", async (route) => {
    const body = route.request().postDataJSON();
    exported.push(body);
    if (body.action === "download") {
      await route.fulfill({ contentType: "application/zip", body: "PK E2E slice archive" });
      return;
    }
    if (body.action === "library") {
      await route.fulfill({
        status: 201,
        json: { images: [], added_count: body.boxes.length },
      });
      return;
    }
    await route.fulfill({ status: 201, json: { asset: referenceAsset } });
  });

  await page.reload();
  await page.locator("[data-item-id=\"" + itemId + "\"]").click();
  await expect(page.locator(".image-dialog-preview"))
    .toHaveCSS("background-image", /conic-gradient/);
  await expect(page.locator(".image-dialog-preview"))
    .toHaveCSS("padding", /^(12|16)px$/);
  await expect(page.locator("#detailImage")).toHaveCSS("outline-style", "solid");
  await page.locator("#detailSlice").click();
  await firstAnalysisStarted;

  await expect(page.locator("#sliceDialog")).toBeVisible();
  await page.locator('[data-close-dialog="sliceDialog"]').click();
  const failedAnalysis = page.waitForResponse((response) => (
    response.status() === 503
      && new URL(response.url()).pathname.endsWith("/slice-analysis")
  ));
  releaseFirstAnalysis();
  await failedAnalysis;
  await expect(page.locator("#detailSlice")).toBeEnabled();
  await expect(page.locator("#sliceDialog")).toBeHidden();
  await expect(page.locator("#imageDialog")).toBeHidden();

  await page.locator("[data-item-id=\"" + itemId + "\"]").click();
  await page.locator("#detailSlice").click();

  await expect(page.locator("#sliceDialog")).toBeVisible();
  await expect(page.locator("#sliceCanvas"))
    .toHaveCSS("background-image", /conic-gradient/);
  await expect(page.locator("#slicePreviewTitle")).toHaveText("2 行 × 3 列");
  await expect(page.locator("#sliceConfidence")).toHaveText("高置信度");
  await expect(page.locator("#sliceOverlay .slice-box")).toHaveCount(6);
  await expect(page.locator("#sliceList .slice-list-item.selected")).toHaveCount(6);
  await expect(page.locator("#sliceSelectionSummary")).toHaveText("已选择 6 / 6 个切片");
  await expect(page.locator("#sliceMarginX, #sliceMarginY, #sliceGapX, #sliceGapY"))
    .toHaveCount(0);
  await expect(page.locator("#sliceReuse")).toBeDisabled();

  await page.locator("#sliceRows").fill("8");
  await page.locator("#sliceColumns").fill("8");
  await expect(page.locator("#sliceList .slice-list-item")).toHaveCount(64);
  const scrollOverflow = await page.locator("#sliceDialog").evaluate((dialog) => {
    const layout = dialog.querySelector(".slice-dialog-layout");
    const list = dialog.querySelector("#sliceList");
    return {
      dialog: dialog.scrollHeight - dialog.clientHeight,
      layout: layout.scrollHeight - layout.clientHeight,
      list: list.scrollHeight - list.clientHeight,
    };
  });
  expect(scrollOverflow.dialog).toBeLessThanOrEqual(1);
  expect(scrollOverflow.layout).toBeLessThanOrEqual(1);
  expect(scrollOverflow.list).toBeGreaterThan(1);
  await page.locator("#sliceReset").click();

  await page.locator("#sliceColumns").fill("2");
  await expect(page.locator("#sliceOverlay .slice-box")).toHaveCount(4);
  await expect(page.locator("#sliceSelectionSummary")).toHaveText("已选择 4 / 4 个切片");
  await page.locator("#sliceReset").click();
  await expect(page.locator("#sliceColumns")).toHaveValue("3");
  await expect(page.locator("#sliceOverlay .slice-box")).toHaveCount(6);

  await page.locator("#sliceSaveLibrary").click();
  await expect.poll(() => exported.length).toBe(1);
  expect(exported[0]).toEqual({
    action: "library",
    boxes: [
      { x: 0, y: 0, width: 83, height: 69 },
      { x: 83, y: 0, width: 82, height: 69 },
      { x: 165, y: 0, width: 83, height: 69 },
      { x: 0, y: 69, width: 83, height: 68 },
      { x: 83, y: 69, width: 82, height: 68 },
      { x: 165, y: 69, width: 83, height: 68 },
    ],
  });

  const download = page.waitForEvent("download");
  await page.locator("#sliceDownload").click();
  await download;
  await expect.poll(() => exported.length).toBe(2);
  expect(exported[1].action).toBe("download");

  await page.locator("#sliceClearSelection").click();
  await page.locator("#sliceList .slice-list-item").first().click();
  await expect(page.locator("#sliceReuse")).toBeEnabled();
  await page.locator("#sliceReuse").click();

  await expect(page.locator("#sliceDialog")).toBeHidden();
  await expect(page.locator("#chatInput")).toHaveValue("请基于这个切片继续调整：");
  await expect(page.locator("#chatReferenceCount")).toHaveText("1");
  expect(exported[2]).toEqual({
    action: "reference",
    boxes: [{ x: 0, y: 0, width: 83, height: 69 }],
  });
});
