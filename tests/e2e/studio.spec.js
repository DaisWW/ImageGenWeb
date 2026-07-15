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
