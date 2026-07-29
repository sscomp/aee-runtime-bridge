# AEE Bootstrap v1 — Documentation Index

> **Spec reference:** `reports/aee_bootstrap_v1_spec.md` §16 W14, §17.3 Phase D

This directory contains the operator-facing documentation for AEE
Bootstrap v1 (Phase 7 / Phase D deliverable W14).

## Documents

| Document | Description | Audience |
|----------|-------------|----------|
| [operator-guide.md](operator-guide.md) | Quick start, profiles, CLI commands, release channels, version pinning, stage lifecycle, secrets, production safety, idempotency, rollback | Operators |
| [troubleshooting.md](troubleshooting.md) | Common issues, platform-specific issues, diagnostics, recovery procedures | Operators / Support |
| [offline-bundle.md](offline-bundle.md) | Building and using offline/air-gapped bundles | Operators on air-gapped hosts |

## Related Documents

- **Specification:** `reports/aee_bootstrap_v1_spec.md` — the full
  bootstrap v1 design document (planning artifact, read-only).
- **CLI reference:** `aee --help`, `aee install --help`,
  `aee doctor --help`, `aee update --help` — the authoritative CLI
  surface.
- **Exit codes:** spec §10.4 — the canonical exit-code table.
- **Health checks:** spec §11 — H1–H10 health check definitions.

## Phase History

| Phase | Work Items | Status |
|-------|-----------|--------|
| Phase A (Phase 2–4) | W1–W5: platform identity, lifecycle, doctor, install CLI, update CLI | Shipped |
| Phase B (Phase 5) | W6, W8, W10, W11, W12: POSIX trampoline, manifests, integration tests, container/macOS E2E | Shipped |
| Phase C (Phase 6) | W7, W13: Windows trampoline + E2E (experimental) | Shipped |
| **Phase D (Phase 7)** | **W9, W14, W15: release channels + ref pinning + drift detection, docs, acceptance gate** | **This phase** |

---

_End of documentation index._