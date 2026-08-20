import { expect, provenance, requirement, test } from "@qualityproof/playwright";

/**
 * Written the way the annotation API is meant to be used: the assertions are
 * ordinary Playwright, and traceability is one extra field.
 */
test(
  "catalogue lists products with accessible headings",
  {
    annotation: [
      requirement("CHECKOUT-014"),
      provenance({
        kind: "REQUIREMENT",
        source: "example/requirements.yaml",
        locator: "requirement:CHECKOUT-014",
      }),
    ],
  },
  async ({ page }) => {
    await page.goto("/products", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Products", level: 1 })).toBeVisible();
    await expect(page.getByRole("link", { name: "Cart" })).toBeVisible();
  },
);

test(
  "a product page exposes an accessible add-to-cart control",
  {
    annotation: [
      requirement("CHECKOUT-015"),
      provenance({
        kind: "REQUIREMENT",
        source: "example/requirements.yaml",
        locator: "requirement:CHECKOUT-015",
      }),
    ],
  },
  async ({ page, evidence }) => {
    await page.goto("/products/1", { waitUntil: "domcontentloaded" });
    // Role-based, so renaming the CSS hook cannot break this assertion. That is
    // the same locator doctrine the Python generator emits.
    const addToCart = page.getByRole("button", { name: "Add to cart" });
    await expect(addToCart).toBeVisible();
    await evidence("locator-strategy", { strategy: "role", role: "button" });
  },
);

test(
  "an untraceable test is still reported, but lands as UNKNOWN",
  {},
  async ({ page }) => {
    // Deliberately unannotated: the engine must classify this UNKNOWN rather
    // than crediting it, which is what makes the ledger's zero-config default
    // honest in both languages.
    await page.goto("/help", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Help" })).toBeVisible();
  },
);
