# Legacy retirement and current ownership

The canonical runtime has no import, packaging or installation dependency on an
external AEE-MINI repository or `AEE_MASTER_PLAN.md`. The current tree retires 24
checks of those external archive files, plus the unused historical-fixture
acquisition README. This changes test ownership transparently; it does not
replace archive evidence, mark active failures skipped or rewrite Git history.
The pre-retirement full profile suite ran 519 tests: 6 failures and 18 errors,
all matching the retired external-file checks below.

| Paths/components | Decision | Dependency evidence |
|---|---|---|
| External AEE-MINI README/DEPRECATED/archive matrix and Master Plan existence checks | RETIRE | Test-only file reads; absent from runtime imports, entrypoints and package/install inputs |
| `aee/tests/fixtures/historical_docs/README.md` | RETIRE | Fixture acquisition instructions for the above checks; no current consumer |
| Current profile README assertions | MIGRATE | Validate maintained HTTP/profile reference; MCP README validates current onboarding |
| `full`, `mini`, `edge`, `developer`; CLI/installer/Docker/profile descriptors | KEEP | Supported parser, installer, dispatch/safety and CI dependencies |
| HTTP APIs, `dispatcher/`, `aee/adapters/`, deprecation API | KEEP | Maintained entrypoints/imports and upgrade contracts; name alone is not retirement evidence |
| `docs/MIGRATION_FROM_AEE_MINI.md`, Hermes adapter matrix | KEEP | Real deployed-host compatibility instructions and current adapter contract |
| `mcp_gateway.py`, `aee/mcp_runtime/`, hashed MCP closure, `config/p2c/` | KEEP | Canonical restricted runtime, broker, packaging, result and deployment dependencies |
| Existing public historical reports/probes outside onboarding | UNKNOWN / preserve | Not needed for current onboarding; insufficient evidence for deleting all historical consumers |
| Private engineering host evidence/diagnostic trees | Preserve at source/history | No runtime dependency; not copied into portable publication, never fabricated as target-host proof |

No runtime module, adapter, supported profile or historical public report is
deleted. Normal Git history retains the removed fixture README and test bodies.
No old tag/release is removed. Original engineering worktree remains untouched.

## Exact retired checks

- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniReadmeDeprecationNotice.test_aee_mini_readme_exists`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniReadmeDeprecationNotice.test_readme_contains_deprecation_marker`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniReadmeDeprecationNotice.test_readme_redirects_to_profile_mini`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniReadmeDeprecationNotice.test_readme_redirects_to_unified_repo`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniReadmeDeprecationNotice.test_readme_references_master_plan_section_21_10`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniReadmeDeprecationNotice.test_readme_states_frozen_at_1_0_1`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniReadmeDeprecationNotice.test_readme_preserves_original_title`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniReadmeIsShort.test_aee_mini_readme_line_count_is_small`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniReadmeIsShort.test_aee_mini_readme_does_not_document_adapter_contract`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniArchivePreserved.test_aee_mini_archive_matrix_still_exists`
- `aee/tests/test_aee99_documentation_migration.py::TestAeeMiniArchivePreserved.test_aee_mini_archive_matrix_is_nonempty`
- `aee/tests/test_aee99_documentation_migration.py::TestMasterPlanPathResolves.test_master_plan_path_exists`
- `aee/tests/test_aee99_documentation_migration.py::TestBothReadmesCoexist.test_aee_mini_readme_exists`
- `aee/tests/test_aee99_documentation_migration.py::TestBothReadmesCoexist.test_unified_readme_is_longer_than_aee_mini_readme`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestDeprecatedMd.test_file_exists_at_repo_root`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestDeprecatedMd.test_content_has_deprecated_marker`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestDeprecatedMd.test_content_mentions_version_1_0_1`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestDeprecatedMd.test_content_mentions_fresh_install`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestDeprecatedMd.test_content_mentions_profile_mini`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestDeprecatedMd.test_content_references_adr_009`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestDeprecatedMd.test_content_references_master_plan_section_21_10`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestDeprecatedMd.test_content_states_no_forced_migration`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestLegacyPathPreservation.test_deprecated_md_is_additive_not_replacing_readme`
- `aee/tests/test_aee9_10_deprecation_plan.py::TestLegacyPathPreservation.test_no_file_marked_readonly_by_this_slice`

## Compatibility and review

The stricter completed-result contract is carried forward from the identified
engineering source. Unsealed historical records cannot become accepted successes;
retain them privately and plan real-host state conversion separately. Current
migration tests still check stopped/private source, verified backup and fixture
rollback. Production approval, real host containment and ChatGPT E2E remain
independent Stage 3 gates. See [deployment](deployment.md).
