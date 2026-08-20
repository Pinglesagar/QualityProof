/**
 * Build, redact and merge external run manifests.
 *
 * Merging exists because sharding is how Playwright is actually run in CI, and a
 * reporter that only works single-process quietly produces a partial ledger.
 * Merging is deterministic so two identical shard sets always produce a
 * byte-identical manifest.
 */

import { MANIFEST_SCHEMA_VERSION } from "./types.js";
import type { ExternalRunManifest, ExternalTestRecord, VerdictStatus } from "./types.js";
import { Redactor, environmentIsAuthenticated } from "./redact.js";

/** Playwright outcomes map onto the engine's verdict vocabulary one-to-one. */
const OUTCOME_TO_STATUS: Record<string, VerdictStatus> = {
  expected: "pass",
  unexpected: "fail",
  flaky: "flaky",
  skipped: "inconclusive",
};

export function statusFromOutcome(outcome: string): VerdictStatus {
  return OUTCOME_TO_STATUS[outcome] ?? "not_run";
}

/** Suffixes that cannot be redacted after capture and must not be published. */
const UNREDACTABLE = [".png", ".jpg", ".jpeg", ".zip", ".webp", ".webm"];

export function isUnredactable(path: string): boolean {
  const lower = path.toLowerCase();
  return UNREDACTABLE.some((suffix) => lower.endsWith(suffix));
}

export interface ArtifactSplit {
  attachments: string[];
  quarantined: string[];
}

/**
 * Separate publishable attachments from unredactable ones.
 *
 * A trace zip and a screenshot cannot be sanitised after the fact, so on an
 * authenticated run they are recorded as quarantined: still known about, never
 * treated as publishable evidence.
 */
export function splitArtifacts(paths: readonly string[], authenticated: boolean): ArtifactSplit {
  const attachments: string[] = [];
  const quarantined: string[] = [];
  for (const path of [...paths].sort()) {
    if (authenticated && isUnredactable(path)) {
      quarantined.push(path);
    } else {
      attachments.push(path);
    }
  }
  return { attachments, quarantined };
}

export interface BuildManifestOptions {
  runId: string;
  startedAt: Date;
  finishedAt: Date;
  tests: ExternalTestRecord[];
  shard?: string | null;
  env?: NodeJS.ProcessEnv;
}

export function buildManifest(options: BuildManifestOptions): ExternalRunManifest {
  const env = options.env ?? process.env;
  const redactor = Redactor.fromEnvironment(env);
  const authenticated = environmentIsAuthenticated(env);
  const acknowledged = env.QUALITYPROOF_ALLOW_UNREDACTABLE_ARTIFACTS === "1";
  const mode = authenticated && !acknowledged ? "off" : (env.QUALITYPROOF_ARTIFACTS ?? "on_failure");
  const manifest: ExternalRunManifest = {
    schema_version: MANIFEST_SCHEMA_VERSION,
    run_id: options.runId,
    framework: "playwright-typescript",
    started_at: options.startedAt.toISOString(),
    finished_at: options.finishedAt.toISOString(),
    shard: options.shard ?? null,
    artifact_policy: `artifacts=${mode} authenticated=${authenticated} quarantined=${
      authenticated && mode !== "off"
    }`,
    redacted: true,
    tests: [...options.tests].sort((a, b) => a.id.localeCompare(b.id)),
  };
  return redactor.value(manifest) as ExternalRunManifest;
}

/**
 * Combine shard manifests into one.
 *
 * A test seen as both failing and passing across shards is reported flaky, the
 * same rule the Python runner applies to reruns: two runners, one definition of
 * what counts as green.
 */
export function mergeManifests(manifests: readonly ExternalRunManifest[]): ExternalRunManifest {
  if (manifests.length === 0) {
    throw new Error("at least one manifest is required");
  }
  const first = manifests[0]!;
  const byId = new Map<string, ExternalTestRecord>();
  for (const manifest of manifests) {
    for (const test of manifest.tests) {
      const existing = byId.get(test.id);
      if (!existing) {
        byId.set(test.id, test);
        continue;
      }
      const statuses = new Set([existing.status, test.status]);
      const status: VerdictStatus =
        statuses.has("fail") && statuses.has("pass")
          ? "flaky"
          : statuses.has("fail")
            ? "fail"
            : statuses.has("flaky")
              ? "flaky"
              : existing.status;
      byId.set(test.id, {
        ...existing,
        status,
        duration_ms: existing.duration_ms + test.duration_ms,
        attachments: [...new Set([...existing.attachments, ...test.attachments])].sort(),
        quarantined_attachments: [
          ...new Set([...existing.quarantined_attachments, ...test.quarantined_attachments]),
        ].sort(),
      });
    }
  }
  const started = manifests
    .map((item) => item.started_at)
    .sort()
    .at(0)!;
  const finished = manifests
    .map((item) => item.finished_at)
    .sort()
    .at(-1)!;
  return {
    ...first,
    run_id: `merged-${manifests.map((item) => item.run_id).sort().join("+")}`,
    shard: null,
    started_at: started,
    finished_at: finished,
    tests: [...byId.values()].sort((a, b) => a.id.localeCompare(b.id)),
  };
}
