const { test: base, expect } = require("@playwright/test");

async function loginAsAdmin(page) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill("e2e-admin");
  await page.getByLabel("密码").fill("E2eStrongPass123!");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page).toHaveURL(/\/$/);
}

async function createWorkspace(page, name, kind = "image") {
  await page.locator("#newWorkspaceButton").click();
  await page.locator("#workspaceNameInput").fill(name);
  if (kind !== "image") {
    await page.locator(`#workspaceKindSwitch [data-workspace-kind="${kind}"]`).click();
  }
  await page.locator('#workspaceForm button[type="submit"]').click();
  await expect(page.locator("#workspaceTitle")).toHaveText(name);
}

async function deleteWorkspace(page, name) {
  const workspace = page.locator("#workspaceList .workspace-item", { hasText: name });
  await workspace.locator("[data-delete-workspace]").click();
  await expect(page.locator("#workspaceDeleteDialog")).toBeVisible();
  await page.locator('#workspaceDeleteForm button[type="submit"]').click();
  await expect(page.locator("#workspaceList")).not.toContainText(name);
}

async function closeGenerationComposer(page) {
  if (page.viewportSize().width >= 640) {
    await page.locator("#generationBackdrop").click({ position: { x: 10, y: 10 } });
  } else {
    await page.locator("#generationBackButton").click();
  }
}

async function uploadLibraryImage(page, file) {
  await expect(page.locator("#libraryUploadButton")).toBeEnabled();
  await page.locator("#libraryInput").setInputFiles(file);
}

async function mockConfiguredImageChannel(page) {
  await page.route("**/api/channels", (route) => route.fulfill({
    json: {
      version: "e2e-channels",
      channels: [{
        id: "e2e",
        label: "E2E 渠道",
        enabled: true,
        configured: true,
        models: [{ id: "e2e-image", label: "GPT Image 2" }],
        default_model: "e2e-image",
        price_rmb: "0.0300",
        capabilities: {
          modes: ["text2img", "img2img"],
          max_reference_images: 2,
          max_reference_image_mb: 10,
          max_reference_total_mb: 40,
          sizes: ["1024x1024"],
          formats: ["png", "jpeg", "webp"],
        },
        limits: { max_concurrency: 2 },
      }],
    },
  }));
}

function rectanglesOverlap(first, second) {
  return first.x < second.x + second.width
    && first.x + first.width > second.x
    && first.y < second.y + second.height
    && first.y + first.height > second.y;
}

const test = base.extend({
  studioPage: async ({ page }, use) => {
    await loginAsAdmin(page);
    await use(page);
  },
});

module.exports = {
  closeGenerationComposer,
  createWorkspace,
  deleteWorkspace,
  expect,
  loginAsAdmin,
  mockConfiguredImageChannel,
  rectanglesOverlap,
  test,
  uploadLibraryImage,
};
