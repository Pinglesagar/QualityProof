/**
 * Static analysis of Playwright Test specs, using the TypeScript compiler API.
 *
 * This is the TypeScript counterpart of the Python auditor's `ast` walk, and it
 * obeys the same rule: the source is parsed, never executed. Importing a spec to
 * discover its assertions would run module-level code from the system under
 * test's own repository, which is precisely the trust boundary the Python side
 * refuses to cross.
 */

import { readFileSync } from "node:fs";
import ts from "typescript";

import { PROVENANCE_ANNOTATION, REQUIREMENT_ANNOTATION } from "./index.js";
import type { Provenance, ProvenanceKind, SourceAssertion } from "./types.js";

export interface AnalyzedTest {
  name: string;
  line: number;
  assertions: SourceAssertion[];
  requirementIds: string[];
  provenance: Provenance[];
}

const PROVENANCE_KINDS = new Set<string>([
  "REQUIREMENT",
  "HUMAN_APPROVED",
  "API_SPEC",
  "BASELINE",
  "OBSERVATION",
  "AI_HYPOTHESIS",
]);

function lineOf(source: ts.SourceFile, node: ts.Node): number {
  return source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
}

/** True for `expect(...)`, `expect.soft(...)` and `expect.poll(...)` roots. */
function isExpectRoot(node: ts.Node): boolean {
  if (ts.isIdentifier(node)) {
    return node.text === "expect";
  }
  if (ts.isPropertyAccessExpression(node)) {
    return isExpectRoot(node.expression);
  }
  if (ts.isCallExpression(node)) {
    return isExpectRoot(node.expression);
  }
  return false;
}

/** True when a node is an *invoked* expect, e.g. `expect(x)` or `expect.soft(x)`. */
function isExpectInvocation(node: ts.Node): boolean {
  if (ts.isCallExpression(node)) {
    return isExpectRoot(node.expression);
  }
  if (ts.isPropertyAccessExpression(node)) {
    // Walks modifier chains such as `expect(x).not`.
    return isExpectInvocation(node.expression);
  }
  return false;
}

/**
 * Find the matcher call that terminates an expect chain.
 *
 * `expect(locator).toBeVisible()` is two nested calls; only the outer one is an
 * assertion. Counting both would inflate every assertion total, which for a
 * project that reports assertion counts as evidence would be quietly corrupting.
 *
 * The subject must be an *invoked* expect rather than merely rooted at `expect`,
 * because `expect.soft(locator)` has the identical shape to a matcher call and
 * would otherwise be counted as an assertion in its own right.
 */
function isMatcherCall(node: ts.CallExpression): boolean {
  return (
    ts.isPropertyAccessExpression(node.expression) &&
    isExpectInvocation(node.expression.expression)
  );
}

