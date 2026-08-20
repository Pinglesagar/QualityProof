/**
 * A native Playwright Test reporter that emits QualityProof evidence.
 *
 * Being a reporter rather than a wrapper matters: Playwright still owns
 * execution, so sharding, retries, fixtures, projects and tracing all behave
 * exactly as a team already expects. This only observes the result and records
 * what can be defended afterwards.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import type {
  FullConfig,
  Reporter,
  TestCase,
  TestResult,
  FullResult,
} from "@playwright/test/reporter";

import { analyzeFile } from "./analyze.js";
import { buildManifest, splitArtifacts, statusFromOutcome } from "./manifest.js";
import { environmentIsAuthenticated } from "./redact.js";
import type { ExternalTestRecord, Provenance, SourceAssertion } from "./types.js";
import { PROVENANCE_ANNOTATION, REQUIREMENT_ANNOTATION } from "./index.js";

export interface QualityProofReporterOptions {
  /** Where the manifest is written, relative to the project root. */
  outputFile?: string;
  /**
   * Fail the run when a test declares no requirement.
   *
   * Off by default so adopting the reporter cannot break an existing suite. On,
   * it turns traceability from a report into a gate — the thing a governance
   * claim needs in order to mean anything in CI.
   */
  strictAnnotations?: boolean;
  /** Shard label, defaulting to Playwright's own shard configuration. */
  shard?: string;
}

function annotationValues(test: TestCase, type: string): string[] {
  return test.annotations
    .filter((annotation) => annotation.type === type && annotation.description)
    .map((annotation) => String(annotation.description));
}

const PROVENANCE_KINDS = new Set([
  "REQUIREMENT",
  "HUMAN_APPROVED",
  "API_SPEC",
  "BASELINE",
  "OBSERVATION",
  "AI_HYPOTHESIS",
]);

function parseProvenance(description: string): Provenance | null {
  const [head, ...rest] = description.split(";");
  if (!head) return null;
  const separator = head.indexOf(":");
  if (separator < 0) return null;
  const kind = head.slice(0, separator).trim();
  const source = head.slice(separator + 1).trim();
  if (!PROVENANCE_KINDS.has(kind) || !source) return null;
  const record = {
    kind,
    source,
    captured_at: new Date(0).toISOString(),
  } as Provenance;
  for (const part of rest) {
    const index = part.indexOf("=");
    if (index < 0) continue;
    const key = part.slice(0, index).trim();
    const value = part.slice(index + 1).trim();
    if (key === "locator") record.locator = value;
    if (key === "content_hash") record.content_hash = value;
    if (key === "approved_by") record.approved_by = value;
    if (key === "approved_at") record.approved_at = value;
  }
  return record;
}

export default class QualityProofReporter implements Reporter {
  private readonly options: QualityProofReporterOptions;
  private rootDir = process.cwd();
  private startedAt = new Date(0);
  private finishedAt = new Date(0);
  private shardLabel: string | null = null;
  private readonly records = new Map<string, ExternalTestRecord>();
  private readonly assertionCache = new Map<string, SourceAssertion[]>();
  private readonly missingAnnotations: string[] = [];

  constructor(options: QualityProofReporterOptions = {}) {
    this.options = options;
  }

  printsToStdio(): boolean {
    return false;
  }

  onBegin(config: FullConfig): void {
    this.rootDir = config.rootDir || process.cwd();
    this.startedAt = new Date();
    if (this.options.shard) {
      this.shardLabel = this.options.shard;
    } else if (config.shard) {
      this.shardLabel = `${config.shard.current}/${config.shard.total}`;
    }
  }

  /**
   * Read assertions from source, once per file.
   *
   * A runtime count would only see the assertions that executed: a test failing
   * on its first expect would appear to contain exactly one. Static analysis
   * reports what the test actually claims regardless of where it stopped.
   */
  private assertionsFor(file: string, name: string): SourceAssertion[] {
    if (!this.assertionCache.has(file)) {
      try {
        const analyzed = analyzeFile(file);
        for (const item of analyzed) {
          this.assertionCache.set(`${file}::${item.name}`, item.assertions);
        }
        this.assertionCache.set(file, []);
      } catch {
        this.assertionCache.set(file, []);
      }
    }
    return this.assertionCache.get(`${file}::${name}`) ?? [];
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    const file = test.location.file;
    const relativePath = relative(this.rootDir, file) || file;
    const identifier = `${relativePath}::${test.title}`;
    const requirementIds = annotationValues(test, REQUIREMENT_ANNOTATION);
    const provenance = annotationValues(test, PROVENANCE_ANNOTATION)
      .map(parseProvenance)
      .filter((item): item is Provenance => item !== null);
    const authenticated = environmentIsAuthenticated();
    const artifactPaths = result.attachments
      .map((attachment) => attachment.path)
      .filter((path): path is string => Boolean(path))
      .map((path) => relative(this.rootDir, path) || path);
    const { attachments, quarantined } = splitArtifacts(artifactPaths, authenticated);

    // Playwright reports the final outcome per test, having already accounted
    // for retries, so `flaky` arrives as a first-class outcome rather than
    // something to be reconstructed from attempt counts.
    const existing = this.records.get(identifier);
    const status = statusFromOutcome(test.outcome());
    this.records.set(identifier, {
      id: identifier,
      path: relativePath,
      name: test.title,
      line: test.location.line || 1,
      framework: "playwright-typescript",
      status,
      duration_ms: (existing?.duration_ms ?? 0) + Math.max(0, Math.round(result.duration)),
      requirement_ids: requirementIds,
      assertions: this.assertionsFor(file, test.title),
      provenance,
      attachments: [...new Set([...(existing?.attachments ?? []), ...attachments])].sort(),
      quarantined_attachments: [
        ...new Set([...(existing?.quarantined_attachments ?? []), ...quarantined]),
      ].sort(),
    });

    if (this.options.strictAnnotations && requirementIds.length === 0) {
      this.missingAnnotations.push(identifier);
    }
  }

  async onEnd(result: FullResult): Promise<{ status?: FullResult["status"] } | void> {
    this.finishedAt = new Date();
    const suffix = this.shardLabel ? `-${this.shardLabel.replace("/", "-of-")}` : "";
    const target = resolve(
      this.rootDir,
      this.options.outputFile ?? `.qualityproof/external/playwright-run${suffix}.json`,
    );
    const manifest = buildManifest({
      runId: `ts-${this.startedAt.toISOString().replace(/[:.]/g, "-")}${suffix}`,
      startedAt: this.startedAt,
      finishedAt: this.finishedAt,
      tests: [...this.records.values()],
      shard: this.shardLabel,
    });
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");

    if (this.missingAnnotations.length > 0) {
      const listed = this.missingAnnotations.sort().join(", ");
      process.stderr.write(
        `QualityProof: ${this.missingAnnotations.length} test(s) declare no requirement ` +
          `annotation and cannot be traced: ${listed}\n`,
      );
      return { status: "failed" };
    }
    return { status: result.status };
  }
}
