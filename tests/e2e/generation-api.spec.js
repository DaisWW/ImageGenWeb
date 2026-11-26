const { expect, loginAsAdmin, test } = require("./fixtures");

test("users can enable the generation API and download its skill", { tag: "@responsive" }, async ({ page }) => {
  await loginAsAdmin(page);
  await expect(await page.request.get("/api/generations/active")).toBeOK();
  await page.getByRole("link", { name: "生图 API", exact: true }).click();
  await expect(page.getByRole("heading", { name: "生图 API" })).toBeVisible();
  await expect(page.getByLabel("结果工作站")).toHaveCount(0);
  await expect(page.locator("#generationApiStatus")).toHaveText("未开通");

  await page.getByRole("button", { name: "开通并生成密钥" }).click();
  const token = page.getByLabel("个人生图 API 密钥");
  await expect(token).toHaveValue(/^snow_[A-Za-z0-9_-]{43}$/);
  await expect(page.locator("#generationApiStatus")).toHaveText("已开通");
  const issuedToken = await token.inputValue();
  const headers = { Authorization: "Bearer " + issuedToken };
  const config = await page.request.get("/api/v1/config", { headers });
  expect(config.ok()).toBeTruthy();
  expect((await config.json()).channels).toBeInstanceOf(Array);

  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  });
  const copy = page.getByRole("button", { name: "复制密钥" });
  await copy.click();
  await expect(page.locator("#toastRegion")).toContainText("已复制密钥");
  expect(await token.evaluate(async (field) => {
    delete navigator.clipboard;
    return (await navigator.clipboard.readText()) === field.value;
  })).toBe(true);

  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", { value: undefined });
    document.execCommand = () => false;
  });
  await copy.click();
  await expect(copy).toBeHidden();
  await expect(page.locator("#toastRegion")).toContainText("请手动复制选中的密钥");

  const downloaded = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载 AI Skill" }).click();
  expect((await downloaded).suggestedFilename()).toBe("snow-image-api.zip");
  await page.reload();
  await expect(page.locator("#generationApiStatus")).toHaveText("已开通");
  await expect(token).toHaveValue(issuedToken);
  await copy.click();
  await expect(page.locator("#toastRegion")).toContainText("已复制密钥");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(
    await page.evaluate(() => window.innerWidth),
  );

  await page.getByRole("button", { name: "关闭 API" }).click();
  await expect(page.locator("#generationApiStatus")).toHaveText("未开通");
  expect((await page.request.get("/api/v1/config", { headers })).status()).toBe(401);
});
