const { readFile } = require("node:fs/promises");
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

test("image library repeated paste reports an upload in progress without duplicating it", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const image = libraryImage(
    "library-clipboard", "clipboard.png", "/static/assets/brand-mark-v2.png",
  );
  let uploads = 0;
  let releaseUpload;
  const uploadReleased = new Promise((resolve) => { releaseUpload = resolve; });
  await page.route("**/api/library-images?*", (route) => route.fulfill({
    json: { images: [], total: 0, has_more: false },
  }));
  await page.route("**/api/library-images", async (route) => {
    uploads += 1;
    await uploadReleased;
    await route.fulfill({ status: 201, json: { images: [image], added_count: 1 } });
  });
  const pasteImage = () => page.evaluate(() => {
    const clipboardData = new DataTransfer();
    clipboardData.items.add(new File(["image"], "clipboard.png", { type: "image/png" }));
    document.getElementById("libraryDialog").dispatchEvent(new ClipboardEvent("paste", {
      clipboardData, bubbles: true, cancelable: true,
    }));
  });

  await page.locator("#libraryButton").click();
  await expect(page.locator("#libraryUploadButton")).toBeEnabled();
  try {
    await pasteImage();
    await expect.poll(() => uploads).toBe(1);
    await expect(page.locator("#libraryUploadButton")).toBeDisabled();
    await pasteImage();
    await expect(page.locator("#toastRegion .toast.info")).toHaveText("图片正在导入，请稍候");
    await expect(page.locator("#toastRegion .toast.error")).toHaveCount(0);
    expect(uploads).toBe(1);
  } finally {
    releaseUpload();
  }
  await expect(page.locator("#libraryGrid .library-card")).toHaveCount(1);
  await expect(page.locator("#toastRegion .toast.success")).toHaveText("已将 1 张图片存入图库");
  await expect(page.locator("#libraryUploadButton")).toBeEnabled();
  expect(uploads).toBe(1);
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
  await checkboxes.nth(1).check();
  await checkboxes.nth(0).check();
  await expect(page.locator("#libraryGrid .reference-order")).toHaveText(["2", "1"]);
  await expect(page.locator("#librarySelectionSummary")).toHaveText("已选择 2 / 20 张");
  await expect(page.locator("#libraryConfirmButton")).toBeEnabled();

  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#libraryDialog")).toBeHidden();
  await expect(page.locator("#chatReferenceCount")).toHaveText("2");
  expect(imported).toEqual(["library-chat-b", "library-chat-a"]);
  await expect(page.locator("#chatReferenceList .reference-order")).toHaveText(["1", "2"]);
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

test("image library reselecting an attachment updates its order", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const images = [
    libraryImage("library-chat-order-a", "order-a.png", "/static/assets/brand-mark-v2.png"),
    libraryImage("library-chat-order-b", "order-b.png", "/static/assets/starter-ocean-sky-reference.png"),
  ];
  const imported = await mockLibrarySelection(page, images);

  await page.locator("#libraryButton").click();
  const checkboxes = page.locator("#libraryGrid [data-select-library-image]");
  await checkboxes.nth(1).check();
  await checkboxes.nth(0).check();
  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#libraryDialog")).toBeHidden();
  expect(imported).toEqual(["library-chat-order-b", "library-chat-order-a"]);

  await page.locator("#libraryButton").click();
  await expect(page.locator("#libraryGrid .reference-order")).toHaveText(["2", "1"]);
  await page.locator("#libraryGrid .library-card").nth(1).locator("[data-toggle-library-image]").click();
  await page.locator("#libraryGrid .library-card").nth(1).locator("[data-toggle-library-image]").click();
  await expect(page.locator("#libraryGrid .reference-order")).toHaveText(["1", "2"]);
  await expect(page.locator("#libraryConfirmButton")).toBeEnabled();
  await page.locator("#libraryConfirmButton").click();

  expect(await page.locator("#chatReferenceList .chat-reference-item").evaluateAll((cards) => (
    cards.map((card) => ({
      id: card.querySelector("[data-chat-reference-toggle]")?.dataset.chatReferenceToggle,
      order: card.querySelector(".reference-order")?.textContent || "",
    }))
  ))).toEqual([
    { id: "asset-library-chat-order-b", order: "2" },
    { id: "asset-library-chat-order-a", order: "1" },
  ]);
  expect(imported).toEqual(["library-chat-order-b", "library-chat-order-a"]);
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
  await expect(page.locator("#modeSwitch")).toHaveCount(0);
  await expect(page.locator("#referenceStrip")).toBeVisible();
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

test("image library keeps new and existing padding images in selection order", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  await mockConfiguredImageChannel(page);
  const images = [
    libraryImage("library-padding-old", "padding-old.png", "/static/assets/brand-mark-v2.png"),
    libraryImage("library-padding-new", "padding-new.png", "/static/assets/starter-ocean-sky-reference.png"),
  ];
  const imported = await mockLibrarySelection(page, images);
  await page.route("**/api/generations", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    return route.fulfill({ status: 409, json: { error: "E2E 仅验证图片顺序" } });
  });
  await page.reload();
  await page.locator("#directGenerationButton").evaluate((button) => button.click());
  await expect(page.locator("#generationForm")).toBeVisible();
  await page.locator("#referenceLibrary").click();
  const checkboxes = page.locator("#libraryGrid [data-select-library-image]");
  await checkboxes.nth(0).check();
  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#libraryDialog")).toBeHidden();

  await page.locator("#referenceLibrary").click();
  await checkboxes.nth(0).uncheck();
  await checkboxes.nth(1).check();
  await checkboxes.nth(0).check();
  await expect(page.locator("#libraryGrid .reference-order")).toHaveText(["2", "1"]);
  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#referenceList .reference-order")).toHaveText(["2", "1"]);
  expect(imported).toEqual(["library-padding-old", "library-padding-new"]);

  await page.locator("#promptInput").fill("使用图1的风格修改图2");
  const request = page.waitForRequest((entry) => (
    entry.method() === "POST" && new URL(entry.url()).pathname === "/api/generations"
  ));
  await page.locator("#generateButton").click();
  expect((await request).postDataJSON().reference_ids).toEqual([
    "asset-library-padding-new", "asset-library-padding-old",
  ]);
});

