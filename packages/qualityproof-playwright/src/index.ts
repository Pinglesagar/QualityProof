/**
 * Public API: Playwright's own `test` and `expect`, plus typed annotations.
 *
 * The wrapper is intentionally thin. Replacing Playwright's runner would mean
 * reimplementing fixtures, parallelism, retries and tracing badly; extending it
 * means a team keeps everything it already knows and gains requirement
 * traceability. The reporter reads these annotations after the fact, so a test
 * that forgets them still runs — it simply lands in the ledger as UNKNOWN,
 * exactly like an unannotated Python test.
 */

import { test as base, expect } from "@playwright/test";
import type { TestInfo } from "@playwright/test";

export { expect };
export type { ExternalRunManifest, ExternalTestRecord, VerdictStatus } from "./types.js";

export const REQUIREMENT_ANNOTATION = "requirement";
export const PROVENANCE_ANNOTATION = "provenance";

export interface Annotation {
  type: string;
  description: string;
}

/** Link a test to a requirement identifier the evidence engine can resolve. */
export function requirement(id: string): Annotation {
  if (!id.trim()) {
    throw new Error("requirement id must not be empty");
  }
  return { type: REQUIREMENT_ANNOTATION, description: id.trim() };
}

export interface ProvenanceInput {
  kind: "REQUIREMENT" | "HUMAN_APPROVED" | "API_SPEC" | "BASELINE" | "OBSERVATION";
  source: string;
  locator?: string;
  contentHash?: string;
  approvedBy?: string;
  approvedAt?: string;
}

/**
 * Declare where a test's authority comes from.
 *
 * Encoded as a single annotation string because Playwright annotations are
 * `{type, description}` pairs; the engine parses it back into the same
 * Provenance record a Python decorator produces.
 */
export function provenance(input: ProvenanceInput): Annotation {
  if (!input.source.trim()) {
    throw new Error("provenance source must not be empty");
  }
  // The same invariant the Python engine enforces, applied at authoring time so
  // the failure surfaces in the test file rather than at the ingest boundary
  // after a full browser run has already been spent.
  if (
    (input.kind === "REQUIREMENT" || input.kind === "API_SPEC") &&
    !input.locator &&
    !input.contentHash
  ) {
    throw new Error(
      `${input.kind} provenance requires a locator or contentHash so the engine can resolve it`,
    );
  }
  const parts = [`${input.kind}:${input.source.trim()}`];
  if (input.locator) parts.push(`locator=${input.locator}`);
  if (input.contentHash) parts.push(`content_hash=${input.contentHash}`);
  if (input.approvedBy) parts.push(`approved_by=${input.approvedBy}`);
  if (input.approvedAt) parts.push(`approved_at=${input.approvedAt}`);
  return { type: PROVENANCE_ANNOTATION, description: parts.join(";") };
}

export interface QualityProofFixtures {
  /** Attach structured evidence that the reporter hoists into the manifest. */
  evidence: (name: string, body: unknown) => Promise<void>;
}

export const test = base.extend<QualityProofFixtures>({
  evidence: async ({}, use, testInfo: TestInfo) => {
    await use(async (name: string, body: unknown) => {
      await testInfo.attach(`qualityproof:${name}`, {
        body: JSON.stringify(body, null, 2),
        contentType: "application/json",
      });
    });
  },
});
