const { expect, loginAsAdmin, test } = require("./fixtures");

test("users can enable the generation API and download its skill", { tag: "@responsive" }, async ({ page }) => {
  await loginAsAdmin(page);
  await expect(await page.request.get("/api/generations/active")).toBeOK();
  await page.getByRole("link", { name: "生图 API", exact: true }).click();
  await expect(page.getByRole("heading", { name: "生图 API" })).toBeVisible();
  await expect(page.getByLabel("结果工作站")).toHaveCount(0);
  await expect(page.locator("#generationApiStatus")).toHaveText("未开通");

  await page.getByRole("button", { name: "开通并生成密钥" }).click();
  const token = page.getByLabel("新密钥仅显示一次，请妥善保存");
  await expect(token).toHaveValue(/^snow_[A-Za-z0-9_-]{43}$/);
  await expect(page.locator("#generationApiStatus")).toHaveText("已开通");
  const headers = { Authorization: "Bearer " + await token.inputValue() };
  const config = await page.request.get("/api/v1/config", { headers });
  expect(config.ok()).toBeTruthy();
  expect((await config.json()).channels).toBeInstanceOf(Array);

  const downloaded = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载 AI Skill" }).click();
  expect((await downloaded).suggestedFilename()).toBe("snow-image-api.zip");
  await page.reload();
  await expect(page.locator("#generationApiStatus")).toHaveText("已开通");
  await expect(token).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(
    await page.evaluate(() => window.innerWidth),
  );

  await page.getByRole("button", { name: "关闭 API" }).click();
  await expect(page.locator("#generationApiStatus")).toHaveText("未开通");
  expect((await page.request.get("/api/v1/config", { headers })).status()).toBe(401);
});
