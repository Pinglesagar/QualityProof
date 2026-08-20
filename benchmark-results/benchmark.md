# QualityProof controlled demo benchmark

- Journeys discovered: 8
- QualityProof precision: 0.818
- QualityProof recall: 1.000
- Matched finding IDs: SEED-A11Y-001, SEED-AUTHZ-001, SEED-JOURNEY-001, SEED-LAYOUT-001, SEED-LINK-001, SEED-LOCATOR-001, SEED-SAFETY-001, SEED-TOTAL-001, SEED-VALIDATION-001
- Missed finding IDs: none
- False-positive signals: execution_finding:journey-45b88468bc87, execution_finding:journey-c98448123e49
- Route retargets (one finding, not two): route_retargeted:/help->/missing-help
- Context changes (not scored as findings): application_metadata_changed:demo_version, route_removed:/help
- Signals rejected for wrong cause: none
- Fixture integrity checks (not product detections): 9/9 passed
- Assertions by provenance: {"OBSERVATION": 32, "REQUIREMENT": 21, "UNATTRIBUTED": 6}
- Ledger unknown count: 1
- Discovery unknown count: 4
- Benchmark runtime: 0.050409 seconds

Direct fixture checks are setup integrity checks, not QualityProof findings. No third-party tool was run and no comparative claim is made.

Every observed change is emitted. Application-metadata differences are reported as context because they identify which releases were compared, and a retargeted link is counted once because one referrer changed one destination; an unpaired route removal is still scored as a removal.

**What the recall figure does and does not mean.** Of the nine seeded defects, SEED-SAFETY-001 is identical in both releases and is 'detected' by the crawler reporting its own refusal to activate a destructive control, so it measures the guard rather than a regression. SEED-VALIDATION-001 and SEED-TOTAL-001 are detected because the workflow runs two hand-authored tests that assert those specific behaviours and observes them flip from pass to fail; that is a genuine execution finding but it is not discovery. The six remaining detections are both regressions and discoveries. Read recall as 9/9 of a fixture the author wrote, not as a general detection rate.
