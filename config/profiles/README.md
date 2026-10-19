# AEE deployment profiles (v1)

Two first-class, machine-checkable deployment profiles for the AEE MCP
gateway. The profile is an explicit operator choice — it is recorded in the
install receipt, reported by `aee_status` gateway diagnostics
(`gateway.deployment_profile`), validated against the schema, and covered by
the fresh-host parity tests.

| | `os-native` (default) | `restricted` (explicit opt-in) |
|---|---|---|
| Service model | systemd **user** service under an existing OS identity that owns the agent toolchain | dedicated system accounts + broker + workspace snapshot + manifests |
| Working directory | any absolute existing dir accessible by the executor identity; **no mandatory allowlist** | explicit `A3_DISPATCH_ALLOWED_ROOTS` confinement |
| Adapter resolution | agent CLIs resolved from the service identity (`A3_ADAPTER_<NAME>_BIN` override or service PATH) | broker-managed `runner/codex` snapshot contract |
| Isolation boundary | OS DAC/ACL/MAC + adapter sandboxes; gateway+workers share the service UID (documented residual risk; state must be 0700/0600) | systemd sandbox (`ProtectHome=yes`, `ProtectSystem=strict`) as the unit boundary |
| Trusted write/autonomous | only via provisioned trusted-dispatch policy (fail-closed otherwise) | excluded by the broker contract design |
| Files | `os-native.env.example`, `systemd/aee-gateway-user.service.example` | `restricted.env.example` + `config/p2c/*` |

Schema: [`profile-schema.json`](profile-schema.json) — required/optional/fixed
env keys per profile, secret placeholder rules, forbidden cross-profile
artifacts. Validated by `scripts/validate_profiles.py` (CI + installer
preflight).

Selection:

```bash
scripts/install-aee-gateway.sh --profile os-native --release-dir <release> \
  --env-path <0600-env> --check
```

See `docs/deployment-profiles.md` for semantics, threat model, rollback and
cold-boot behavior. SSH is a human debugging convenience only — never an AEE
runtime, transport or installer dependency.