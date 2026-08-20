# Controlled seeded shop

This application is intentionally small, local, and defective. It is QualityProof benchmark
input, not a production reference application. Both the application and its test data are original
Apache-2.0 project code.

## Safe demo credentials

**LOCAL DEMO ONLY. Never reuse these credentials or session design in production.**

- Normal role: `shopper@example.test` / `shopper-demo`
- Admin role: `admin@example.test` / `admin-demo`

Run either controlled version:

```console
uv run python -m demo.app --version v1 --port 8765
uv run python -m demo.app --version v2 --port 8765
```

`POST /__demo/reset` clears carts and profiles when called from localhost. Test-mode CAPTCHA content
is a first-party static `Type DEMO` field: no third-party CAPTCHA is contacted or bypassed.

The ground truth is [`seeded-defects.json`](seeded-defects.json). `v1` is the baseline and `v2` is
the candidate containing nine declared changes/seeds. The destructive-action seed exists in both
versions to prove the discovery safety guard; it must never be activated by discovery.
