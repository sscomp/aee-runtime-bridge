# Migration from AEE-MINI to AEE (Unified Product, `--profile mini`)

This document is the **operator migration guide** for deployments
currently running AEE-MINI `1.0.1`. It covers the canonical path from
AEE-MINI `1.0.1` to the unified AEE product at version `2.0.0` (and
later) running with `--profile mini`.

## Summary

| From | To | Path | Type |
|---|---|---|---|
| AEE-MINI `1.0.1` | AEE `2.0.0` | `install.sh --profile mini` | **Fresh install** (not in-place) |

There is **no in-place migration** from AEE-MINI `1.0.1` to AEE
`2.0.0`. The canonical path is a **fresh install** of the unified
product with `--profile mini`, deployed side-by-side, followed by a
cutover. AEE-MINI `1.0.1` is the **last release of the AEE-MINI line**
(per Master Plan §21.8 line 7796 and §21.10 line 7809).

## Canonical references

- **Master Plan:** `/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md`
  - §21.8 Release Strategy (line 7792)
  - §21.9 Documentation Migration (line 7798)
  - §21.10 Deprecation Plan (lines 7804–7815)
  - §21.A Acceptance Criteria (item 10)
- **Architecture decision:** ADR-009 (Master Plan §9) — Architecture
  Unification: one AEE product, four profiles (`full`, `mini`,
  `edge`, `developer`).
- **Unified product README:**
  `/home/ubuntu/hermes-runtime-bridge/README.md`
- **AEE-MINI deprecation marker:**
  `/home/ubuntu/Abacus/aee-runtime-api-mini/DEPRECATED.md`

## The `mini` profile

The `mini` profile is one of the four unified product profiles defined
by ADR-009. It **absorbs all of AEE-MINI's hardening** and is the
supported path forward for new deployments and for deployments
migrating from AEE-MINI:

- Idempotent pre-flight checks
- Dedicated `aee` system user
- `0600` permissions on the env file
- Smoke test on install

Install the unified product with the `mini` profile:

```bash
cd /home/ubuntu/hermes-runtime-bridge
./install.sh --profile mini
```

The `mini` profile string literal is the **only surviving reference**
to the "MINI" name after the AEE-MINI repo is archived (Master Plan
§21.10 line 7812).

## Deprecation timeline (4 rows)

Per Master Plan §21.10 (lines 7808–7814):

| Phase | Target version | Event |
|---|---|---|
| Epic 9 ship | `2.0.0-rc1` | AEE-MINI brand deprecated. AEE-MINI `1.0.1` is the last release of the line. Repo frozen (security patches only). `DEPRECATED.md` placed at AEE-MINI repo root. |
| Epic 9 + 1 | `2.0.0-rc2` | Unified installer's `--profile mini` validated end-to-end on a fresh host. B2 deployments may migrate. AEE-MINI install path still works. |
| Epic 9 + 2 | `2.0.0` GA | AEE-MINI install path **no longer supported** (not removed — script still exists, not tested in CI). New deployments must use the unified installer. |
| Epic 9 + 4 | `2.0.2` | AEE-MINI repo **archived** (marked read-only). `mini` profile string literal is the only surviving reference to the "MINI" name. |

## No forced migration

**No forced migration.** Any B2 deployment running AEE-MINI `1.0.1`
continues to run. The deprecation is about *new deployments* and *the
canonical path forward*, not about shutting down working services.

Operators are encouraged — not required — to move to the unified
product with `--profile mini` when their cadence allows. Existing
AEE-MINI `1.0.1` deployments remain supported on a best-effort basis
(security patches only) until the repo is archived at Epic 9 + 4
(`2.0.2`).

## Migration steps (operator checklist)

1. **Read** the Master Plan §21.10 and this guide end-to-end.
2. **Stand up** the unified product with `--profile mini` on a fresh
   host (or side-by-side on the existing host if resources allow).
3. **Validate** the `mini` profile end-to-end using the unified
   installer's smoke test (per §21.10 Epic 9 + 1 validation).
4. **Cutover** traffic from AEE-MINI `1.0.1` to the unified product.
5. **Leave AEE-MINI `1.0.1` in place** — do not delete or rename the
   AEE-MINI repo. Per §21.9, no documentation or code is deleted; the
   AEE-MINI repo remains on disk as the frozen archive.

## What is NOT happening

- No file in the AEE-MINI repo is deleted, renamed, or marked
  read-only by the deprecation marker. `DEPRECATED.md` is **additive**.
- No in-place upgrade path exists. The path is fresh install only.
- No forced cutover timeline. Existing deployments continue to run.
- No code, function, class, or module is removed from the unified
  product as a consequence of the deprecation. The `mini` profile
  **inherits** AEE-MINI's hardening; it does not lose it.