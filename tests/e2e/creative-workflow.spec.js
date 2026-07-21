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
} = require("./fixtures");

test("AI automatically prepares a gallery template before generation", {
  tag: "@responsive",
}, async ({ page }) => {
  const createdAt = new Date().toISOString();
  const userId = "a".repeat(32);
  const assistantId = "b".repeat(32);
  let workspaceId = "";
  let promptDraftRequests = 0;

  await mockConfiguredImageChannel(page);
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
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/prompt-drafts")) {
      promptDraftRequests += 1;
    }
  });
  await page.route("**/api/workspaces/*/messages*", async (route) => {
    const request = route.request();
    const targetId = new URL(request.url()).pathname.split("/")[3];
    if (!workspaceId || targetId !== workspaceId || request.method() !== "POST") {
      await route.continue();
      return;
    }
    const body = request.postDataJSON();
    await route.fulfill({
      status: 201,
      json: {
        messages: [
          {
            id: body.message_id || userId,
            role: "user",
            kind: "message",
            content: body.content,
            payload: { reply_message_id: assistantId },
            created_at: createdAt,
            attachments: [],
          },
          {
            id: assistantId,
            role: "assistant",
            kind: "prompt_draft",
            content: "需求确认",
            payload: {
              status: "ready",
              summary_zh: "竖版运动鞋新品发布海报，标题 AIR ZERO。",
              prompt: '3:4 vertical product poster with exact title "AIR ZERO".',
              canvas_request: { aspect_ratio: "16:9", width: 1920, height: 1080 },
              language: "en",
              generation_mode: "text2img",
              reference_ids: [],
              creative_direction: "poster",
              template_id: "poster-layout-system",
              template_label: "海报排版系统",
              gallery_categories: ["typography-and-posters"],
              gallery_category_labels: ["排版与海报"],
              retrieved_cases: [{
                id: "awesome:41",
                title: "城市旅游推广海报",
                source: "awesome-gpt-image-2",
                source_url: "https://example.test/case-41",
                category: "Posters & Typography",
              }],
              style_tags: ["Poster"],
              style_labels: ["海报"],
              scene_tags: ["Commerce", "Social"],
              scene_labels: ["商业", "社媒"],
              selection_reason: "交付物是商业海报，需要明确主视觉和标题层级。",
              hard_checks: ["标题逐字显示 AIR ZERO", "只出现一双主运动鞋"],
              quality_hint: "low",
              reply_to_message_id: body.message_id || userId,
            },
            provider_label: "E2E 助手",
            created_at: createdAt,
            attachments: [],
          },
        ],
        context: {
          compacted: false,
          estimated_context_tokens: 100,
          max_context_tokens: 32000,
        },
      },
    });
  });

  await loginAsAdmin(page);
  const workspaceName = `E2E-AI-Review-${Date.now()}`;
  await createWorkspace(page, workspaceName);
  workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");

  await expect(page.locator("#creativeDirectionSelect")).toHaveValue("auto");
  await page.locator("#chatInput").fill("竖版运动鞋新品发布海报，标题 AIR ZERO");
  await page.locator("#chatForm").evaluate((form) => form.requestSubmit());
  await expect(page.locator(".prompt-draft-content").last()).toBeVisible();
  await expect(page.locator("#draftPromptButton")).toHaveCount(0);
  expect(promptDraftRequests).toBe(0);

  const draft = page.locator(".prompt-draft-content").last();
  await expect(draft).toContainText("海报排版系统");
  await expect(draft).toContainText("图谱 排版与海报");
  await expect(draft).toContainText("案例 城市旅游推广海报");
  await expect(draft).toContainText("海报");
  await expect(draft).toContainText("商业 / 社媒");
  await expect(draft).toContainText("AI 匹配：交付物是商业海报");
  await expect(page.locator("#chatInput")).toBeEnabled();
  await draft.getByRole("button", { name: "使用此提示词生图" }).click();

  await expect(page.locator("#generationForm")).toBeVisible();
  await expect(page.locator("#promptReviewStatus")).toContainText("最终提示词已就绪");
  await expect(page.locator("#creativeDirectionSelect")).toHaveValue("auto");
  await expect(page.getByRole("button", { name: "草稿", exact: true })).toHaveCount(0);
  await expect(page.locator("#canvasConflict")).toBeVisible();
  await expect(page.locator("#canvasConflictMessage")).toContainText("1920×1080");
  await expect(page.locator("#generateButton")).toBeDisabled();
  await page.locator("#canvasConflictApply").click();
  await expect(page.locator("#sizeInput")).toHaveValue("1920x1080");
  await expect(page.locator("#canvasConflictMessage")).toContainText("已应用对话画幅");
  await expect(page.locator("#generateButton")).toBeEnabled();
  await page.locator("#sizeInput").fill("1024x1024");
  await page.locator("#sizeInput").press("Tab");
  await expect(page.locator("#canvasConflictMessage")).toContainText("请选择后再生成");
  await expect(page.locator("#generateButton")).toBeDisabled();
  await page.locator("#canvasConflictKeep").click();
  await expect(page.locator("#canvasConflictMessage")).toContainText("已保持当前尺寸");
  await expect(page.locator("#generateButton")).toBeEnabled();

  const toast = page.locator("#toastRegion .toast");
  if (await toast.count()) {
    const toastBox = await toast.boundingBox();
    const composerBox = await page.locator("#generationForm").boundingBox();
    expect(toastBox).not.toBeNull();
    expect(composerBox).not.toBeNull();
    expect(rectanglesOverlap(toastBox, composerBox)).toBe(false);
  }

  await page.locator("#promptInput").fill("用户手工修改后的提示词");
  await expect(page.locator("#promptReviewStatus")).toContainText("可直接编辑提示词");
  await expect(page.locator("#generateButton")).toBeEnabled();

  await closeGenerationComposer(page);
  await deleteWorkspace(page, workspaceName);
});
test("chat images are used for generation only when their semantic role requires it", async ({
  page,
}) => {
  const createdAt = new Date().toISOString();
  const assistantIds = ["c".repeat(32), "d".repeat(32)];
  const requests = [];
  let workspaceId = "";
  let round = 0;

  await mockConfiguredImageChannel(page);
  await page.route("**/api/chat-models", (route) => route.fulfill({
    json: {
      version: "e2e-semantic-references",
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
    const targetId = new URL(request.url()).pathname.split("/")[3];
    if (!workspaceId || targetId !== workspaceId || request.method() !== "POST") {
      await route.continue();
      return;
    }
    const body = request.postDataJSON();
    requests.push(body);
    const usesReferences = round === 0;
    const assistantId = assistantIds[round];
    round += 1;
    const referenceIds = usesReferences ? body.attachment_ids : [];
    await route.fulfill({
      status: 201,
      json: {
        messages: [{
          id: body.message_id,
          role: "user",
          kind: "message",
          content: body.content,
          payload: { reply_message_id: assistantId },
          created_at: createdAt,
          attachments: [],
        }, {
          id: assistantId,
          role: "assistant",
          kind: "prompt_draft",
          content: "需求确认",
          payload: {
            status: "ready",
            summary_zh: usesReferences
              ? "仿照两张水墨武侠图的视觉语言创作新图标。"
              : "图片只用于提炼风格，生成阶段不传入原图。",
            prompt: usesReferences
              ? "参考图 1 与参考图 2 作为水墨风格依据，创作不同的武侠图标。"
              : "独立创作一组高反差黑白水墨武侠图标，不使用参考图作为生成输入。",
            language: "zh",
            generation_mode: usesReferences ? "img2img" : "text2img",
            reference_usage: usesReferences ? "generation" : "analysis_only",
            reference_reason: usesReferences
              ? "用户要求仿照附件的水墨视觉语言。"
              : "用户明确要求原图仅用于分析。",
            reference_ids: referenceIds,
            creative_direction: "icon",
            template_id: "icon-symbol-system",
            template_label: "图标符号系统",
            style_tags: [],
            scene_tags: [],
            selection_reason: "交付物是武侠图标集。",
            hard_checks: ["图标缩小后仍清晰"],
            quality_hint: "low",
            reply_to_message_id: body.message_id,
          },
          provider_label: "E2E 助手",
          created_at: createdAt,
          attachments: [],
        }],
        context: {
          compacted: false,
          estimated_context_tokens: 100,
          max_context_tokens: 32000,
        },
      },
    });
  });

  await loginAsAdmin(page);
  const workspaceName = `E2E-Semantic-References-${Date.now()}`;
  await createWorkspace(page, workspaceName);
  workspaceId = await page.locator("#workspaceList .workspace-item.active")
    .getAttribute("data-workspace-id");

  await page.locator("#chatReferenceButton").click();
  const chooserPromise = page.waitForEvent("filechooser");
  await page.locator("[data-upload-chat-reference]").click();
  const chooser = await chooserPromise;
  await chooser.setFiles([
    path.resolve("static/assets/starter-ocean-sky-reference.png"),
    path.resolve("static/assets/brand-mark-v2.png"),
  ]);
  await expect(page.locator("#chatReferenceCount")).toHaveText("2");

  await page.locator("#chatInput").fill("仿照这两张图的水墨风格生成一套新图标");
  await page.locator("#chatForm").evaluate((form) => form.requestSubmit());
  const generationDraft = page.locator(".prompt-draft-content").last();
  await expect(generationDraft).toContainText("使用 2 张垫图");
  await generationDraft.getByRole("button", { name: "使用此提示词生图" }).click();
  await expect(page.locator("#modeSwitch")).toHaveAttribute("data-mode", "img2img");
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(2);

  await page.locator('#modeSwitch [data-mode="text2img"]').click();
  await closeGenerationComposer(page);
  await page.locator("#chatReferenceButton").click();
  const chatReferences = page.locator("[data-chat-reference-toggle]");
  await expect(chatReferences).toHaveCount(2);
  await chatReferences.nth(0).click();
  await chatReferences.nth(1).click();
  await expect(page.locator("#chatReferenceCount")).toHaveText("2");

  await page.locator("#chatInput").fill("只分析并提炼风格，不要把原图传给生图模型");
  await page.locator("#chatForm").evaluate((form) => form.requestSubmit());
  const analysisDraft = page.locator(".prompt-draft-content").last();
  await expect(analysisDraft).toContainText("图片仅用于分析");
  await analysisDraft.getByRole("button", { name: "使用此提示词生图" }).click();
  await expect(page.locator("#modeSwitch")).toHaveAttribute("data-mode", "text2img");
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(0);

  expect(requests).toHaveLength(2);
  expect(requests.map((body) => body.generation_mode)).toEqual(["auto", "auto"]);
  expect(requests.map((body) => body.generation_reference_ids)).toEqual([[], []]);

  await closeGenerationComposer(page);
  await deleteWorkspace(page, workspaceName);
});