test("image library pagination preserves canceled images and pending selection order", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const images = Array.from({ length: 61 }, (_value, index) => libraryImage(
    `library-page-order-${index}`, `page-order-${index}.png`, "/static/assets/brand-mark-v2.png",
  ));
  const imported = await mockLibrarySelection(page, images);
  await page.route("**/api/library-images?*", (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get("offset"));
    return route.fulfill({ json: {
      images: offset ? [images[0], images[60]] : images.slice(0, 60),
      total: images.length,
      has_more: offset === 0,
    } });
  });
  await page.locator("#libraryButton").click();
  const checkboxes = page.locator("#libraryGrid [data-select-library-image]");
  await checkboxes.nth(0).check();
  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#libraryDialog")).toBeHidden();

  await page.locator("#libraryButton").click();
  await checkboxes.nth(0).uncheck();
  await checkboxes.nth(1).check();
  await page.locator("#libraryLoadMoreButton").click();
  await expect(page.locator("#libraryGrid .library-card")).toHaveCount(61);
  await expect(checkboxes.nth(0)).not.toBeChecked();
  await expect(page.locator("#libraryGrid .reference-order")).toHaveText(["1"]);
  await checkboxes.nth(60).check();
  await expect(page.locator("#libraryGrid .reference-order")).toHaveText(["1", "2"]);
  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#chatReferenceCount")).toHaveText("2");
  await expect(page.locator("#chatReferenceList .reference-order")).toHaveText(["1", "2"]);
  expect(imported).toEqual([
    "library-page-order-0", "library-page-order-1", "library-page-order-60",
  ]);
});

test("image library numbering includes selected local references", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  await mockConfiguredImageChannel(page);
  const images = [libraryImage(
    "library-after-local", "after-local.png", "/static/assets/starter-ocean-sky-reference.png",
  )];
  await mockLibrarySelection(page, images);
  await page.route("**/api/workspaces/*/assets", (route) => route.fulfill({ json: {
    assets: [{
      id: "local-reference", name: "local-reference.png",
      url: "/static/assets/brand-mark-v2.png", mime_type: "image/png", bytes: 1024,
    }],
  } }));
  await page.reload();
  await page.locator("#directGenerationButton").evaluate((button) => button.click());
  await page.locator("#referenceInput").setInputFiles(path.resolve("static/assets/brand-mark-v2.png"));
  await expect(page.locator("#referenceList .reference-order")).toHaveText(["1"]);

  await page.locator("#referenceLibrary").click();
  await page.locator("#libraryGrid [data-select-library-image]").check();
  await expect(page.locator("#libraryGrid .reference-order")).toHaveText(["2"]);
  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#referenceList .reference-order")).toHaveText(["1", "2"]);
});

