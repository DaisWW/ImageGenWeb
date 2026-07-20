const path = require("node:path");
const {
  expect,
  mockConfiguredImageChannel,
  test,
  uploadLibraryImage,
} = require("./fixtures");

function libraryImage(id, name, url) {
  return { id, name, url, thumbnail_url: url };
}

async function mockLibrarySelection(page, images) {
  const imported = [];
  await page.route("**/api/library-images?*", (route) => route.fulfill({
    json: { images, total: images.length, has_more: false },
  }));
  await page.route("**/api/workspaces/*/assets/from-library/*", async (route) => {
    const imageId = new URL(route.request().url()).pathname.split("/").at(-1);
    const image = images.find((entry) => entry.id === imageId);
    imported.push(imageId);
    await route.fulfill({
      json: {
        asset: {
          id: `asset-${imageId}`,
          name: image.name,
          url: image.url,
          thumbnail_url: image.thumbnail_url,
          mime_type: "image/png",
          bytes: 1024,
        },
      },
    });
  });
  return imported;
}

test("image library exposes retry after its initial load fails", async ({ studioPage: page }) => {
  let attempts = 0;
  await page.route("**/api/library-images?*", async (route) => {
    attempts += 1;
    if (attempts === 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "图库暂时不可用", code: "library_unavailable" }),
      });
      return;
    }
    await route.fulfill({
      json: {
        images: [],
        total: 0,
        has_more: false,
        max_count: 200,
        max_bytes: 2147483648,
      },
    });
  });

  await page.locator("#libraryButton").click();
  await expect(page.locator("#libraryError")).toBeVisible();
  await expect(page.locator("#libraryUploadButton")).toBeDisabled();
  await page.locator("#libraryRetryButton").click();

  await expect(page.locator("#libraryEmpty")).toBeVisible();
  await expect(page.locator("#libraryUploadButton")).toBeEnabled();
  expect(attempts).toBe(2);
});
test("image library pagination keeps its server offset after merging a duplicate", async ({
  studioPage: page,
}) => {
  const image = (index) => ({
    id: `library-${index}`,
    name: `library-${index}.png`,
    url: "/static/assets/brand-mark-v2.png",
    thumbnail_url: "/static/assets/brand-mark-v2.png",
  });
  const images = Array.from({ length: 61 }, (_value, index) => image(index));
  const requestedOffsets = [];
  await page.route("**/api/library-images**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname !== "/api/library-images") {
      await route.continue();
      return;
    }
    if (request.method() === "POST") {
      await route.fulfill({ json: { images: [images[60]], added_count: 0 } });
      return;
    }
    const offset = Number(url.searchParams.get("offset") || 0);
    requestedOffsets.push(offset);
    const pageImages = offset === 0 ? images.slice(0, 60) : images.slice(offset);
    await route.fulfill({
      json: {
        images: pageImages,
        total: images.length,
        has_more: offset + pageImages.length < images.length,
        max_count: 200,
        max_bytes: 2147483648,
      },
    });
  });

  await page.locator("#libraryButton").click();
  await expect(page.locator("#libraryGrid .library-card")).toHaveCount(60);
  await uploadLibraryImage(page, path.resolve("static/assets/brand-mark-v2.png"));
  await expect(page.locator("#libraryGrid .library-card")).toHaveCount(61);
  await expect(page.locator("#libraryLoadMoreButton")).toBeVisible();
  await page.locator("#libraryLoadMoreButton").click();

  await expect(page.locator("#libraryPagination")).toBeHidden();
  expect(requestedOffsets).toEqual([0, 60]);
});

test("image library confirms multiple message attachments together", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const images = [
    libraryImage("library-chat-a", "chat-a.png", "/static/assets/brand-mark-v2.png"),
    libraryImage("library-chat-b", "chat-b.png", "/static/assets/starter-ocean-sky-reference.png"),
  ];
  const imported = await mockLibrarySelection(page, images);

  await page.locator("#libraryButton").click();
  await expect(page.locator("#libraryGrid .library-card")).toHaveCount(2);
  const checkboxes = page.locator("#libraryGrid [data-select-library-image]");
  await checkboxes.nth(0).check();
  await checkboxes.nth(1).check();
  await expect(page.locator("#librarySelectionSummary")).toHaveText("已选择 2 / 20 张");
  await expect(page.locator("#libraryConfirmButton")).toBeEnabled();

  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#libraryDialog")).toBeHidden();
  await expect(page.locator("#chatReferenceCount")).toHaveText("2");
  expect(imported).toEqual(["library-chat-a", "library-chat-b"]);
});

test("image library mirrors existing message selections and card clicks toggle", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const images = [
    libraryImage("library-chat-sync", "chat-sync.png", "/static/assets/brand-mark-v2.png"),
  ];
  const imported = await mockLibrarySelection(page, images);

  await page.locator("#libraryButton").click();
  const card = page.locator("#libraryGrid .library-card").first();
  await card.locator("[data-toggle-library-image]").click();
  await expect(card.locator("[data-select-library-image]")).toBeChecked();
  await expect(page.locator("#libraryDialog")).toBeVisible();
  await expect(page.locator("#librarySelectionSummary")).toHaveText("已选择 1 / 20 张");
  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#libraryDialog")).toBeHidden();
  await expect(page.locator("#chatReferenceCount")).toHaveText("1");

  await page.locator("#libraryButton").click();
  const reopenedCard = page.locator("#libraryGrid .library-card").first();
  await expect(reopenedCard.locator("[data-select-library-image]")).toBeChecked();
  await expect(page.locator("#librarySelectionSummary")).toHaveText("已选择 1 / 20 张");

  await reopenedCard.locator("[data-toggle-library-image]").click();
  await expect(reopenedCard.locator("[data-select-library-image]")).not.toBeChecked();
  await expect(page.locator("#librarySelectionSummary")).toHaveText("已选择 0 / 20 张");
  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#libraryDialog")).toBeHidden();
  await expect(page.locator("#chatReferenceCount")).toHaveText("0");
  expect(imported).toEqual(["library-chat-sync"]);
});

test("image library confirms multiple padding images up to the channel limit", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  await mockConfiguredImageChannel(page);
  await page.reload();
  const images = [
    libraryImage("library-generation-a", "generation-a.png", "/static/assets/brand-mark-v2.png"),
    libraryImage("library-generation-b", "generation-b.png", "/static/assets/starter-ocean-sky-reference.png"),
  ];
  const imported = await mockLibrarySelection(page, images);

  await page.locator("#directGenerationButton").evaluate((button) => {
    button.hidden = false;
    button.click();
  });
  await expect(page.locator("#generationForm")).toBeVisible();
  await page.locator('#modeSwitch [data-mode="img2img"]').click();
  await page.locator("#referenceLibrary").click();
  await expect(page.locator("#libraryTargetLabel")).toHaveText("设为垫图");
  const checkboxes = page.locator("#libraryGrid [data-select-library-image]");
  await checkboxes.nth(0).check();
  await checkboxes.nth(1).check();
  await expect(page.locator("#librarySelectionSummary")).toHaveText("已选择 2 / 2 张");
  await page.locator("#libraryConfirmButton").click();

  await expect(page.locator("#libraryDialog")).toBeHidden();
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(2);
  await expect(page.locator("#referenceLimit")).toHaveText("2 / 2");
  expect(imported).toEqual(["library-generation-a", "library-generation-b"]);
});
