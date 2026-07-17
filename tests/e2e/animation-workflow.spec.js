const path = require("node:path");
const {
  closeGenerationComposer,
  createWorkspace,
  deleteWorkspace,
  expect,
  mockConfiguredImageChannel,
  test,
  uploadLibraryImage,
} = require("./fixtures");

test("animation workstation only generates frames from a user-selected master", async ({
  studioPage: page,
}, testInfo) => {
  await mockConfiguredImageChannel(page);
  await page.reload();
  const workspaceName = `E2E-Animation-${testInfo.project.name}-${Date.now()}`;
  await createWorkspace(page, workspaceName, "animation");

  await expect(page.locator("#animationParametersButton")).toBeVisible();
  await page.locator("#chatInput").fill("角色原地挥手，固定镜头，无缝循环。");
  await page.locator("#animationParametersButton").click();

  await expect(page.locator("#generationHeadingTitle")).toHaveText("确认帧动画参数");
  await expect(page.locator("#generationHeadingSubtitle"))
    .toContainText("选择一张母图并确认帧参数");
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
  await expect(page.locator("#generateButton")).toBeEnabled();
  await expect(page.locator("#saveState")).toHaveText("参数已保存");

  await page.reload();
  await page.locator("#chatInput").fill("角色原地挥手，固定镜头，无缝循环。");
  await page.locator("#animationParametersButton").click();
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(1);
  await expect(page.locator("#referenceLimit")).toHaveText("1 / 1");
  await expect(page.locator("#generateButton")).toBeEnabled();

  const master = page.locator("#referenceList [data-reference-toggle]");
  await master.click();
  await expect(page.locator("#generateButton")).toHaveAttribute("title", "请先添加并选择一张母图");
  await master.click();
  await expect(page.locator("#generateButton")).toBeEnabled();

  await master.click();
  await closeGenerationComposer(page);
  await page.locator("#animationParametersButton").click();
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(1);
  await expect(page.locator("#generateButton")).toBeEnabled();

  await closeGenerationComposer(page);
  await deleteWorkspace(page, workspaceName);
});
test("image library keeps the selected animation master when chat has an attachment", async ({
  studioPage: page,
}, testInfo) => {
  await mockConfiguredImageChannel(page);
  await page.reload();
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
  await expect(page.locator("#generateButton")).toBeEnabled();

  await closeGenerationComposer(page);
  await deleteWorkspace(page, workspaceName);
});
