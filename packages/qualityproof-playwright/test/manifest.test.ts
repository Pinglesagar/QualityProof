import { describe, expect, it } from "vitest";

import {
  buildManifest,
  isUnredactable,
  mergeManifests,
  splitArtifacts,
  statusFromOutcome,
} from "../src/manifest.js";
import type { ExternalTestRecord } from "../src/types.js";

function record(overrides: Partial<ExternalTestRecord> = {}): ExternalTestRecord {
  return {
    id: "spec.ts::t",
    path: "spec.ts",
    name: "t",
    line: 1,
    framework: "playwright-typescript",
    status: "pass",
    duration_ms: 10,
    requirement_ids: [],
    assertions: [],
    provenance: [],
    attachments: [],
    quarantined_attachments: [],
    ...overrides,
  };
}

describe("statusFromOutcome", () => {
  it("maps Playwright outcomes onto the engine's verdict vocabulary", () => {
    expect(statusFromOutcome("expected")).toBe("pass");
    expect(statusFromOutcome("unexpected")).toBe("fail");
    expect(statusFromOutcome("skipped")).toBe("inconclusive");
  });

  it("preserves flaky as its own verdict rather than collapsing to pass", () => {
    // Playwright knows natively that a test needed a retry. Reporting that as a
    // pass is how an unreliable suite looks healthy.
    expect(statusFromOutcome("flaky")).toBe("flaky");
  });

  it("refuses to guess at an unknown outcome", () => {
    expect(statusFromOutcome("something-new")).toBe("not_run");
  });
});

describe("splitArtifacts", () => {
  it("quarantines unredactable artifacts on an authenticated run", () => {
    const split = splitArtifacts(["trace.zip", "shot.png", "stdout.txt"], true);

    expect(split.quarantined).toEqual(["shot.png", "trace.zip"]);
    expect(split.attachments).toEqual(["stdout.txt"]);
  });

  it("publishes them when no session is present to leak", () => {
    const split = splitArtifacts(["trace.zip"], false);
    expect(split.attachments).toEqual(["trace.zip"]);
    expect(split.quarantined).toEqual([]);
  });

  it("recognises every unredactable suffix", () => {
    for (const path of ["a.png", "b.JPG", "c.zip", "d.webm"]) {
      expect(isUnredactable(path)).toBe(true);
    }
    expect(isUnredactable("e.txt")).toBe(false);
  });
});

describe("buildManifest", () => {
  it("redacts secrets sourced from the environment", () => {
    const manifest = buildManifest({
      runId: "run-1",
      startedAt: new Date("2026-01-01T00:00:00Z"),
      finishedAt: new Date("2026-01-01T00:01:00Z"),
      tests: [record({ name: "logs in as s3cr3t-token-value" })],
      env: { QUALITYPROOF_PASSWORD: "s3cr3t-token-value" },
    });

    expect(JSON.stringify(manifest)).not.toContain("s3cr3t-token-value");
    expect(manifest.tests[0]!.name).toContain("<REDACTED>");
    expect(manifest.redacted).toBe(true);
  });

  it("records artifact policy and defaults closed when authenticated", () => {
    const manifest = buildManifest({
      runId: "run-1",
      startedAt: new Date(0),
      finishedAt: new Date(0),
      tests: [],
      env: { QUALITYPROOF_STORAGE_STATE: "auth.json" },
    });

    expect(manifest.artifact_policy).toContain("artifacts=off");
    expect(manifest.artifact_policy).toContain("authenticated=true");
  });

  it("orders tests deterministically so two identical runs match byte for byte", () => {
    const options = {
      runId: "run-1",
      startedAt: new Date(0),
      finishedAt: new Date(0),
      env: {},
    };
    const first = buildManifest({
      ...options,
      tests: [record({ id: "b" }), record({ id: "a" })],
    });
    const second = buildManifest({
      ...options,
      tests: [record({ id: "a" }), record({ id: "b" })],
    });

    expect(JSON.stringify(first)).toBe(JSON.stringify(second));
  });
});

describe("mergeManifests", () => {
  it("reports a test that passed in one shard and failed in another as flaky", () => {
    const base = {
      schema_version: "qualityproof-external-run/v1" as const,
      framework: "playwright-typescript" as const,
      redacted: true,
      artifact_policy: null,
    };
    const merged = mergeManifests([
      {
        ...base,
        run_id: "s1",
        shard: "1/2",
        started_at: "2026-01-01T00:00:00.000Z",
        finished_at: "2026-01-01T00:01:00.000Z",
        tests: [record({ status: "fail" })],
      },
      {
        ...base,
        run_id: "s2",
        shard: "2/2",
        started_at: "2026-01-01T00:00:30.000Z",
        finished_at: "2026-01-01T00:02:00.000Z",
        tests: [record({ status: "pass" })],
      },
    ]);

    expect(merged.tests).toHaveLength(1);
    expect(merged.tests[0]!.status).toBe("flaky");
    expect(merged.tests[0]!.duration_ms).toBe(20);
    expect(merged.started_at).toBe("2026-01-01T00:00:00.000Z");
    expect(merged.finished_at).toBe("2026-01-01T00:02:00.000Z");
    expect(merged.shard).toBeNull();
  });

  it("refuses to merge nothing", () => {
    expect(() => mergeManifests([])).toThrow(/at least one/);
  });
});
