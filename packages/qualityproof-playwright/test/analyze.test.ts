import { describe, expect, it } from "vitest";

import { analyzeSource } from "../src/analyze.js";

const SPEC = `
import { expect, provenance, requirement, test } from "@qualityproof/playwright";

test("checkout total", {
  annotation: [
    { type: "requirement", description: "CHECKOUT-014" },
    { type: "provenance", description: "REQUIREMENT:docs/requirements.md;locator=requirement:CHECKOUT-014" },
  ],
}, async ({ page }) => {
  await page.goto("/checkout");
  await expect(page.getByTestId("total")).toHaveText("£99.99");
  await expect.soft(page.getByRole("button", { name: "Pay" })).toBeVisible();
});

test("untraceable", {}, async ({ page }) => {
  await page.goto("/help");
  await expect(page).toHaveTitle("Help");
});
`;

describe("analyzeSource", () => {
  it("reports each test with its assertions and annotations", () => {
    const tests = analyzeSource("checkout.spec.ts", SPEC);

    expect(tests.map((item) => item.name)).toEqual(["checkout total", "untraceable"]);
    expect(tests[0]!.requirementIds).toEqual(["CHECKOUT-014"]);
    expect(tests[0]!.provenance[0]).toMatchObject({
      kind: "REQUIREMENT",
      source: "docs/requirements.md",
      locator: "requirement:CHECKOUT-014",
    });
  });

  it("counts one assertion per matcher, not per nested call", () => {
    // expect(locator).toHaveText() is two CallExpressions; only the matcher is
    // an assertion. Double-counting would inflate reported evidence.
    const tests = analyzeSource("checkout.spec.ts", SPEC);

    expect(tests[0]!.assertions).toHaveLength(2);
    expect(tests[0]!.assertions.every((item) => item.kind === "expect")).toBe(true);
    expect(tests[0]!.assertions[0]!.expression).toContain("toHaveText");
  });

  it("counts soft assertions, which still record a failure", () => {
    const tests = analyzeSource("s.ts", SPEC);
    expect(tests[0]!.assertions[1]!.expression).toContain("toBeVisible");
  });

  it("reports an unannotated test rather than dropping it", () => {
    const tests = analyzeSource("checkout.spec.ts", SPEC);

    expect(tests[1]!.requirementIds).toEqual([]);
    expect(tests[1]!.assertions).toHaveLength(1);
  });

  it("never executes the source it analyses", () => {
    // A module-level throw would surface here if the analyzer imported instead
    // of parsing, which is the trust boundary the Python auditor also holds.
    const hostile = `throw new Error("module side effect");\ntest("x", {}, async () => {});`;
    expect(() => analyzeSource("hostile.spec.ts", hostile)).not.toThrow();
  });

  it("ignores a malformed provenance annotation instead of inventing one", () => {
    const spec = `test("t", { annotation: [{ type: "provenance", description: "NOPE" }] }, async () => {});`;
    const tests = analyzeSource("t.spec.ts", spec);
    expect(tests[0]!.provenance).toEqual([]);
  });
});

describe("expect chain shapes", () => {
  it("counts negated and polled matchers exactly once each", () => {
    const spec = `
test("shapes", {}, async ({ page }) => {
  await expect(page.getByRole("alert")).not.toBeVisible();
  await expect.poll(() => 1).toBe(1);
  await expect.soft(page.getByText("hi")).toBeVisible();
});`;

    const tests = analyzeSource("shapes.spec.ts", spec);

    expect(tests[0]!.assertions.map((item) => item.expression.split("(")[0])).toEqual([
      "expect",
      "expect.poll",
      "expect.soft",
    ]);
    expect(tests[0]!.assertions).toHaveLength(3);
  });

  it("does not treat a bare expect.soft subject as an assertion", () => {
    // expect.soft(x) has the same shape as a matcher call; only the terminating
    // matcher is an assertion.
    const spec = `test("t", {}, async ({ page }) => { const l = expect.soft(page); });`;
    const tests = analyzeSource("t.spec.ts", spec);
    expect(tests[0]!.assertions).toEqual([]);
  });
});

describe("annotation helper resolution", () => {
  it("reads the ergonomic requirement() and provenance() helpers", () => {
    // An auditor that only understood the literal form would report every
    // idiomatically written test as untraceable.
    const spec = `
import { expect, provenance, requirement, test } from "@qualityproof/playwright";

test("helper form", {
  annotation: [
    requirement("CHECKOUT-014"),
    provenance({ kind: "REQUIREMENT", source: "docs/requirements.md", locator: "requirement:CHECKOUT-014" }),
  ],
}, async ({ page }) => {
  await expect(page).toHaveTitle("x");
});`;

    const tests = analyzeSource("helper.spec.ts", spec);

    expect(tests[0]!.requirementIds).toEqual(["CHECKOUT-014"]);
    expect(tests[0]!.provenance[0]).toMatchObject({
      kind: "REQUIREMENT",
      source: "docs/requirements.md",
      locator: "requirement:CHECKOUT-014",
    });
  });

  it("ignores an unknown helper rather than inventing provenance", () => {
    const spec = `test("t", { annotation: [somethingElse("X")] }, async () => {});`;
    const tests = analyzeSource("t.spec.ts", spec);
    expect(tests[0]!.requirementIds).toEqual([]);
  });
});

describe("provenance helper invariants", () => {
  it("refuses a requirement provenance the engine could never resolve", async () => {
    const { provenance } = await import("../src/index.js");

    expect(() => provenance({ kind: "REQUIREMENT", source: "docs/x.md" })).toThrow(
      /requires a locator or contentHash/,
    );
    expect(() =>
      provenance({ kind: "REQUIREMENT", source: "docs/x.md", locator: "requirement:R-1" }),
    ).not.toThrow();
    // Observational provenance carries no such requirement.
    expect(() => provenance({ kind: "OBSERVATION", source: "crawl" })).not.toThrow();
  });
});
