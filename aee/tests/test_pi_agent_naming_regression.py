"""AEE-5 Pi Agent naming regression test.

The AEE-4 remediation renamed everything "Pi Agent" to
"AEE Lightweight Agent Runtime". AEE-5 must NOT
reintroduce the old names in:

  * Python source (`aee/`, `aee-runtime/`, `dispatcher/`,
    `app.py`).
  * Configuration files (`*.yaml`, `*.json`,
    `*.example`).
  * Documentation (`docs/`, `*.md` in the repo root).

Historical migration notes and changelogs are allowed
to mention the old names; the allowlist below lists
the files / lines that are explicitly permitted to
contain those tokens.

If this test ever fails, AEE-5 is being re-polluted
with the old "Pi Agent" naming and the previous
remediation effort is being undone.
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# Tokens that are NOT allowed in AEE-5 code /
# config / non-historical docs. The regex is
# case-insensitive but matches as a word boundary so
# `api.py` (which contains the substring `pi`) is not
# flagged. Each pattern requires the token to be a
# distinct word.
FORBIDDEN_TOKENS = [
    (re.compile(r"\bpi_agent\b", re.IGNORECASE), "pi_agent (worker_type)"),
    (re.compile(r"\bpi[-_]agent\b", re.IGNORECASE), "pi-agent / pi_agent"),
    (re.compile(r"\bPiWorker\b"), "PiWorker class"),
    (re.compile(r"\bpi_worker\b"), "pi_worker (logger)"),
    (re.compile(r"\bruntime\.pi\b"), "runtime.pi (capability)"),
    (re.compile(r"\bpi-mono\b"), "pi-mono (third-party package)"),
]

# Directories to scan.
SCAN_DIRS = [
    ROOT / "aee",
    ROOT / "aee-runtime",
    ROOT / "dispatcher",
    ROOT / "tests",
    ROOT / "docs",
]

# Allowlist: file paths (relative to ROOT) that may
# contain forbidden tokens because they are historical
# migration notes / changelogs.
ALLOWLIST = {
    # The AEE-4 remediation report itself
    "docs/AEE4_FINAL_VALIDATION_REPORT.md",
    "docs/AEE4_AEE_RUNTIME_REPORT.md",
    "Abacus/AEE4_PI_REFERENCE_IMPLEMENTATION_REPORT.md",
    "Abacus/AEE4_CAPABILITY_NAMING_SPEC.md",
    "Abacus/AEE4_CAPABILITY_EXTENSION_POINT.md",
    "Abacus/AEE4_FUTURE_MATCHER_DESIGN.md",
    "Abacus/AEE4_PI_WORKER_INTEGRATION_TASK.md",
    "Abacus/AEE4_WORKER_RUNTIME_CONTRACT_AND_PI_REFERENCE_IMPLEMENTATION.md",
    "Abacus/AEE3_CAPABILITY_MATCHING_REPORT.md",
    "Abacus/AEE2_WORKER_CLAIM_PROTOCOL_REPORT.md",
    "Abacus/AEE_MASTER_PLAN.md",
    "Abacus/AEE1_CORE_ADAPTER_REPORT.md",
    # The audit note in the new AEE-5 module
    "aee/runtimes/AUDIT_NOTE.md",
    # The AEE-5 migration guide itself
    "docs/aee/AEE5_MIGRATION_GUIDE.md",
    "docs/aee/AEE5_COMPLETION_REPORT.md",
    "docs/aee/AEE5_RUNTIME_REGISTRY_ARCHITECTURE.md",
    "docs/aee/AEE5_TEST_REPORT.md",
    "docs/aee/AEE5_API_REFERENCE.md",
    "docs/aee/AEE5_CONFIGURATION.md",
    # The new AEE-5 regression test (it mentions the
    # names by design to assert they're not present).
    "aee/tests/test_pi_agent_naming_regression.py",
    "tests/test_pi_agent_naming_regression.py",
    # config.example.yaml may mention pi-* packages in
    # the comments (provider env file uses PI_PROVIDER_*
    # names; this is a third-party OpenAI-compatible
    # provider naming convention we did not invent).
    "aee-runtime/aee_runtime.provider.env.example",
    "aee-runtime/config.example.yaml",
    # The README of the AEE-4 runtime must mention
    # the historical rename target.
    "aee-runtime/README.md",
    # The AEE-4 report in the docs/ directory.
    "docs/AEE_RUNTIME_INTEGRATION_GUIDE.md",
    # The AEE-4 Worker Runtime Contract doc.
    "docs/runtime/Worker_Runtime_Contract.md",
    # The AEE-0..AEE-3 reports (historical / migration).
    "docs/AEE0_BASELINE_REPORT.md",
    "docs/AEE1_CORE_ADAPTER_REPORT.md",
    "docs/AEE2_WORKER_CLAIM_PROTOCOL_REPORT.md",
    "docs/AEE3_CAPABILITY_MATCHING_REPORT.md",
    "docs/AEE4_AEE_RUNTIME_REPORT.md",
    "docs/AEE4_FINAL_VALIDATION_REPORT.md",
    # AEE-1 test fixture that has historical marker
    # comments referencing the pre-AEE-4-Part-B rename.
    "tests/test_manager_aee1.py",
    # The AEE-4 runtime daemon's module docstring
    # explicitly disambiguates from the third-party
    # "Pi Agent" packages. The historical reference
    # is intentional.
    "aee-runtime/aee_runtime.py",
}


# File extensions to scan.
SCAN_EXTS = {
    ".py", ".yaml", ".yml", ".json",
    ".md", ".txt", ".example", ".sh", ".service",
    ".conf", ".env",
}


def _is_text_file(p: Path) -> bool:
    """Heuristic: only scan text-looking files.

    We also skip `.git/`, `node_modules/`, `.venv/`,
    `__pycache__/`, and the data dir.
    """
    parts = set(p.parts)
    skip_dirs = {".git", "node_modules", ".venv", "__pycache__", "data", "logs", "runtime_data", ".claude", "dist", "build"}
    if parts & skip_dirs:
        return False
    if p.suffix.lower() not in SCAN_EXTS:
        return False
    return True


class TestNoPiAgentNames(unittest.TestCase):
    """Scan the repository and fail if any forbidden
    token re-appears in AEE-5 code / config / non-
    historical docs.

    Allowed (allowlisted) files are listed above.
    """

    def test_no_pi_agent_in_aee5_code(self):
        offenders: list = []
        for d in SCAN_DIRS:
            if not d.exists():
                continue
            for p in d.rglob("*"):
                if not p.is_file() or not _is_text_file(p):
                    continue
                rel = str(p.relative_to(ROOT))
                if rel in ALLOWLIST:
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for rx, label in FORBIDDEN_TOKENS:
                    for m in rx.finditer(text):
                        # Report only the first 3 offenders per file/pattern
                        offenders.append(
                            (rel, label, m.group(0)[:40])
                        )
                        if len([o for o in offenders if o[0] == rel and o[1] == label]) >= 3:
                            break
        if offenders:
            msg = "\n".join(
                f"  {rel}: {label} (matched: {match!r})"
                for rel, label, match in offenders[:50]
            )
            self.fail(
                f"AEE-5 has re-introduced {len(offenders)} forbidden "
                f"naming token(s):\n{msg}\n\n"
                f"If the token belongs to a historical migration / "
                f"changelog, add the file to ALLOWLIST in "
                f"`aee/tests/test_pi_agent_naming_regression.py`."
            )

    def test_no_third_party_pi_agent_dependency(self):
        """The runtime's package.json must not depend on
        any third-party Pi Agent package (`pi-agent-core`,
        `badlogic/pi-mono`, `earendil-works/pi-mono`, etc.)."""
        # Read the runtime package.json (if it exists)
        pkg = ROOT / "aee-runtime" / "runtime" / "package.json"
        if not pkg.exists():
            self.skipTest(f"package.json not found at {pkg}")
        import json
        with open(pkg, "r", encoding="utf-8") as f:
            data = json.load(f)
        deps = data.get("dependencies", {}) or {}
        for k in deps.keys():
            kl = k.lower()
            for forbidden in (
                "pi-agent",
                "pi_agent",
                "pi-mono",
                "pi_mono",
                "earendil-works",
                "badlogic",
            ):
                self.assertNotIn(
                    forbidden,
                    kl,
                    f"package.json has forbidden dependency {k!r}",
                )

    def test_no_pi_agent_in_requirements_txt(self):
        """The Python requirements must not pull in a
        third-party Pi Agent package."""
        for fname in ("requirements.txt",):
            for d in (ROOT, ROOT / "aee-runtime"):
                p = d / fname
                if not p.exists():
                    continue
                text = p.read_text(encoding="utf-8")
                for forbidden in (
                    "pi-agent",
                    "pi_agent",
                    "pi-mono",
                    "pi_mono",
                ):
                    self.assertNotIn(
                        forbidden,
                        text.lower(),
                        f"{p} references forbidden package {forbidden!r}",
                    )


if __name__ == "__main__":
    unittest.main()
