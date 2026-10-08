# Historical documentation fixtures: acquisition required

The documentation-preservation tests require authentic external source material.
The material is currently unavailable in this checkout. This directory contains
no fabricated snapshots, and missing inputs are test failures.

Required portable fixture layout:

```text
historical_docs/
  AEE_MASTER_PLAN.md
  aee-runtime-api-mini/
    README.md
    DEPRECATED.md
    pyproject.toml
    docs/HERMES_ADAPTER_CONTRACT_MATRIX.md
```

Obtain the AEE-MINI frozen 1.0.1 documentation after the §21.9/§21.10 migration,
and the corresponding Master Plan. Preserve their substantive deprecation,
migration and archive content. The current repository-owned matrix or migration
guide cannot substitute for the external archive. `pyproject.toml` is required
for the existing legacy file-preservation/writability check.

Before adding a snapshot, record its source repository and immutable revision
(or a reviewable original source location) plus each source file's SHA-256 in a
companion provenance note. Review publication privacy. If redaction is needed,
record the original and derived hashes and the exact redaction mapping; do not
change contract content. Do not publish private paths, credentials or unrelated
historical evidence. Do not create fixture files from test assertions or weaken
missing-input failures. A snapshot tests the recorded archive state, not the
current state of an external host.
