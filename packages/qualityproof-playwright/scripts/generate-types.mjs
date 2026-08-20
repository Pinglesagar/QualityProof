#!/usr/bin/env node
/**
 * Derive TypeScript types from the JSON Schema the Python engine exports.
 *
 * The direction of authority is the whole point: pydantic models are the
 * contract, so a change there becomes a TypeScript compile error here rather
 * than a silently divergent hand-written mirror.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(here, "..");
const repoRoot = resolve(packageRoot, "..", "..");
const schemaDir = join(repoRoot, "interop", "schemas");
const outputDir = join(packageRoot, "src", "generated");
const outputFile = join(outputDir, "manifest.ts");

function exportSchemas() {
  const python = existsSync(join(repoRoot, ".venv", "bin", "python"))
    ? join(repoRoot, ".venv", "bin", "python")
    : "python3";
  execFileSync(
    python,
    [
      "-c",
      "import sys,pathlib;sys.path.insert(0,'src');" +
        "from qualityproof.schema import export_schemas;" +
        `export_schemas(pathlib.Path(${JSON.stringify(schemaDir)}), 'v1')`,
    ],
    { cwd: repoRoot, stdio: "inherit" },
  );
}

async function main() {
  try {
    exportSchemas();
  } catch (error) {
    console.warn(`QualityProof: schema export skipped (${error.message}).`);
  }
  const schemaPath = join(schemaDir, "v1", "ExternalRunManifest.schema.json");
  if (!existsSync(schemaPath)) {
    console.warn("QualityProof: no exported schema found; keeping existing generated types.");
    return;
  }
  const { compile } = await import("json-schema-to-typescript");
  const schema = JSON.parse(readFileSync(schemaPath, "utf8"));
  const rendered = await compile(schema, "ExternalRunManifestSchema", {
    bannerComment:
      "/* eslint-disable */\n" +
      "/**\n * GENERATED FILE. Run `npm run schema` to refresh.\n" +
      " * Source of truth: qualityproof.models (pydantic) -> JSON Schema.\n */",
    additionalProperties: false,
    style: { singleQuote: false },
  });
  mkdirSync(outputDir, { recursive: true });
  writeFileSync(outputFile, rendered, "utf8");
  console.log(`QualityProof: wrote ${outputFile}`);
}

await main();
