# Security policy

## Reporting

This is a pre-alpha personal project. Open a GitHub issue for anything
non-sensitive; for anything you would rather not post publicly, use GitHub's
private vulnerability reporting on the repository.

## Design commitments

- **Credentials never enter configuration.** `qualityproof.toml` rejects
  secret-shaped keys outright (`config.py::_reject_secret_keys`). Login values are
  read only from named environment variables.
- **Requests are refused before network I/O.** The crawler aborts cross-origin
  requests, denied routes, and any mutating method other than one exact
  configured login endpoint (`discovery.py::is_allowed_request`).
- **Controls are never activated.** Discovery reads structure; it does not click.
  Links labelled with destructive terms are recorded as blocked unknowns.
- **Evidence is redacted before it is written**, including HTTP failures, console
  output, page text and test stdout.
- **A model never drives the browser.** Crawler actions are fixed code. Model
  proposals are validated against persisted discovery twice, and AI-authored
  assertions cannot execute before human approval.

## Known residual risks

1. **Traces and screenshots cannot be redacted after capture.** A trace is a zip
   of DOM snapshots and network payloads. Capture is therefore off by default when
   any secret is present. Enabling it on an authenticated run requires
   `QUALITYPROOF_ALLOW_UNREDACTABLE_ARTIFACTS=1`, and the output is written to a
   `quarantine/` directory with an `UNREDACTED.md` marker and excluded from
   reports, snapshots and published output. It is isolated, not made safe.
2. **Redaction recognises known values and credential shapes.** A secret that
   never appears in the environment cannot be recognised. Values shorter than six
   characters are matched on word boundaries only, to avoid corrupting unrelated
   evidence.
3. **`scenarios/custom` is trusted executable code.** It is human-owned, read-only
   to every QualityProof command, and not sandboxed.
4. **Storage-state files contain live sessions.** `--save-storage-state` writes
   one deliberately; `.gitignore` excludes them. Never commit one.
5. **`VERIFIED` is not a correctness claim.** It means traceable and attributable.

## Out of scope

A hostile system under test attacking the browser process; malicious code placed
in `scenarios/custom`; compromise of the host running QualityProof.
