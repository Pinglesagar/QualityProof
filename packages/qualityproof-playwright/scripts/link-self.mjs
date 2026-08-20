#!/usr/bin/env node
/**
 * Symlink the package into its own node_modules.
 *
 * The example suite imports "@qualityproof/playwright" by its published name
 * rather than by relative path, so running it also verifies the `exports` map a
 * real consumer will resolve through. Without the link, the example would have
 * to import ../../dist/... and would stop testing the package boundary.
 */
import { mkdirSync, rmSync, symlinkSync, existsSync, lstatSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const scope = join(packageRoot, "node_modules", "@qualityproof");
const link = join(scope, "playwright");

mkdirSync(scope, { recursive: true });
if (existsSync(link) || (() => { try { lstatSync(link); return true; } catch { return false; } })()) {
  rmSync(link, { recursive: true, force: true });
}
symlinkSync(packageRoot, link, "dir");
console.log(`QualityProof: linked ${link} -> ${packageRoot}`);
