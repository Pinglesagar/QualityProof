# Evidence and audit model

QualityProof separates claims about intended behavior from evidence that behavior was observed.
This distinction is preserved in source audits, SQLite records, JSON Schemas, and reports.

## Provenance

Defining sources are `REQUIREMENT`, `HUMAN_APPROVED`, and `API_SPEC`. `AI_HYPOTHESIS` is treated
as defining only when `approved_by` and `approved_at` are both recorded. An `expires_at` timestamp
makes any source non-authoritative after that instant.

`HUMAN_APPROVED` records are invalid without both reviewer identity and approval time.
`REQUIREMENT` and `API_SPEC` records require either a source locator or a SHA-256 content hash.
Requirement loaders attach locators and hashes and reject mismatched supplied hashes. The
`validates_content` API allows integrations to validate independently loaded source bytes.

`BASELINE` records a comparison reference. `OBSERVATION` records something seen during execution
or review. Neither defines expected behavior, so these kinds cannot establish verification alone.
A passed test, screenshot, trace, or runtime report is therefore not requirement provenance.

## Static audit boundary

`qualityproof audit PATH` parses Python with the standard-library `ast` module. It recognizes:

- functions named `test_*`;
- functions explicitly decorated with QualityProof metadata;
- test methods in classes, with class metadata inherited;
- Python `assert` statements; and
- chained Playwright assertions rooted at `expect(...)`.

The auditor never imports or executes the target module. Metadata arguments must consequently be
literal lists, tuples, dictionaries, strings, numbers, booleans, or null values. Dynamic metadata
is left unclassified rather than evaluated.

## Classification

`VERIFIED` means the source contains an assertion, links at least one requirement ID, and cites at
least one active defining source. It does not mean the software is correct or that the test passed.

`PARTIAL` means metadata exists, but assertions, requirement links, or active defining provenance
are missing. This includes observation-only, baseline-only, expired, and unapproved-AI records.

`UNKNOWN` is the zero-configuration result when no usable metadata is present. Unknowns remain in
the ledger and reports; they are never silently upgraded.

## Storage and reports

SQLite `records` hold the latest materialized ledger entries. Scoped materialization manifests
atomically reconcile repeat audits, so deleted tests remove stale ledger entries without affecting
other record kinds. `audit_events` is append-only, sequence-ordered, and rejects duplicate event
IDs. Repository APIs support stable listing, prefix queries, event appends, and event listing.

`qualityproof report` reads the ledger and writes `.qualityproof/reports/ledger.json` plus a
standalone `.qualityproof/reports/ledger.html`. Both include the runtime-observation provenance
notice. Public Pydantic records are exported as deterministic JSON Schemas under the versioned
`v1` schema directory.
