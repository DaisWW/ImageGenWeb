const path = require("node:path");
const {
  expect,
  test,
  uploadLibraryImage,
} = require("./fixtures");

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
