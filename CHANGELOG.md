# Changelog

## Unreleased — canonical deployment profiles (os-native default) and installer (2026-10-19)

- Formalize the two gateway deployment profiles in a machine-checked contract:
  `os-native` (default; systemd user unit under an existing OS identity,
  DAC/ACL boundary, no mandatory workspace allowlist) and `restricted`
  (explicit opt-in; dedicated system accounts, sandbox hardening, mandatory
  `A3_DISPATCH_ALLOWED_ROOTS`). Schema: `config/profiles/profile-schema.json`;
  shipped examples validated by `scripts/validate_profiles.py`.
- Add the canonical stdlib installer `scripts/install_aee_gateway.py`
  (+ `install-aee-gateway.sh` wrapper): fail-before-write preflight,
  idempotent apply (byte-identical inputs rewrite nothing; only changed
  installer-managed unit files are backed up), rollback by provenance-stamped
  stamp, uninstall (operator data never touched), unmanaged-collision refusal
  (verified by sidecar marker + unit hash), restricted profile stages the
  canonical P2C unit template and prints operator sudo commands without
  executing them, receipts carry env var names only.
- Add `scripts/provision_trusted_dispatch.py` (fingerprint-only policy
  onboarding with runtime re-validation) and `scripts/audit_adapters.py`
  (adapter resolution report; `A3_ADAPTER_<NAME>_BIN` documented).
- `mcp_gateway.py` aee_status gains an additive `gateway.deployment_profile`
  diagnostic (validated label, degrades to absent on invalid values).
- New documentation `docs/deployment-profiles.md`; CI gains a
  deployment-profiles job wired into the merge gate.

Documentation and CI only: no runtime behavior change beyond the additive
status diagnostic, no deployment, no service or host modification.

## Unreleased — documentation onboarding and privacy cleanup (2026-10-09)

- Add one-host Linux onboarding for AEE, OpenAI Secure MCP Tunnel and ChatGPT custom
  MCP Plugin, with smoke checks and troubleshooting.
- Replace captured private host paths and profile details with generic placeholders.
- Explain source availability, restricted tools, separate credentials and verification
  gaps; keep centralized updates and fleet orchestration on the roadmap.

Documentation only: no version bump, runtime installation, service change, tag or
release. Product metadata remains `2.0.0-rc1`; prior Stage 2B BLOCKED verdicts and
the separate production deployment gate are unchanged.
