const {
  expect,
  mockConfiguredImageChannel,
  test,
} = require("./fixtures");

test("image detail keeps its reference through multi-turn refinement", async ({
  studioPage: page,
}) => {
  await mockConfiguredImageChannel(page);
  const workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");
  const createdAt = new Date().toISOString();
  const jobId = "e2e-reviewed-job";
  const itemId = "e2e-reviewed-item";
  const imageUrl = "/static/assets/brand-mark-v2.png";
  const suggestion = "只改变背景为纯白色；必须保持主体、构图和文字不变。";
  const referenceAsset = {
    id: "e2e-review-reference",
    name: "review-result.png",
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
    },
    output_format: "png",
    compression: 90,
    transparent_background: false,
    animation_fps: null,
    animation_loop: null,
    animation_format: null,
    animation_duration_seconds: null,
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
    animation_url: null,
    animation_download_url: null,
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
  await page.route(`**/api/generation-items/${itemId}/review`, (route) => route.fulfill({
    json: {
      review: {
        verdict: "revise",
        hard_checks: [{
          id: "criterion_1",
          label: "背景必须为纯白色",
          passed: false,
          evidence: "背景仍有灰色纹理",
        }],
        scores: { composition: 4.2, visual_quality: 4.0, usability: 3.1 },
        findings: ["背景不符合交付要求"],
        suggested_edit: suggestion,
      },
    },
  }));
  await page.route(`**/api/generation-items/${itemId}/reference`, (route) => route.fulfill({
    status: 201,
    json: { asset: referenceAsset },
  }));
  let chatRound = 0;
  const sentAttachmentIds = [];
  const sentGenerationReferenceIds = [];
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
        payload: { reply_to_message_id: body.message_id },
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
  await expect(page.locator("#detailList")).toContainText("商品商业视觉");
  await page.locator("#detailRunReview").click();

  await expect(page.locator("#detailReviewVerdict")).toHaveText("需要精修");
  await expect(page.locator("#detailReviewChecks")).toContainText("背景必须为纯白色");
  await expect(page.locator("#detailReviewSuggestion")).toHaveText(suggestion);
  await expect(page.locator("#detailApplyReview")).toBeVisible();
  await page.locator("#detailApplyReview").click();

  await expect(page.locator("#imageDialog")).toBeHidden();
  await expect(page.locator("#chatInput")).toHaveValue(suggestion);
  await expect(page.locator("#chatReferenceCount")).toHaveText("1");

  await page.locator("#chatForm").evaluate((form) => form.requestSubmit());
  await expect(page.locator(".message-row.assistant", { hasText: "背景还需要确认" }))
    .toBeVisible();
  await page.locator("#chatInput").fill("背景使用纯白色，可以生成了。");
  await page.locator("#chatForm").evaluate((form) => form.requestSubmit());

  const draft = page.locator(".prompt-draft-content").last();
  await expect(draft).toContainText("只把背景改成纯白色");
  expect(sentAttachmentIds).toEqual([[referenceAsset.id], []]);
  expect(sentGenerationReferenceIds).toEqual([[referenceAsset.id], [referenceAsset.id]]);
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
    animation_fps: null,
    animation_loop: null,
    animation_format: null,
    animation_duration_seconds: null,
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
      review: {},
    }],
    animation_url: null,
    animation_download_url: null,
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
  await expect(page.locator("#slicePreviewTitle")).toHaveText("2 行 × 3 列");
  await expect(page.locator("#sliceConfidence")).toHaveText("高置信度");
  await expect(page.locator("#sliceOverlay .slice-box")).toHaveCount(6);
  await expect(page.locator("#sliceList .slice-list-item.selected")).toHaveCount(6);
  await expect(page.locator("#sliceSelectionSummary")).toHaveText("已选择 6 / 6 个切片");
  await expect(page.locator("#sliceMarginX, #sliceMarginY, #sliceGapX, #sliceGapY"))
    .toHaveCount(0);
  await expect(page.locator("#sliceReuse")).toBeDisabled();

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