test("many padding images stay inside the generation drawer", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const images = Array.from({ length: 12 }, (_value, index) => libraryImage(
    `library-generation-overflow-${index}`,
    `generation-overflow-${index}.png`,
    "/static/assets/brand-mark-v2.png",
  ));
  await page.route("**/api/channels", (route) => route.fulfill({
    json: {
      version: "e2e-many-reference-channel",
      channels: [{
        id: "e2e-many-reference-channel",
        label: "E2E 多垫图渠道",
        enabled: true,
        configured: true,
        models: [{ id: "e2e-many-reference-model", label: "GPT Image 2" }],
        default_model: "e2e-many-reference-model",
        price_rmb: "0.0300",
        capabilities: {
          modes: ["text2img", "img2img"],
          max_reference_images: images.length,
          max_reference_image_mb: 10,
          max_reference_total_mb: 40,
          formats: ["png"],
        },
        limits: { max_concurrency: 2 },
      }],
    },
  }));
  await page.route("**/api/library-images?*", (route) => route.fulfill({
    json: { images, total: images.length, has_more: false },
  }));
  await page.route("**/api/workspaces/*/assets/from-library/*", async (route) => {
    const imageId = new URL(route.request().url()).pathname.split("/").at(-1);
    const image = images.find((entry) => entry.id === imageId);
    await route.fulfill({
      json: {
        asset: {
          ...image,
          id: `asset-${imageId}`,
          mime_type: "image/png",
          bytes: 1024,
        },
      },
    });
  });

  await page.reload();
  await page.locator("#directGenerationButton").evaluate((button) => {
    button.hidden = false;
    button.click();
  });
  await expect(page.locator("#generationForm")).toBeVisible();
  await page.locator("#referenceLibrary").click();
  await page.locator("#librarySelectAllButton").click();
  await page.locator("#libraryConfirmButton").click();
  await expect(page.locator("#libraryDialog")).toBeHidden();
  await expect(page.locator("#referenceList .reference-card.selected"))
    .toHaveCount(images.length);

  const layout = await page.evaluate(() => {
    const form = document.getElementById("generationForm");
    const strip = document.getElementById("referenceStrip");
    const lastCard = document.querySelector("#referenceList .reference-card:last-child");
    const stripBox = strip.getBoundingClientRect();
    const lastCardBox = lastCard.getBoundingClientRect();
    strip.scrollLeft = strip.scrollWidth;
    const scrolledLastCardBox = lastCard.getBoundingClientRect();
    return {
      drawerHasNoHorizontalOverflow: form.scrollWidth <= form.clientWidth + 1,
      stripFitsDrawer: stripBox.right <= form.getBoundingClientRect().right + 1,
      stripScrollsHorizontally: strip.scrollWidth > strip.clientWidth,
      lastCardVisibleAfterScroll: scrolledLastCardBox.right <= stripBox.right + 1,
    };
  });
  expect(layout).toEqual({
    drawerHasNoHorizontalOverflow: true,
    stripFitsDrawer: true,
    stripScrollsHorizontally: true,
    lastCardVisibleAfterScroll: true,
  });
});

