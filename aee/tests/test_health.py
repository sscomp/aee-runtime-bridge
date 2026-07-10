"""AEE-5 Runtime health model — unit tests."""
from __future__ import annotations

import unittest

from aee.runtimes.health import (
    health_score,
    is_dispatchable,
    is_health_status,
)
from aee.runtimes.models import RuntimeHealthStatus


class TestIsHealthStatus(unittest.TestCase):
    def test_canonical_values(self):
        for s in RuntimeHealthStatus.ALL:
            self.assertTrue(is_health_status(s), f"expected {s} to be canonical")

    def test_invalid_values(self):
        for s in ("bogus", "", None, 1, "HEALTHY"):
            self.assertFalse(is_health_status(s))  # type: ignore[arg-type]


class TestIsDispatchable(unittest.TestCase):
    def test_healthy_is_dispatchable(self):
        self.assertTrue(is_dispatchable(RuntimeHealthStatus.HEALTHY))

    def test_degraded_is_dispatchable(self):
        self.assertTrue(is_dispatchable(RuntimeHealthStatus.DEGRADED))

    def test_unknown_default_dispatchable(self):
        # AEE-4 compat: allow_unknown_health=True by default.
        self.assertTrue(is_dispatchable(RuntimeHealthStatus.UNKNOWN))

    def test_unknown_strict_mode_not_dispatchable(self):
        self.assertFalse(
            is_dispatchable(
                RuntimeHealthStatus.UNKNOWN, allow_unknown_health=False
            )
        )

    def test_unhealthy_not_dispatchable(self):
        self.assertFalse(is_dispatchable(RuntimeHealthStatus.UNHEALTHY))

    def test_offline_not_dispatchable(self):
        self.assertFalse(is_dispatchable(RuntimeHealthStatus.OFFLINE))

    def test_invalid_status_not_dispatchable(self):
        self.assertFalse(is_dispatchable("BOGUS"))


class TestHealthScore(unittest.TestCase):
    def test_healthy_lowest(self):
        self.assertLess(
            health_score(RuntimeHealthStatus.HEALTHY),
            health_score(RuntimeHealthStatus.DEGRADED),
        )

    def test_degraded_lower_than_unknown(self):
        self.assertLess(
            health_score(RuntimeHealthStatus.DEGRADED),
            health_score(RuntimeHealthStatus.UNKNOWN),
        )

    def test_offline_highest(self):
        self.assertGreater(
            health_score(RuntimeHealthStatus.OFFLINE),
            health_score(RuntimeHealthStatus.UNHEALTHY),
        )

    def test_unknown_score(self):
        self.assertEqual(health_score(RuntimeHealthStatus.UNKNOWN), 2)


if __name__ == "__main__":
    unittest.main()
