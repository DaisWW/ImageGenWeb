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
    detailSaveLibrary: "将当前生成图保存到工作站图库，便于以后作为参考图复用。",
    detailRunReview: "让 AI 按提示词和硬门槛检查当前图片，并给出单点修正建议。",
    detailApplyReview: "把当前图作为参考并载入验收建议，继续生成精修版本。",
    detailSeriesAnchor: "将当前图设为系列固定参考；后续只改变新需求允许的内容，并保持主体、风格和构图一致。",
    detailReuse: "将当前图加入参考图，并回到创作区描述需要改变的内容。",
    detailDownload: "下载当前生成结果的原始文件。",
  });

  const seriesStates = await page.evaluate(() => {
    const refresh = window.ImageGenStudio.StudioApp.prototype.refreshDetailSeriesAnchorState;
    const resolve = ({ contract = {}, current = false }) => {
      const button = document.createElement("button");
      const item = { id: "detail-item" };
      const job = { id: "detail-job", items: [item], workflow: { series_contract: contract } };
      refresh.call({
        activeWorkspace: {
          settings: { series_anchor: current ? { source_item_id: item.id } : null },
        },
        detailItemId: item.id,
        detailJobId: job.id,
        detailReferenceBusy: false,
        jobs: [job],
        el: { detailSeriesAnchor: button },
      }, job, item);
      return { disabled: button.disabled, text: button.textContent, title: button.title };
    };
    return {
      unavailable: resolve({}),
      available: resolve({ contract: { visual_language: ["一致"] } }),
      current: resolve({ contract: { visual_language: ["一致"] }, current: true }),
    };
  });

  expect(seriesStates).toEqual({
    unavailable: {
      disabled: true,
      text: "设为系列基准",
      title: "需先用 AI 整理提示词生成系列契约；之后可将此图设为系列固定参考。",
    },
    available: {
      disabled: false,
      text: "设为系列基准",
      title: "将当前图设为系列固定参考；后续只改变新需求允许的内容，并保持主体、风格和构图一致。",
    },
    current: {
      disabled: true,
      text: "当前系列基准",
      title: "当前图已是系列固定参考；后续生成会优先保持主体、风格和构图一致。",
    },
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
    const detailSeriesAnchor = document.createElement("button");
    const detailApplyReview = document.createElement("button");
    const target = {
      detailItemId: "detail-item",
      detailJobId: "detail-job",
      activeWorkspace: { id: "workspace" },
      jobs: [],
      detailReferenceBusy: false,
      detailReviewSuggestion: "只改变一个问题",
      el: {
        detailReuse,
        detailUiKit,
        detailSeriesAnchor,
        detailApplyReview,
        imageDialog: document.createElement("dialog"),
      },
      applyReferenceAsset: async () => {},
      refreshDetailSeriesAnchorState() {
        window.ImageGenStudio.StudioApp.prototype.refreshDetailSeriesAnchorState.call(this);
      },
    };
    try {
      const first = window.ImageGenStudio.StudioApp.prototype.useDetailAsReference.call(target);
      const second = window.ImageGenStudio.StudioApp.prototype.useDetailAsReference.call(target);
      const disabledDuringRequest = detailReuse.disabled
        && detailUiKit.disabled
        && detailSeriesAnchor.disabled
        && detailApplyReview.disabled;
      release();
      await Promise.all([first, second]);
      return {
        calls,
        disabledDuringRequest,
        eligibleActionsDisabledAfterRequest: detailReuse.disabled
          || detailUiKit.disabled
          || detailApplyReview.disabled,
        seriesAnchorDisabledAfterRequest: detailSeriesAnchor.disabled,
      };
    } finally {
      window.ImageGenStudio.UI.api = originalApi;
    }
  });

  expect(result).toEqual({
    calls: 1,
    disabledDuringRequest: true,
    eligibleActionsDisabledAfterRequest: false,
    seriesAnchorDisabledAfterRequest: true,
  });
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
      review: {},
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
  let reviewRequest = null;
  let markReviewStarted;
  const reviewStarted = new Promise((resolve) => {
    markReviewStarted = resolve;
  });
  let releaseReview;
  const reviewCanFinish = new Promise((resolve) => {
    releaseReview = resolve;
  });
  let markReviewRetryStarted;
  const reviewRetryStarted = new Promise((resolve) => {
    markReviewRetryStarted = resolve;
  });
  let releaseReviewRetry;
  const reviewRetryCanFinish = new Promise((resolve) => {
    releaseReviewRetry = resolve;
  });
  let reviewAttempts = 0;
  await page.route(`**/api/generation-items/${itemId}/review`, async (route) => {
    reviewAttempts += 1;
    reviewRequest = route.request().postDataJSON();
    if (reviewAttempts > 1) {
      markReviewRetryStarted();
      await reviewRetryCanFinish;
      await route.fulfill({
        status: 503,
        json: { error: "E2E 验收服务暂时不可用", code: "review_unavailable" },
      });
      return;
    }
    markReviewStarted();
    await reviewCanFinish;
    await route.fulfill({
      json: {
        review: {
          verdict: "revise",
          hard_checks: [
            {
              id: "instruction_following",
              label: "整体指令遵循",
              passed: true,
              evidence: "主体与构图符合要求",
            },
            {
              id: "criterion_1",
              label: "画面中不得出现文字",
              passed: false,
              evidence: "鞋盒右下角存在多余文字",
            },
          ],
          scores: { composition: 4.2, visual_quality: 3.8, usability: 2.5 },
          findings: Array(18).fill("这里是一段用于验证长验收内容滚动布局的说明。"),
          suggested_edit: "只改变鞋盒右下角，移除多余文字；必须保持运动鞋和构图不变。",
        },
      },
    });
  });
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
  await page.locator("#detailRunReview").click();
  await reviewStarted;
  await expect(page.locator("#detailReview")).toBeVisible();
  await expect(page.locator("#detailReview")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#detailReviewVerdict")).toHaveText("正在验收");
  await expect(page.locator("#detailReviewProgress")).toBeVisible();
  await expect(page.locator("#detailRunReview")).toBeDisabled();
  await expect(page.locator("#detailRunReview")).toContainText("正在验收");
  expect(reviewRequest).toEqual({ model_id: "e2e-chat" });

  await page.locator('#imageDialog [data-close-dialog="imageDialog"]').click();
  await expect(page.locator("#imageDialog")).toBeHidden();
  await page.locator(`[data-item-id="${itemId}"]`).click();
  await expect(page.locator("#detailReview")).toBeVisible();
  await expect(page.locator("#detailReviewVerdict")).toHaveText("正在验收");
  await expect(page.locator("#detailReviewProgress")).toBeVisible();
  await expect(page.locator("#detailRunReview")).toBeDisabled();

  releaseReview();
  await expect(page.locator("#detailReviewVerdict")).toHaveText("需要精修");
  await expect(page.locator("#detailReview")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#detailReviewProgress")).toBeHidden();
  await expect(page.locator("#detailRunReview")).toBeEnabled();
  await expect(page.locator("#detailReviewScores")).toContainText("4.2");
  await expect(page.locator("#detailReviewChecks")).toContainText("鞋盒右下角存在多余文字");

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
        bodyScrolls: scroller.scrollHeight > scroller.clientHeight,
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
      bodyScrolls: true,
      bodyOverflow: "auto",
      infoOverflow: "hidden",
      layoutOverflow: "hidden",
      previewStayedFixed: true,
      closeVisible: true,
      actionsVisible: true,
    });
  }

  await page.locator("#detailRunReview").click();
  await reviewRetryStarted;
  await expect(page.locator("#detailReviewVerdict")).toHaveText("正在重新验收");
  await expect(page.locator("#detailReviewProgress")).toBeVisible();
  await page.locator("#detailRunReview").dispatchEvent("click");
  expect(reviewAttempts).toBe(2);
  releaseReviewRetry();
  await expect(page.locator("#detailReviewVerdict")).toHaveText("需要精修");
  await expect(page.locator("#detailReviewProgress")).toBeHidden();
  await expect(page.locator("#detailRunReview")).toBeEnabled();

  await page.locator("#detailApplyReview").click();
  await expect(page.locator("#imageDialog")).toBeHidden();
  await expect(page.locator("#chatInput"))
    .toHaveValue("只改变鞋盒右下角，移除多余文字；必须保持运动鞋和构图不变。");

  await page.locator(`[data-item-id="${itemId}"]`).click();
  await page.locator("#detailUiKit").click();
  await expect(page.locator("#imageDialog")).toBeHidden();
  await expect(page.locator("#chatInput")).toHaveValue(/不要抠取、分割或复制原图像素/);
  await expect(page.locator("#chatInput")).toHaveValue(/模块 → 原子资源/);
  await expect(page.locator("#chatReferenceCount")).toHaveText("1");
  await expect(page.locator("#modeSwitch")).toHaveAttribute("data-mode", "img2img");
  await expect(page.locator("#creativeDirectionSelect")).toHaveValue("game_ui");
  await expect(page.locator("#formatSelect")).toHaveValue("png");
  await expect(page.locator("#sizeInput")).toHaveValue("1024x1024");
  await expect(page.locator("#transparentBackground")).toBeChecked();
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
  await expect(page.locator("#modeSwitch")).toHaveAttribute("data-mode", "img2img");
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
