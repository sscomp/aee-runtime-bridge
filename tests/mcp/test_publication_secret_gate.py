"""Keep probe markers synthetic without widening the source secret gate."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts/scan-aee-baseline-secrets.py"
spec = importlib.util.spec_from_file_location("publication_secret_gate", SCANNER)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class PublicationSecretGateTests(unittest.TestCase):
    def test_probe_marker_is_explicitly_synthetic(self):
        path = "scripts/probe-adapter-budget.py"
        data = (ROOT / path).read_bytes()
        self.assertIn(b"synthetic-probe-key", data)
        self.assertEqual(scanner.findings(path, data, [])[0], [])

    def test_real_looking_credential_blocked_in_probe(self):
        data = b'API_KEY="' + b"q9Z7" * 8 + b'"'
        findings, exceptions = scanner.findings("scripts/probe-adapter-budget.py", data, [])
        self.assertEqual(exceptions, 0)
        self.assertEqual([f["kind"] for f in findings], ["literal_credential_assignment"])

    def test_nonfixture_source_blocked(self):
        data = b'API_KEY="' + b"q9Z7" * 8 + b'"'
        self.assertTrue(scanner.findings("aee/runtime.py", data, [])[0])

    def test_provider_prefix_not_exempt_even_with_synthetic_label(self):
        token = b"sk" + b"-" + b"synthetic" * 4
        self.assertTrue(scanner.findings("scripts/probe-adapter-budget.py", token, [])[0])

    def test_known_secret_comparison_overrides_placeholder(self):
        marker = b"synthetic-probe-key"
        self.assertEqual(scanner.findings("scripts/probe-adapter-budget.py", marker, [marker])[0][0]["kind"],
                         "known_local_secret")

    def test_cli_clean_and_injected_violation_exit_codes(self):
        with tempfile.TemporaryDirectory(prefix="aee-scan-") as name:
            repo = Path(name)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            source = repo / "source.py"
            for value, expected in [("synthetic-probe-key", 0), ("q9Z7" * 8, 1)]:
                source.write_text('API_KEY="' + value + '"\n')
                subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True)
                result = subprocess.run([sys.executable, str(SCANNER), "--repo", str(repo)],
                                        capture_output=True, check=False)
                self.assertEqual(result.returncode, expected)
                report = json.loads(result.stdout)
                self.assertEqual(report["result"], "PASS" if expected == 0 else "FAIL")
                if expected:
                    self.assertEqual(report["findings"][0]["path"], "source.py")
                    self.assertNotIn(value, result.stdout.decode())