function parseProvenance(description: string): Provenance | null {
  const [head, ...rest] = description.split(";");
  if (!head) {
    return null;
  }
  const separator = head.indexOf(":");
  if (separator < 0) {
    return null;
  }
  const kind = head.slice(0, separator).trim();
  const source = head.slice(separator + 1).trim();
  if (!PROVENANCE_KINDS.has(kind) || !source) {
    return null;
  }
  const record: Provenance = {
    kind: kind as ProvenanceKind,
    source,
    captured_at: new Date(0).toISOString(),
  };
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

function stringProperty(node: ts.ObjectLiteralExpression, name: string): string {
  for (const property of node.properties) {
    if (!ts.isPropertyAssignment(property)) continue;
    if (property.name.getText().replace(/["']/g, "") !== name) continue;
    return ts.isStringLiteralLike(property.initializer) ? property.initializer.text : "";
  }
  return "";
}

/**
 * Resolve one annotation entry, in either supported form.
 *
 * Both the literal `{ type, description }` pair and the exported
 * `requirement()` / `provenance()` helpers are public API, so an auditor that
 * understood only the literal form would silently report every ergonomically
 * written test as untraceable. The helpers are resolved statically — their
 * shapes are fixed by this package, so no execution is needed to know what they
 * produce.
 */
function resolveAnnotation(
  entry: ts.Expression,
): { type: string; description: string } | null {
  if (ts.isObjectLiteralExpression(entry)) {
    return {
      type: stringProperty(entry, "type"),
      description: stringProperty(entry, "description"),
    };
  }
  if (!ts.isCallExpression(entry) || !ts.isIdentifier(entry.expression)) {
    return null;
  }
  const helper = entry.expression.text;
  const argument = entry.arguments[0];
  if (helper === "requirement") {
    if (!argument || !ts.isStringLiteralLike(argument)) return null;
    return { type: REQUIREMENT_ANNOTATION, description: argument.text };
  }
  if (helper === "provenance") {
    if (!argument || !ts.isObjectLiteralExpression(argument)) return null;
    const kind = stringProperty(argument, "kind");
    const source = stringProperty(argument, "source");
    if (!kind || !source) return null;
    const parts = [`${kind}:${source}`];
    for (const [field, key] of [
      ["locator", "locator"],
      ["contentHash", "content_hash"],
      ["approvedBy", "approved_by"],
      ["approvedAt", "approved_at"],
    ] as const) {
      const value = stringProperty(argument, field);
      if (value) parts.push(`${key}=${value}`);
    }
    return { type: PROVENANCE_ANNOTATION, description: parts.join(";") };
  }
  return null;
}

function readAnnotations(node: ts.CallExpression): {
  requirementIds: string[];
  provenance: Provenance[];
} {
  const requirementIds: string[] = [];
  const provenance: Provenance[] = [];
  for (const argument of node.arguments) {
    if (!ts.isObjectLiteralExpression(argument)) continue;
    for (const property of argument.properties) {
      if (!ts.isPropertyAssignment(property)) continue;
      if (property.name.getText() !== "annotation") continue;
      const value = property.initializer;
      const entries = ts.isArrayLiteralExpression(value) ? value.elements : [value];
      for (const entry of entries) {
        const resolved = resolveAnnotation(entry);
        if (!resolved) continue;
        const { type, description } = resolved;
        if (type === REQUIREMENT_ANNOTATION && description) {
          requirementIds.push(description);
        }
        if (type === PROVENANCE_ANNOTATION && description) {
          const parsed = parseProvenance(description);
          if (parsed) provenance.push(parsed);
        }
      }
    }
  }
  return { requirementIds, provenance };
}

function isTestCall(node: ts.CallExpression): boolean {
  const callee = node.expression;
  if (ts.isIdentifier(callee)) {
    return callee.text === "test" || callee.text === "it";
  }
  if (ts.isPropertyAccessExpression(callee)) {
    // test.only / test.fixme etc. still declare a test.
    return ts.isIdentifier(callee.expression) && callee.expression.text === "test";
  }
  return false;
}

/** Parse one spec file and report each declared test with its assertions. */
export function analyzeSource(fileName: string, sourceText: string): AnalyzedTest[] {
  const source = ts.createSourceFile(fileName, sourceText, ts.ScriptTarget.ES2022, true);
  const tests: AnalyzedTest[] = [];

  const collectAssertions = (body: ts.Node): SourceAssertion[] => {
    const found: SourceAssertion[] = [];
    const walk = (node: ts.Node): void => {
      if (ts.isCallExpression(node) && isMatcherCall(node)) {
        found.push({
          kind: "expect",
          line: lineOf(source, node),
          expression: node.getText(source).replace(/\s+/g, " ").trim(),
        });
      }
      ts.forEachChild(node, walk);
    };
    ts.forEachChild(body, walk);
    return found.sort((a, b) => a.line - b.line);
  };

  const walk = (node: ts.Node): void => {
    if (ts.isCallExpression(node) && isTestCall(node)) {
      const titleNode = node.arguments[0];
      const title = titleNode && ts.isStringLiteralLike(titleNode) ? titleNode.text : "";
      const body = node.arguments.find(
        (argument) => ts.isArrowFunction(argument) || ts.isFunctionExpression(argument),
      );
      if (title && body) {
        const { requirementIds, provenance } = readAnnotations(node);
        tests.push({
          name: title,
          line: lineOf(source, node),
          assertions: collectAssertions(body),
          requirementIds,
          provenance,
        });
      }
    }
    ts.forEachChild(node, walk);
  };

  ts.forEachChild(source, walk);
  return tests;
}

export function analyzeFile(path: string): AnalyzedTest[] {
  return analyzeSource(path, readFileSync(path, "utf8"));
}
