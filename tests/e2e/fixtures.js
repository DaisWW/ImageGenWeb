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
  test,
  uploadLibraryImage,
};
