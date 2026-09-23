const { expect, test } = require("./fixtures");

const model = (label) => ({
  label,
  enabled: true,
  configured: true,
  base_url: "https://chat.example",
  has_api_key: true,
  api_key_hint: "test****cret",
  model: "gpt-test",
  reasoning_effort: "max",
  review_reasoning_effort: "medium",
  timeout_seconds: 30,
  max_output_tokens: 1000,
  fallback_model_names: [],
});

test("admin copies a chat model in one click and saves dragged order", async ({ studioPage: page }) => {
  let config = {
    version: "chat-test",
    revision: "revision-1",
    managed: true,
    source: "database",
    last_error: "",
    context: { max_context_tokens: 32000 },
    models: [model("模型 A"), model("模型 B")],
    system_prompts: { chat: "Chat prompt" },
    workspace_prompts: { image: "Image prompt" },
  };
  const saves = [];
  await page.route("**/api/admin/chat-models", (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      saves.push(body);
      config = { ...config, ...body, revision: `revision-${saves.length + 1}` };
    }
    return route.fulfill({ json: { config } });
  });

  await page.goto("/admin");
  await page.getByRole("button", { name: "渠道与模型", exact: true }).click();
  const rows = page.locator("#chatModelTableBody tr");
  await expect(rows).toHaveCount(2);
  await rows.first().getByRole("button", { name: "复制模型" }).click();
  await expect(rows).toHaveCount(3);
  expect(saves[0].models[2]).toMatchObject({
    label: "模型 A 副本",
    copy_from_label: "模型 A",
  });
  expect(saves[0].models[2]).not.toHaveProperty("id");

  await rows.nth(2).dragTo(rows.first());
  await expect(rows.first()).toContainText("模型 A 副本");
  expect(saves[1].models.map((item) => item.label))
    .toEqual(["模型 A 副本", "模型 A", "模型 B"]);
});

test("studio lists chat models in configured order and selects the first", async ({ studioPage: page }) => {
  await page.route("**/api/chat-models", (route) => route.fulfill({
    json: {
      version: "ordered-chat-models",
      models: [model("模型 B"), model("模型 A")],
    },
  }));

  await page.goto("/");
  const select = page.locator("#chatModelSelect");
  await expect(select).toHaveValue("模型 B");
  await expect(select.locator("option")).toHaveText(["模型 B", "模型 A"]);
});
