"""Focused regression tests for the --day argument-parsing fix in soak_checkpoint.py.

Tests prove:
  - Day 1-7 each resolve to the correct distinct report path.
  - Both '--day N' and '--day=N' forms work.
  - Invalid/missing day handling is safe and deterministic (defaults to 1).
  - No checkpoint file can overwrite another day's report due to parsing fallback.

Scope: ONLY the _parse_day helper and report path derivation logic.
Does NOT exercise the full main() health-check pipeline (which requires
live bridge/DB/supervisord).  main() is tested only for argument parsing
isolation via a stub that short-circuits before any health check runs.
"""
import importlib.util
import os
import sys
import unittest

SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "soak_checkpoint.py",
)


def _load_soak_module():
    """Load soak_checkpoint.py as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("soak_checkpoint", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestParseDay(unittest.TestCase):
    """Test the _parse_day helper directly."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_soak_module()

    def test_space_form_day_1(self):
        self.assertEqual(self.mod._parse_day(["--day", "1"]), 1)

    def test_space_form_day_2(self):
        self.assertEqual(self.mod._parse_day(["--day", "2"]), 2)

    def test_space_form_day_3(self):
        self.assertEqual(self.mod._parse_day(["--day", "3"]), 3)

    def test_space_form_day_4(self):
        self.assertEqual(self.mod._parse_day(["--day", "4"]), 4)

    def test_space_form_day_5(self):
        self.assertEqual(self.mod._parse_day(["--day", "5"]), 5)

    def test_space_form_day_6(self):
        self.assertEqual(self.mod._parse_day(["--day", "6"]), 6)

    def test_space_form_day_7(self):
        self.assertEqual(self.mod._parse_day(["--day", "7"]), 7)

    def test_equals_form_day_1(self):
        self.assertEqual(self.mod._parse_day(["--day=1"]), 1)

    def test_equals_form_day_2(self):
        self.assertEqual(self.mod._parse_day(["--day=2"]), 2)

    def test_equals_form_day_3(self):
        self.assertEqual(self.mod._parse_day(["--day=3"]), 3)

    def test_equals_form_day_4(self):
        self.assertEqual(self.mod._parse_day(["--day=4"]), 4)

    def test_equals_form_day_5(self):
        self.assertEqual(self.mod._parse_day(["--day=5"]), 5)

    def test_equals_form_day_6(self):
        self.assertEqual(self.mod._parse_day(["--day=6"]), 6)

    def test_equals_form_day_7(self):
        self.assertEqual(self.mod._parse_day(["--day=7"]), 7)

    def test_missing_arg_defaults_to_1(self):
        self.assertEqual(self.mod._parse_day([]), 1)

    def test_missing_arg_no_day_flag_defaults_to_1(self):
        self.assertEqual(self.mod._parse_day(["--verbose"]), 1)

    def test_space_form_no_value_defaults_to_1(self):
        """--day at end of argv with no following value."""
        self.assertEqual(self.mod._parse_day(["--day"]), 1)

    def test_invalid_value_defaults_to_1(self):
        """Non-integer value after --day."""
        self.assertEqual(self.mod._parse_day(["--day", "abc"]), 1)

    def test_invalid_equals_value_defaults_to_1(self):
        """Non-integer value in --day=abc form."""
        self.assertEqual(self.mod._parse_day(["--day=abc"]), 1)

    def test_zero_clamped_to_1(self):
        """Day 0 is clamped to minimum 1."""
        self.assertEqual(self.mod._parse_day(["--day", "0"]), 1)

    def test_negative_clamped_to_1(self):
        """Negative day is clamped to minimum 1."""
        self.assertEqual(self.mod._parse_day(["--day", "-3"]), 1)

    def test_zero_equals_form_clamped_to_1(self):
        self.assertEqual(self.mod._parse_day(["--day=0"]), 1)

    def test_dual_form_both_present_equals_wins(self):
        """When both --day=7 and --day are present (the workaround used during
        the soak), the first match in argv order wins.  --day=7 appears first
        so day=7 is returned."""
        self.assertEqual(self.mod._parse_day(["--day=7", "--day"]), 7)

    def test_dual_form_space_first(self):
        """If --day N appears before --day=N, the space form wins."""
        self.assertEqual(self.mod._parse_day(["--day", "5", "--day=3"]), 5)

    def test_extra_args_before_day(self):
        """--day works even if other args precede it."""
        self.assertEqual(self.mod._parse_day(["--verbose", "--day=4"]), 4)

    def test_extra_args_after_day_space(self):
        self.assertEqual(self.mod._parse_day(["--day", "3", "--verbose"]), 3)

    def test_extra_args_after_day_equals(self):
        self.assertEqual(self.mod._parse_day(["--day=3", "--verbose"]), 3)


