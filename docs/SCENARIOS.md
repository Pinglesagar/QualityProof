# Scenario planning and execution

QualityProof mines stable root-to-leaf journeys from the persisted `PageState`/`ActionEdge` graph.
The default proposer is deterministic and makes no network calls. Optional requirements may be
Markdown or YAML. Generated drafts and approved scenarios are deliberately separate:

```text
scenarios/
  generated/drafts/       # machine-proposed, never executable
  generated/approved/     # human-approved source for generation
  custom/                 # human-owned trusted executable code; QualityProof never writes here
.qualityproof/
  generated/              # deterministic pytest output
  generated-custom/       # transient output for custom YAML
  runs/<run-id>/           # normalized result and failure evidence
```

## Workflow

```console
qualityproof plan --project . --requirements requirements.yaml
qualityproof review --project . --scenario scenarios/generated/drafts/journey-....yaml \
  --decision approve --actor alice --reason "Matches REQ-LOGIN"
qualityproof generate --project .
qualityproof test --project .
```

Omit review options for prompts. `--decision edit` opens `$EDITOR`, or accepts `--edited FILE`.
Every review persists actor, reason, timestamp, and decision and materializes the reviewed scenario
in SQLite in addition to appending an immutable event.

The v1 YAML format is language-neutral and uses discriminated `type` fields for steps and
assertions. Supported steps are `navigate`, `click`, `fill`, and `press`; supported assertions are
`visible`, `text`, `url`, and `title`. Generated Python has stable ordering and headers and must
pass `ast.parse`, Ruff, and `pytest --collect-only`.
Generation publishes through an ownership manifest. On a later run, outputs for deleted approved
YAML are removed only when the prior manifest identifies them; custom files are never candidates.

## Optional model proposer

Use `--provider http --endpoint URL --model MODEL` for an OpenAI-compatible chat endpoint or an
Ollama endpoint returning `message.content`/`response`. The API key is read only from
`QUALITYPROOF_LLM_API_KEY`. Calls have a bounded timeout, temperature zero, JSON-only validated
output, and prompt/template hashes. Reasoning is neither requested nor persisted.

`--cassette FILE` records the validated response envelope. Add `--replay` to use it without a
network call or API key. A mismatched request hash is rejected.

Model-proposed assertions remain `hypothesis_assertions` and cannot reach executable output.
Human approval adds approval provenance and promotes them to executable assertions.
Model outputs must retain the persisted candidate ID, route sequence, origin, and requirement
associations. Action and selector assertions must use exact semantic selectors persisted by
discovery; URL/title assertions must match discovered values. Generic or unknown selectors,
origin/route substitution, changed control semantics, and destructive semantic controls are
rejected during planning, rechecked during review, and rechecked from SQLite before generation.

Execution includes approved generated tests and human-owned custom Python/YAML tests. Custom Python
is trusted executable code with the same OS permissions as QualityProof. It is not parsed as data,
isolated, or sandboxed; review it before execution. QualityProof snapshots the custom tree before
its own writes and rejects generated output and cassette paths inside it.

Generated navigation is bound to the first absolute HTTP(S) origin. Cross-origin navigation,
credential-bearing URLs, destructive route names/click selectors, and submit-key actions are
rejected before review and again before generation. Runtime Playwright trace/screenshot capture is
off because authenticated page content cannot be reliably sanitized after capture. Text result
and evidence artifacts pass through centralized redaction.
Each normal `qualityproof test` run persists its run record and atomically replaces the current
per-test execution verdict set, making verdicts available to snapshots and release diffs.
