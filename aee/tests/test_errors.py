"""AEE-5 errors — unit tests.

Verify the structured error contract from
the AEE-5 task spec §4.5.
"""
from __future__ import annotations

import unittest

from aee.runtimes.errors import (
    RuntimeNotFoundError,
    RuntimeRegistryError,
    RuntimeValidationError,
)


class TestRuntimeNotFoundError(unittest.TestCase):
    def test_code_is_canonical(self):
        exc = RuntimeNotFoundError()
        self.assertEqual(exc.code, "AEE_RUNTIME_NOT_FOUND")
        self.assertIsInstance(exc, RuntimeRegistryError)

    def test_to_dict_includes_structured_details(self):
        exc = RuntimeNotFoundError(
            message="no match",
            task_id="t1",
            run_id="r1",
            required_runtime_type="aee_lightweight",
            required_capabilities=["task.shell"],
            required_labels={"env": "local"},
            evaluated_runtimes=[
                {"runtime_id": "rt1", "rejected_reasons": ["missing cap: task.shell"]},
            ],
        )
        d = exc.to_dict()
        self.assertEqual(d["code"], "AEE_RUNTIME_NOT_FOUND")
        self.assertEqual(d["message"], "no match")
        self.assertEqual(d["details"]["task_id"], "t1")
        self.assertEqual(d["details"]["run_id"], "r1")
        self.assertEqual(d["details"]["required_capabilities"], ["task.shell"])
        self.assertEqual(d["details"]["required_labels"], {"env": "local"})
        self.assertEqual(len(d["details"]["evaluated_runtimes"]), 1)
        self.assertEqual(
            d["details"]["evaluated_runtimes"][0]["runtime_id"], "rt1"
        )

    def test_default_message(self):
        exc = RuntimeNotFoundError()
        self.assertIn("No enabled runtime", exc.message)


class TestRuntimeValidationError(unittest.TestCase):
    def test_code(self):
        exc = RuntimeValidationError("bad")
        self.assertEqual(exc.code, "AEE_RUNTIME_VALIDATION_ERROR")
        self.assertIsInstance(exc, RuntimeRegistryError)


if __name__ == "__main__":
    unittest.main()
