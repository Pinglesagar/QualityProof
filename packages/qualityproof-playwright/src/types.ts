/**
 * Hand-maintained mirror of the QualityProof interop contract.
 *
 * `npm run schema` regenerates `src/generated/manifest.ts` from the JSON Schema
 * the Python engine exports, and `test/interop.test.ts` asserts this mirror and
 * the generated types describe the same shape. The generated file is the
 * authority; this one exists so the package type-checks before a schema export
 * has been run.
 */

export const MANIFEST_SCHEMA_VERSION = "qualityproof-external-run/v1" as const;

export type VerdictStatus = "pass" | "fail" | "flaky" | "inconclusive" | "not_run";

export type ExternalFramework = "playwright-typescript" | "playwright-python" | "pytest";

export type ProvenanceKind =
  | "REQUIREMENT"
  | "HUMAN_APPROVED"
  | "API_SPEC"
  | "BASELINE"
  | "OBSERVATION"
  | "AI_HYPOTHESIS";

export interface Provenance {
  kind: ProvenanceKind;
  source: string;
  locator?: string | null;
  captured_at: string;
  expires_at?: string | null;
  content_hash?: string | null;
  approved_by?: string | null;
  approved_at?: string | null;
}

export interface SourceAssertion {
  kind: "assert" | "expect";
  line: number;
  expression: string;
}

export interface ExternalTestRecord {
  id: string;
  path: string;
  name: string;
  line: number;
  framework: ExternalFramework;
  status: VerdictStatus;
  duration_ms: number;
  requirement_ids: string[];
  assertions: SourceAssertion[];
  provenance: Provenance[];
  attachments: string[];
  quarantined_attachments: string[];
}

export interface ExternalRunManifest {
  schema_version: typeof MANIFEST_SCHEMA_VERSION;
  run_id: string;
  framework: ExternalFramework;
  started_at: string;
  finished_at: string;
  shard?: string | null;
  artifact_policy?: string | null;
  redacted: boolean;
  tests: ExternalTestRecord[];
}
