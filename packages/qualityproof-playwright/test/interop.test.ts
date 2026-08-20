import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import Ajv from "ajv";
import addFormats from "ajv-formats";
import { describe, expect, it } from "vitest";

import { buildManifest } from "../src/manifest.js";
import { MANIFEST_SCHEMA_VERSION } from "../src/types.js";
import type { ExternalRunManifest } from "../src/types.js";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..", "..");
const fixtureDir = join(repoRoot, "interop", "fixtures");
const schemaPath = join(repoRoot, "interop", "schemas", "v1", "ExternalRunManifest.schema.json");

/**
 * Contract tests over fixtures both languages read.
 *
 * Neither side is trusted to describe the contract alone: these same files are
 * validated by pydantic in `tests/test_interop_contract.py`. If the two
 * implementations ever disagree, one of the suites fails rather than a malformed
 * manifest reaching the ledger.
 */
describe("interop contract", () => {
  const fixtures = readdirSync(fixtureDir).filter((name) => name.endsWith(".json"));

  it("ships fixtures for both sides to validate", () => {
    expect(fixtures.length).toBeGreaterThan(0);
  });

  it("validates every shared fixture against the exported JSON Schema", () => {
    const ajv = new Ajv({ strict: false, allErrors: true });
    addFormats(ajv);
    const validate = ajv.compile(JSON.parse(readFileSync(schemaPath, "utf8")));

    for (const name of fixtures) {
      const payload = JSON.parse(readFileSync(join(fixtureDir, name), "utf8"));
      const valid = validate(payload);
      expect(valid, `${name}: ${ajv.errorsText(validate.errors)}`).toBe(true);
    }
  });

  it("produces manifests that satisfy the schema the engine will enforce", () => {
    const ajv = new Ajv({ strict: false, allErrors: true });
    addFormats(ajv);
    const validate = ajv.compile(JSON.parse(readFileSync(schemaPath, "utf8")));
    const manifest = buildManifest({
      runId: "run-contract",
      startedAt: new Date("2026-01-01T00:00:00Z"),
      finishedAt: new Date("2026-01-01T00:00:05Z"),
      env: {},
      tests: [
        {
          id: "spec.ts::checkout",
          path: "spec.ts",
          name: "checkout",
          line: 4,
          framework: "playwright-typescript",
          status: "flaky",
          duration_ms: 120,
          requirement_ids: ["CHECKOUT-014"],
          assertions: [{ kind: "expect", line: 6, expression: "expect(x).toBeVisible()" }],
          provenance: [
            {
              kind: "REQUIREMENT",
              source: "docs/requirements.md",
              locator: "requirement:CHECKOUT-014",
              captured_at: "1970-01-01T00:00:00.000Z",
            },
          ],
          attachments: [],
          quarantined_attachments: [],
        },
      ],
    });

    expect(validate(manifest), ajv.errorsText(validate.errors)).toBe(true);
    expect(manifest.schema_version).toBe(MANIFEST_SCHEMA_VERSION);
  });

  it("rejects a manifest whose schema version drifted", () => {
    const ajv = new Ajv({ strict: false, allErrors: true });
    addFormats(ajv);
    const validate = ajv.compile(JSON.parse(readFileSync(schemaPath, "utf8")));
    const drifted = {
      ...(JSON.parse(
        readFileSync(join(fixtureDir, fixtures[0]!), "utf8"),
      ) as ExternalRunManifest),
      schema_version: "qualityproof-external-run/v2",
    };

    expect(validate(drifted)).toBe(false);
  });
});
