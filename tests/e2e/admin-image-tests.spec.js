const path = require("node:path");
const { expect, test } = require("./fixtures");

test("管理员保存题组后按题对比生图结果 @responsive", async ({ studioPage: page }, info) => {
  const errors = [];
  const root = "/api/admin/image-tests";
  const channel = {
    id: "fixture",
    label: "测评渠道",
    configured: true,
    price_rmb: "0.2500",
    capabilities: { modes: ["text2img"] },
    models: [{ id: "image-a", label: "绘图 A" }, { id: "image-b", label: "绘图 B" }],
  };
  let run;
  let finished = false;
  let releaseInitialHistory;
  const initialHistory = new Promise((resolve) => { releaseInitialHistory = resolve; });
  let startSuiteSave;
  let releaseSuiteSave;
  const suiteSaveStarted = new Promise((resolve) => { startSuiteSave = resolve; });
  const suiteSaveGate = new Promise((resolve) => { releaseSuiteSave = resolve; });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route(`**${root}`, async (route) => {
    const response = await route.fetch();
    const catalog = await response.json();
    await route.fulfill({ response, json: { ...catalog, channels: [channel] } });
  });
  await page.route(`**${root}/suites`, async (route) => {
    if (route.request().method() === "POST") {
      startSuiteSave();
      await suiteSaveGate;
    }
    await route.continue();
  });
  await page.route(`**${root}/runs*`, async (route) => {
    if (route.request().method() === "GET") {
      if (!new URL(route.request().url()).searchParams.has("offset")) {
        await initialHistory;
        await route.fulfill({ json: { runs: [], total: 0, limit: 20 } });
        return;
      }
      await route.fulfill({ json: { runs: run ? [run] : [], total: run ? 1 : 0, limit: 20 } });
      return;
    }
    const payload = route.request().postDataJSON();
    expect(payload.targets).toEqual(channel.models.map((model) => ({ channel_id: channel.id, model: model.id })));
    const { suites } = await page.request.get(root).then((response) => response.json());
    const suite = suites.find((entry) => entry.id === payload.suite_id);
    run = {
      id: "fixture-run", suite_name: suite.name, created_at: "2026-11-25T00:00:00Z",
      questions: suite.questions, total: 4, finished: 0, active: true,
      worker_ready: true, counts: { queued: 4 }, expired: 0, estimated_rmb: "1.0000",
      results: suite.questions.flatMap((question, index) => channel.models.map((model) => ({
        id: `${index}-${model.id}`, model: model.id, question_index: index,
        channel_label: channel.label, status: "queued", verdict: "",
      }))),
    };
    await route.fulfill({ status: 202, json: { run } });
  });
  await page.route((url) => url.pathname === `${root}/runs/fixture-run`, (route) => {
    if (finished && run.active) {
      run = {
        ...run, active: false, finished: 4, counts: { succeeded: 2, interrupted: 2 },
        results: run.results.map((result) => result.model === "image-a"
          ? {
              ...result, status: "succeeded", elapsed_seconds: 3.5, width: 1024, height: 1024,
              format: "png", image_url: "/fixture-image.png", thumbnail_url: "/fixture-image.png",
            }
          : { ...result, status: "interrupted", error: "渠道超时", error_code: "timeout" }),
      };
    }
    if (new URL(route.request().url()).searchParams.has("progress")) {
      const { questions, ...progress } = run;
      return route.fulfill({ json: { run: progress } });
    }
    return route.fulfill({ json: { run } });
  });
  await page.route(`**${root}/runs/fixture-run/results/*`, (route) => {
    const id = new URL(route.request().url()).pathname.split("/").at(-1);
    run.results.find((result) => result.id === id).verdict = route.request().postDataJSON().verdict;
    return route.fulfill({ json: { ok: true } });
  });
  await page.route("**/fixture-image.png", (route) => route.fulfill({
    path: path.join(__dirname, "../../static/assets/favicon-64-v2.png"),
    contentType: "image/png",
  }));
  await page.goto("/admin");
  await page.getByRole("button", { name: "生图测评", exact: true }).click();
  await expect(page.locator("#imageTestEstimate")).toContainText("6 张图");
  await page.getByRole("button", { name: "清空", exact: true }).click();
  await expect(page.getByRole("button", { name: "开始测试" })).toBeDisabled();
  await page.getByRole("button", { name: "全选", exact: true }).click();
  await page.getByRole("button", { name: "题库", exact: true }).click();
  await page.getByRole("button", { name: "新建题组" }).click();
  const name = `画面对比 ${info.project.name}`;
  await page.getByLabel("题组名称").fill(name);
  await page.getByLabel("题目名称").fill("材质");
  await page.getByLabel("绘图提示词").fill("一只玻璃杯");
  await page.getByRole("button", { name: "添加题目" }).click();
  await page.getByLabel("题目名称").nth(1).fill("排版");
  await page.getByLabel("绘图提示词").nth(1).fill("春日书展海报");
  await page.getByRole("button", { name: "上移题目" }).nth(1).click();
  const saveResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && new URL(response.url()).pathname === `${root}/suites`,
  );
  await page.getByRole("button", { name: "保存并用于测试" }).click();
  await suiteSaveStarted;
  await expect(page.getByLabel("题组名称")).toBeDisabled();
  await expect(page.getByRole("button", { name: "新建题组" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "添加题目" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "下移题目" }).first()).toBeDisabled();
  releaseSuiteSave();
  await saveResponse;
  await expect(page.locator("#imageTestEstimate")).toContainText("2 道题 · 4 张图");
  await page.getByRole("button", { name: "开始测试" }).click();
  await expect(page.locator("#imageTestProgressLabel")).toHaveText("0 / 4 张已处理");
  const historyReply = page.waitForResponse((reply) =>
    reply.request().method() === "GET" && new URL(reply.url()).pathname === `${root}/runs`,
  );
  releaseInitialHistory();
  await historyReply;
  finished = true;
  await expect(page.locator("#imageTestProgressLabel")).toHaveText("4 / 4 张已处理");
  await expect(page.getByLabel("按题对比").locator("option").first()).toHaveText("排版");
  await page.getByLabel("按题对比").selectOption({ label: "材质" });
  await expect(page.locator("#imageTestResults > article")).toHaveCount(2);
  await expect(page.getByRole("button", { name: "重试失败项" })).toBeVisible();
  await expect(page.getByText("渠道超时", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "查看 测评渠道 image-a 原图" }).click();
  await expect(page.locator("#imageViewerDialog")).toBeVisible();
  await page.locator("#imageViewerDialog").evaluate((dialog) => dialog.close());
  await page.getByLabel("复核标记").selectOption("pass");
  await page.getByRole("button", { name: "历史记录", exact: true }).click();
  await expect(page.locator("#imageTestHistoryRuns")).toContainText(name);
  await page.getByLabel("按题对比").selectOption({ label: "材质" });
  await expect(page.getByLabel("复核标记")).toHaveValue("pass");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(page.viewportSize().width);
  expect(errors).toEqual([]);
});
