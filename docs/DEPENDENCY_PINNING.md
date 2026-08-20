# Dependency and image pinning

Python dependencies are resolved in `uv.lock`; CI and production builds use `uv sync --frozen`.
GitHub actions with locally verified release commits are pinned to full commit SHAs with their
release tags in comments.

The Playwright base matches the locked Python Playwright release and is pinned by tag plus the
multi-platform manifest digest returned directly by MCR's `Docker-Content-Digest` header. To update,
query the registry v2 manifest endpoint with the manifest-list Accept header (or inspect the pulled
image on a trusted Docker host), verify the digest from the registry response, update `FROM`, then
build and scan both production targets and run `scripts.image_smoke --launch-browser` in the job
image. If the registry digest cannot be verified, the update must stop; never invent or copy a
digest from an unauthenticated third-party source.

Action updates must resolve the intended upstream release tag in the action's own Git repository
and pin the resulting immutable 40-character commit. Dependabot or an equivalent reviewed update
process should keep these references current.

## Base image patching

The runtime stage applies `apt-get upgrade` over the pinned Playwright base
image. That is deliberate and it is not in tension with pinning: the digest pin
fixes *which* base image is used so builds are reproducible, while the patch layer
resolves fixable OS-package CVEs that accumulate in that image between upstream
releases.

The image scan gates on CRITICAL and HIGH with `ignore-unfixed: true`, so every
finding it reports is one a patch can resolve. When the scan fails, the fix is to
patch or to move the pin forward — never to widen the severity filter. A clean
scan we did not earn is worse than a red build, because it is a false statement
about the artifact we ship.
