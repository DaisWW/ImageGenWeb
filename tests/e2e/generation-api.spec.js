const { expect, loginAsAdmin, test } = require("./fixtures");

test("users can enable the generation API and download its skill", { tag: "@responsive" }, async ({ page }) => {
  await loginAsAdmin(page);
  await expect(await page.request.get("/api/generations/active")).toBeOK();
  await page.getByRole("link", { name: "生图 API", exact: true }).click();
  await expect(page.getByRole("heading", { name: "生图 API" })).toBeVisible();
  await expect(page.getByLabel("结果工作站")).toHaveCount(0);
  await expect(page.locator("#generationApiStatus")).toHaveText("未开通");
  const configuredSkill = page.getByText("ZIP 已包含站点 URL 和当前 API 密钥。", { exact: true });
  const genericSkill = page.getByText("通用 ZIP 需要手动配置站点 URL 和 API 密钥。", { exact: false });
  await expect(genericSkill).toBeVisible();
  await expect(configuredSkill).toBeHidden();

  await page.getByRole("button", { name: "开通并生成密钥" }).click();
  const token = page.getByLabel("个人生图 API 密钥");
  await expect(token).toHaveValue(/^snow_[A-Za-z0-9_-]{43}$/);
  await expect(page.locator("#generationApiStatus")).toHaveText("已开通");
  await expect(configuredSkill).toBeVisible();
  await expect(genericSkill).toBeHidden();
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

  const downloadSkill = page.getByRole("button", { name: "下载 AI Skill" });
  await page.route("**/account/generation-api/skill", (route) => route.fulfill({ status: 503 }), { times: 1 });
  await downloadSkill.click();
  await expect(page.locator("#toastRegion")).toContainText("Skill 下载失败，请重试");
  await expect(downloadSkill).toBeEnabled();

  const [download] = await Promise.all([page.waitForEvent("download"), downloadSkill.click()]);
  expect(download.suggestedFilename()).toBe("snow-image-api-configured.zip");
  expect(await download.failure()).toBeNull();
  await expect(page.locator("#toastRegion")).toContainText("已发起下载 snow-image-api-configured.zip");
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
  await expect(genericSkill).toBeVisible();
  await expect(configuredSkill).toBeHidden();
  expect((await page.request.get("/api/v1/config", { headers })).status()).toBe(401);

  const [genericDownload] = await Promise.all([page.waitForEvent("download"), downloadSkill.click()]);
  expect(genericDownload.suggestedFilename()).toBe("snow-image-api.zip");
  expect(await genericDownload.failure()).toBeNull();

  await page.context().clearCookies();
  await downloadSkill.click();
  await expect(page.locator("#toastRegion")).toContainText("登录已过期，请重新登录后下载");
  await expect(page).toHaveURL(/\/account\/generation-api$/);
});