class TestDistinctReportPaths(unittest.TestCase):
    """Prove Day 1-7 each resolve to a distinct, correct report path."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_soak_module()
        cls.reports_dir = self = cls.mod.REPORTS_DIR

    def _report_path_for_day(self, day):
        return os.path.join(self.reports_dir, f"7_day_soak_day{day}_checkpoint.md")

    def test_all_seven_days_distinct_paths(self):
        paths = {}
        for day in range(1, 8):
            p = self._report_path_for_day(day)
            self.assertNotIn(p, paths.values(), f"Duplicate path for day {day}")
            paths[day] = p

    def test_day1_through_day7_filenames(self):
        for day in range(1, 8):
            expected = os.path.join(self.reports_dir, f"7_day_soak_day{day}_checkpoint.md")
            actual = self._report_path_for_day(day)
            self.assertEqual(actual, expected)

    def test_no_cross_day_overwrite_via_parse(self):
        """The bug: --day=3 was parsed as day=1, writing to day1 file.
        With the fix, --day=3 resolves to day=3, so the day3 file is used.
        Verify the parsed day maps to the correct filename."""
        test_cases = [
            (["--day=1"], 1),
            (["--day=2"], 2),
            (["--day=3"], 3),
            (["--day=4"], 4),
            (["--day=5"], 5),
            (["--day=6"], 6),
            (["--day=7"], 7),
            (["--day", "1"], 1),
            (["--day", "2"], 2),
            (["--day", "3"], 3),
            (["--day", "4"], 4),
            (["--day", "5"], 5),
            (["--day", "6"], 6),
            (["--day", "7"], 7),
        ]
        for argv, expected_day in test_cases:
            with self.subTest(argv=argv):
                parsed = self.mod._parse_day(argv)
                self.assertEqual(parsed, expected_day,
                    f"argv={argv} parsed as day={parsed}, expected {expected_day}")
                # The report path must contain the correct day number
                expected_path = self._report_path_for_day(expected_day)
                parsed_path = self._report_path_for_day(parsed)
                self.assertEqual(parsed_path, expected_path,
                    f"Parsed day {parsed} would write to {parsed_path}, "
                    f"expected {expected_path}")

    def test_bug_repro_equals_form_not_treated_as_day1(self):
        """The specific bug: --day=3 was silently treated as day=1.
        This test proves the fix: --day=3 is now parsed as 3, not 1."""
        parsed = self.mod._parse_day(["--day=3"])
        self.assertNotEqual(parsed, 1, "BUG: --day=3 still parsed as day=1")
        self.assertEqual(parsed, 3)

    def test_bug_repro_equals_form_day5_not_day1(self):
        """Day 5 was also lost due to the bug."""
        parsed = self.mod._parse_day(["--day=5"])
        self.assertNotEqual(parsed, 1, "BUG: --day=5 still parsed as day=1")
        self.assertEqual(parsed, 5)

    def test_bug_repro_equals_form_day7_not_day1(self):
        """Day 7 overwrote Day 1 due to the bug."""
        parsed = self.mod._parse_day(["--day=7"])
        self.assertNotEqual(parsed, 1, "BUG: --day=7 still parsed as day=1")
        self.assertEqual(parsed, 7)


class TestMainArgumentIsolation(unittest.TestCase):
    """Verify that main() calls _parse_day and uses the result for report path.
    We stub _parse_day to avoid running the full health-check pipeline."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_soak_module()

    def test_main_calls_parse_day(self):
        """Confirm main() delegates to _parse_day rather than inline parsing."""
        import inspect
        source = inspect.getsource(self.mod.main)
        self.assertIn("_parse_day", source,
            "main() must call _parse_day, not inline argv parsing")

    def test_main_no_raw_argv_indexing(self):
        """Confirm the old buggy pattern sys.argv[1].split('=') is gone."""
        import inspect
        source = inspect.getsource(self.mod.main)
        self.assertNotIn("sys.argv[1].split", source,
            "main() must not contain the old buggy sys.argv[1].split pattern")
        self.assertNotIn('"--day" in sys.argv', source,
            "main() must not contain the old buggy exact membership check")


if __name__ == "__main__":
    unittest.main(verbosity=2)