test("reference thumbnails open a zoomable image preview with image exports", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  await mockConfiguredImageChannel(page);
  const images = [libraryImage(
    "library-generation-preview",
    "generation-preview.png",
    "/static/assets/starter-ocean-sky-reference.png",
  )];
  await mockLibrarySelection(page, images);
  await page.reload();
  await page.evaluate(() => {
    window.copiedImages = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        write: async (items) => {
          const blob = await items[0].getType("image/png");
          const bitmap = await createImageBitmap(blob);
          window.copiedImages.push({ type: blob.type, width: bitmap.width, height: bitmap.height });
          bitmap.close();
        },
      },
    });
  });
  await page.locator("#directGenerationButton").evaluate((button) => {
    button.hidden = false;
    button.click();
  });
  await expect(page.locator("#generationForm")).toBeVisible();
  await page.locator("#referenceLibrary").click();
  await page.locator("#libraryGrid [data-image-preview]").click();
  await expect(page.locator("#imageViewerDialog")).toBeVisible();
  const originalBytes = await readFile(path.resolve("static/assets/starter-ocean-sky-reference.png"));
  const verifyDownload = async () => {
    await expect(page.locator("#imageViewerDownload")).toBeEnabled();
    const downloadEvent = page.waitForEvent("download");
    await page.locator("#imageViewerDownload").click();
    const download = await downloadEvent;
    expect(download.suggestedFilename()).toBe(images[0].name);
    expect(await readFile(await download.path())).toEqual(originalBytes);
  };
  await verifyDownload();
  await page.locator("#imageViewerCopy").click();
  await expect(page.locator("#toastRegion")).toContainText("图片已复制到剪贴板");
  await page.locator('#imageViewerDialog [data-close-dialog="imageViewerDialog"]').click();
  await page.locator("#libraryGrid [data-select-library-image]").check();
  await page.locator("#libraryConfirmButton").click();
  const preview = page.locator("#referenceList [data-image-preview]");
  await expect(preview).toBeVisible();
  await preview.click();
  await expect(page.locator("#imageViewerDialog")).toBeVisible();
  await expect(page.locator("#imageViewerTitle")).toHaveText("放大预览 generation-preview.png");

  const initialZoom = await page.locator("#imageViewerZoomLabel").textContent();
  await page.locator("#imageViewerZoomIn").click();
  await expect(page.locator("#imageViewerZoomLabel")).not.toHaveText(initialZoom);
  await page.locator("#imageViewerCopy").click();
  await expect.poll(() => page.evaluate(() => window.copiedImages.length)).toBe(2);
  const originalSize = await page.locator("#imageViewerImage").evaluate((image) => ({
    type: "image/png", width: image.naturalWidth, height: image.naturalHeight,
  }));
  expect(await page.evaluate(() => window.copiedImages)).toEqual([originalSize, originalSize]);
  await verifyDownload();
  const toolbarFits = await page.locator(".image-viewer-toolbar").evaluate((toolbar) => {
    const bounds = toolbar.getBoundingClientRect();
    return [...toolbar.querySelectorAll("button, input")].filter((control) => !control.hidden)
      .every((control) => {
        const box = control.getBoundingClientRect();
        return box.left >= bounds.left && box.right <= bounds.right
          && box.top >= bounds.top && box.bottom <= bounds.bottom;
      });
  });
  expect(toolbarFits).toBe(true);
  const stage = page.locator("#imageViewerStage");
  await expect(stage).toHaveClass(/is-draggable/);
  const stageBox = await stage.boundingBox();
  const transformBeforePan = await page.locator("#imageViewerImage").evaluate((image) => image.style.transform);
  await page.mouse.move(stageBox.x + stageBox.width / 2, stageBox.y + stageBox.height / 2);
  await page.mouse.down();
  await expect(stage).toHaveClass(/is-dragging/);
  await page.mouse.move(stageBox.x + stageBox.width / 2 + 80, stageBox.y + stageBox.height / 2 + 40);
  await page.mouse.up();
  await expect(stage).not.toHaveClass(/is-dragging/);
  const transformAfterPan = await page.locator("#imageViewerImage").evaluate((image) => image.style.transform);
  expect(transformAfterPan).not.toBe(transformBeforePan);
  await page.locator("#imageViewerFit").click();
  await expect(page.locator("#imageViewerDialog")).toBeVisible();
  await page.evaluate(() => {
    navigator.clipboard.write = async () => {
      throw new DOMException("Permission denied", "NotAllowedError");
    };
  });
  await page.locator("#imageViewerCopy").click();
  await expect(page.locator("#toastRegion")).toContainText("复制图片失败");
  await expect(page.locator("#imageViewerCopy")).toBeEnabled();
  await expect(page.locator("#imageViewerDownload")).toBeEnabled();
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { value: undefined }));
  await page.locator("#imageViewerCopy").click();
  await expect(page.locator("#toastRegion")).toContainText("当前浏览器不支持复制图片");
  await page.locator('#imageViewerDialog [data-close-dialog="imageViewerDialog"]').click();
  await expect(page.locator("#imageViewerDialog")).toBeHidden();
  await expect(page.locator("#referenceList .reference-card.selected")).toHaveCount(1);
});

