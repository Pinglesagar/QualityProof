#!/usr/bin/env node
/** CLI bridge: analyse specs, verify a manifest, or merge shard manifests. */
import { readFileSync, writeFileSync } from "node:fs";
import { argv, exit, stderr, stdout } from "node:process";

const [command, ...rest] = argv.slice(2);

function usage() {
  stdout.write(
    "Usage:\n" +
      "  qualityproof-ts analyze <spec.ts...>        Report tests, assertions and annotations\n" +
      "  qualityproof-ts verify <manifest.json>      Validate a manifest against the contract\n" +
      "  qualityproof-ts merge <out.json> <in...>    Combine shard manifests deterministically\n",
  );
}

const { analyzeFile } = await import("../dist/analyze.js");
const { mergeManifests } = await import("../dist/manifest.js");
const { MANIFEST_SCHEMA_VERSION } = await import("../dist/types.js");

if (command === "analyze") {
  if (rest.length === 0) {
    usage();
    exit(2);
  }
  const results = rest.flatMap((path) =>
    analyzeFile(path).map((item) => ({ path, ...item })),
  );
  stdout.write(`${JSON.stringify(results, null, 2)}\n`);
} else if (command === "verify") {
  const [path] = rest;
  if (!path) {
    usage();
    exit(2);
  }
  const manifest = JSON.parse(readFileSync(path, "utf8"));
  const problems = [];
  if (manifest.schema_version !== MANIFEST_SCHEMA_VERSION) {
    problems.push(`schema_version must be ${MANIFEST_SCHEMA_VERSION}`);
  }
  if (manifest.redacted !== true) {
    problems.push("redacted must be true before ingest");
  }
  const ids = new Set();
  for (const test of manifest.tests ?? []) {
    if (ids.has(test.id)) problems.push(`duplicate test id: ${test.id}`);
    ids.add(test.id);
  }
  if (problems.length > 0) {
    stderr.write(`${problems.join("\n")}\n`);
    exit(1);
  }
  stdout.write(`ok: ${ids.size} tests, schema ${manifest.schema_version}\n`);
} else if (command === "merge") {
  const [output, ...inputs] = rest;
  if (!output || inputs.length === 0) {
    usage();
    exit(2);
  }
  const merged = mergeManifests(inputs.map((path) => JSON.parse(readFileSync(path, "utf8"))));
  writeFileSync(output, `${JSON.stringify(merged, null, 2)}\n`, "utf8");
  stdout.write(`merged ${inputs.length} manifests into ${output}\n`);
} else {
  usage();
  exit(command ? 2 : 0);
}
