import { describe, expect, it } from "vitest";

import { MIN_REDACTABLE_LENGTH, Redactor, environmentIsAuthenticated } from "../src/redact.js";

describe("Redactor", () => {
  it("removes known secret values and credential shapes", () => {
    const redactor = new Redactor(["super-secret-value"]);

    expect(redactor.text("token=super-secret-value")).toBe("token=<REDACTED>");
    expect(redactor.text("Authorization: Bearer abc.def-123")).toContain("Bearer <REDACTED>");
    expect(redactor.text("https://user:pw@example.test/x")).toBe(
      "https://<REDACTED>@example.test/x",
    );
  });

  it("word-bounds short secrets so unrelated text survives", () => {
    // A two-character secret substituted everywhere would corrupt the evidence
    // it is meant to protect, making it useless for diagnosis.
    const redactor = new Redactor(["ab"]);

    expect(redactor.text("ab about abbey")).toBe("<REDACTED> about abbey");
    expect("ab".length).toBeLessThan(MIN_REDACTABLE_LENGTH);
  });

  it("masks sensitive keys by name regardless of value", () => {
    const redactor = new Redactor([]);

    expect(redactor.value({ Cookie: "session=1", note: "fine" })).toEqual({
      Cookie: "<REDACTED>",
      note: "fine",
    });
  });

  it("collects secrets from environment variable names, including usernames", () => {
    const redactor = Redactor.fromEnvironment({
      QUALITYPROOF_PASSWORD: "long-password-value",
      QUALITYPROOF_USERNAME: "person@example.test",
      HOME: "/Users/someone",
    });

    expect(redactor.secrets).toContain("long-password-value");
    expect(redactor.secrets).toContain("person@example.test");
    expect(redactor.secrets).not.toContain("/Users/someone");
  });
});

describe("environmentIsAuthenticated", () => {
  it("treats a storage state or any secret as authenticated", () => {
    expect(environmentIsAuthenticated({ QUALITYPROOF_STORAGE_STATE: "a.json" })).toBe(true);
    expect(environmentIsAuthenticated({ QUALITYPROOF_TOKEN: "x" })).toBe(true);
    expect(environmentIsAuthenticated({ HOME: "/tmp" })).toBe(false);
  });
});