test("image preview cancels downloads on close and mask changes", {
  tag: "@responsive",
}, async ({ studioPage: page }) => {
  const imageUrl = "/static/assets/starter-ocean-sky-reference.png";
  await mockLibrarySelection(page, [libraryImage("cancel-preview", "preview.png", imageUrl)]);
  await page.locator("#libraryButton").click();
  const preview = page.locator("#libraryGrid [data-image-preview]");
  await preview.evaluate((button) => {
    button.dataset.imagePreviewMaskUrl = "/static/assets/brand-mark-v2.png";
    button.dataset.imagePreviewMaskSource = "/static/assets/brand-mark-v2.png";
  });
  await preview.click();
  await expect(page.locator("#imageViewerDownload")).toBeEnabled();
  await page.evaluate(async () => {
    const originalFetch = window.fetch;
    const blob = await (await originalFetch(document.getElementById("imageViewerImage").src)).blob();
    window.pendingImageDownloads = [];
    window.fetch = (input, options) => {
      if (!options?.signal) return originalFetch(input, options);
      return new Promise((resolve) => {
        window.pendingImageDownloads.push({
          signal: options.signal,
          finish: () => resolve(new Response(blob)),
        });
      });
    };
  });
  const downloads = [];
  page.on("download", (download) => downloads.push(download));
  for (const [completed, action] of ["close", "mask"].entries()) {
    const index = await page.evaluate(() => window.pendingImageDownloads.length);
    await page.locator("#imageViewerDownload").click();
    await expect(page.locator("#imageViewerDownload")).toBeDisabled();
    if (action === "close") {
      await page.locator('#imageViewerDialog [data-close-dialog="imageViewerDialog"]').click();
      await preview.click();
    } else {
      await page.locator("#imageViewerMaskToggle").click();
    }
    await expect.poll(() => page.evaluate((i) => (
      window.pendingImageDownloads[i].signal.aborted
    ), index)).toBe(true);
    await expect(page.locator("#imageViewerDownload")).toBeEnabled();
    await page.locator("#imageViewerDownload").click();
    await page.evaluate(async (i) => {
      window.pendingImageDownloads[i].finish();
      await new Promise(window.requestAnimationFrame);
    }, index);
    await expect(page.locator("#imageViewerDownload")).toBeDisabled();
    expect(downloads).toHaveLength(completed);
    const downloadEvent = page.waitForEvent("download");
    await page.evaluate((i) => window.pendingImageDownloads[i + 1].finish(), index);
    await downloadEvent;
    await expect(page.locator("#imageViewerDownload")).toBeEnabled();
  }
  expect(downloads).toHaveLength(2);
  await expect(page.locator("#toastRegion")).not.toContainText("下载图片失败");
});

test("image preview handles encoding failure after clipboard denial", async ({ studioPage: page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mockLibrarySelection(page, [libraryImage(
    "encoding-preview", "preview.png", "/static/assets/starter-ocean-sky-reference.png",
  )]);
  await page.locator("#libraryButton").click();
  await page.locator("#libraryGrid [data-image-preview]").click();
  await expect(page.locator("#imageViewerCopy")).toBeEnabled();
  await page.evaluate(() => {
    HTMLCanvasElement.prototype.toBlob = (callback) => {
      window.setTimeout(() => {
        callback(null);
        window.pngEncodingFinished = true;
      }, 0);
    };
    Object.defineProperty(navigator, "clipboard", { value: {
      write: async () => { throw new DOMException("Permission denied", "NotAllowedError"); },
    } });
  });
  await page.locator("#imageViewerCopy").click();
  await expect(page.locator("#toastRegion")).toContainText("复制图片失败");
  await expect.poll(() => page.evaluate(() => window.pngEncodingFinished)).toBe(true);
  await expect(page.locator("#imageViewerCopy")).toBeEnabled();
  expect(errors).toEqual([]);
});
