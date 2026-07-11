"""AEE-7.2 — ArtifactService contract tests.

This test file is the AEE-7.2 finalization contract: it pins
the public shape of the read-only operational surface so future
changes to ``aee.operations.artifacts`` cannot silently break
the service contract (DTO field names, ordering rules,
filter semantics, error handling).

What is tested
--------------
* ``list_by_task`` — normal population, empty task_id, no
  rows, ``path`` / ``kind`` filters, sort order
  (newest-first by ``collected_at``), limit cap.
* ``get`` — normal lookup, missing ``artifact_id`` returns
  ``None``, blank input raises ``ValueError``.
* ``latest`` — returns the highest version row for a
  ``(task_id, path)`` pair, missing returns ``None``.
* ``list_policy_events`` — returns all events for a task
  newest-first; ``accepted`` filter limits to one side; limit
  cap holds.
* **multi-version** — two ``save()`` calls for the same
  ``(task_id, path)`` produce two distinct rows and
  ``latest()`` returns v2.
* **invalid input** — blank ``task_id`` / ``artifact_id`` /
  ``path`` raise ``ValueError``; ``limit=0`` and
  ``limit=1001`` raise ``ValueError``; non-positive limits
  rejected at the wire.
* **security boundary** — the DTO never carries
  ``file content`` (we never call ``os.read``); only
  metadata fields. The DTO does not include the file
  body even when the on-disk file contains "secret" text.
* **no-FastAPI** — the service is constructible with no
  HTTP framework installed; we exercise the in-memory
  repository only.
* **Observability contract** — module loggers emit
  structured WARNING / DEBUG records; the WARNING line
  never echoes token / env / secret strings.

This is **deliberately hermetic**: no SQLite, no FastAPI,
no I/O beyond ``tmp_path`` for the in-memory repo (which
only touches the in-process list).
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aee.artifacts.models import (
    ARTIFACT_KIND_ARTIFACT,
    ARTIFACT_KIND_LOG,
    ARTIFACT_KIND_REPORT,
    Artifact,
)
from aee.artifacts.policy import (
    ArtifactPolicy,
    PolicyDecision,
    PolicyViolationCode,
)
from aee.artifacts.repository import (
    ArtifactRepository,
    InMemoryArtifactRepository,
)
from aee.operations.artifacts import (
    ArtifactPolicyEvent,
    ArtifactService,
    ArtifactSummary,
    policy_event_to_dto,
    summarize_artifact,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk_artifact(
    task_id: str,
    path: str,
    *,
    kind: str = ARTIFACT_KIND_REPORT,
    body: bytes = b"hello",
    base_dir: Optional[Path] = None,
    collected_at: Optional[str] = None,
) -> Artifact:
    """Build a real on-disk file and an Artifact pointing at it."""
    if base_dir is None:
        base_dir = Path(tempfile.mkdtemp(prefix="aee72-svc-"))
    full = base_dir / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(body)
    return Artifact(
        path=str(full),
        task_id=task_id,
        kind=kind,
        sha256=None,  # the repo does not compute this; the
                      # service must not require it
        size=len(body),
        mtime=_iso(datetime.fromtimestamp(full.stat().st_mtime)),
        exists=True,
        content_type="text/plain",
        classification_source="auto",
        collected_at=collected_at or _iso(datetime.now(timezone.utc)),
    )


# ---------------------------------------------------------------------------
# summarize_artifact / policy_event_to_dto — pure helpers
# ---------------------------------------------------------------------------


class TestSummarizeArtifact(unittest.TestCase):
    """The pure helper that turns a repository Artifact into the
    read-only DTO. Pin the field set + nullable behavior so a
    future refactor can't silently drop a field.
    """

    def test_all_fields_propagate(self) -> None:
        art = _mk_artifact("t1", "report.md")
        summary = summarize_artifact(art, "t1")
        self.assertIsInstance(summary, ArtifactSummary)
        for field_name in (
            "artifact_id", "task_id", "path", "kind", "version",
            "exists", "size", "mtime", "sha256", "collected_at",
        ):
            self.assertTrue(
                hasattr(summary, field_name),
                f"ArtifactSummary missing field {field_name!r}",
            )

    def test_artifact_id_empty_string_fallback(self) -> None:
        # In-memory repo sets artifact_id post-save; the
        # raw Artifact dataclass leaves it None. The DTO
        # contract is "" (empty string) so the JSON
        # response shape is stable.
        art = _mk_artifact("t1", "report.md")
        # Bypass repo.save so artifact_id is None.
        self.assertIsNone(art.artifact_id)
        summary = summarize_artifact(art, "t1")
        self.assertEqual(summary.artifact_id, "")

    def test_size_and_exists_propagate(self) -> None:
        art = _mk_artifact("t1", "report.md", body=b"x" * 17)
        summary = summarize_artifact(art, "t1")
        self.assertEqual(summary.size, 17)
        self.assertTrue(summary.exists)

    def test_missing_file_size_is_none(self) -> None:
        # ``Artifact`` for a missing path has size=None, exists=False.
        art = Artifact(
            path="/tmp/does-not-exist.md",
            task_id="t1",
            kind=ARTIFACT_KIND_REPORT,
            exists=False,
            size=None,
            mtime=None,
            sha256=None,
        )
        summary = summarize_artifact(art, "t1")
        self.assertFalse(summary.exists)
        self.assertIsNone(summary.size)
        self.assertIsNone(summary.mtime)
        self.assertIsNone(summary.sha256)


class TestPolicyEventToDto(unittest.TestCase):
    """Pin the policy-event DTO shape."""

    def test_happy_path(self) -> None:
        ev = {
            "decision_id": "dec-1",
            "code": "ok",
            "accepted": 1,
            "realpath": "/tmp/x.md",
            "original": "/tmp/x.md",
            "detail": "in-bounds",
            "source": "ArtifactPipeline.collect",
            "artifact_id": "art-1",
            "ts": "2026-07-10T10:00:00Z",
        }
        d = policy_event_to_dto(ev, "task-1")
        self.assertIsInstance(d, ArtifactPolicyEvent)
        self.assertEqual(d.decision_id, "dec-1")
        self.assertEqual(d.task_id, "task-1")
        self.assertEqual(d.code, "ok")
        self.assertTrue(d.accepted)
        self.assertEqual(d.artifact_id, "art-1")
        self.assertEqual(d.recorded_at, "2026-07-10T10:00:00Z")

    def test_artifact_id_none_preserved(self) -> None:
        ev = {
            "decision_id": "dec-2",
            "code": "outside_allowed_roots",
            "accepted": 0,
            "realpath": "/etc/passwd",
            "original": "/etc/passwd",
            "detail": "outside repo roots",
            "source": "ArtifactPipeline.collect",
            "artifact_id": None,
            "recorded_at": "2026-07-10T10:00:00Z",
        }
        d = policy_event_to_dto(ev, "task-2")
        self.assertIsNone(d.artifact_id)
        self.assertFalse(d.accepted)

    def test_missing_fields_default_to_empty_string(self) -> None:
        d = policy_event_to_dto({}, "task-x")
        # We never want a NoneType to leak into the DTO;
        # operators depend on stable string types.
        self.assertEqual(d.decision_id, "")
        self.assertEqual(d.code, "")
        self.assertEqual(d.realpath, "")
        self.assertEqual(d.original, "")
        self.assertEqual(d.detail, "")
        self.assertEqual(d.source, "")
        self.assertEqual(d.recorded_at, "")


# ---------------------------------------------------------------------------
# ArtifactService — the read-only service
# ---------------------------------------------------------------------------


class TestArtifactServiceListByTask(unittest.TestCase):
    """``list_by_task``: normal / empty / filters / sort / limit."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aee72-svc-list-"))
        self.repo = InMemoryArtifactRepository()
        self.svc = ArtifactService(self.repo)
        # Three artifacts across two paths for one task.
        self.repo.save(
            _mk_artifact(
                "t1", "report.md", kind=ARTIFACT_KIND_REPORT,
                collected_at="2026-07-10T10:00:00Z",
                base_dir=self.tmp,
            )
        )
        self.repo.save(
            _mk_artifact(
                "t1", "test.log", kind=ARTIFACT_KIND_LOG,
                collected_at="2026-07-10T11:00:00Z",
                base_dir=self.tmp,
            )
        )
        # Two versions of report.md (the in-memory repo
        # bumps version on every save for the same key).
        self.repo.save(
            _mk_artifact(
                "t1", "report.md", kind=ARTIFACT_KIND_REPORT,
                collected_at="2026-07-10T12:00:00Z",
                base_dir=self.tmp,
            )
        )

    def test_returns_all_for_task(self) -> None:
        out = self.svc.list_by_task("t1")
        # 3 rows: report.md v1, test.log v1, report.md v2.
        self.assertEqual(len(out), 3)

    def test_no_rows_for_unknown_task(self) -> None:
        self.assertEqual(self.svc.list_by_task("nope"), [])

    def test_empty_task_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.list_by_task("")

    def test_path_filter(self) -> None:
        # Pull the absolute path of the v1 report.md row.
        path = str(self.tmp / "report.md")
        out = self.svc.list_by_task("t1", path=path)
        self.assertEqual(len(out), 2)
        for s in out:
            self.assertEqual(s.path, path)

    def test_kind_filter(self) -> None:
        out = self.svc.list_by_task("t1", kind=ARTIFACT_KIND_LOG)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, ARTIFACT_KIND_LOG)

    def test_newest_first_ordering(self) -> None:
        out = self.svc.list_by_task("t1")
        # Three collected_at values: 10:00, 11:00, 12:00.
        # Newest first means the first element is 12:00.
        self.assertEqual(out[0].collected_at, "2026-07-10T12:00:00Z")
        self.assertEqual(out[-1].collected_at, "2026-07-10T10:00:00Z")

    def test_limit_caps_results(self) -> None:
        out = self.svc.list_by_task("t1", limit=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].collected_at, "2026-07-10T12:00:00Z")

    def test_invalid_limit_raises(self) -> None:
        for bad in (0, -1, 1001, 5000):
            with self.assertRaises(ValueError, msg=f"limit={bad}"):
                self.svc.list_by_task("t1", limit=bad)


class TestArtifactServiceGet(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aee72-svc-get-"))
        self.repo = InMemoryArtifactRepository()
        self.svc = ArtifactService(self.repo)
        self.saved = self.repo.save(_mk_artifact("t1", "report.md",
                                                  base_dir=self.tmp))

    def test_get_existing(self) -> None:
        out = self.svc.get(self.saved.artifact_id)
        if out is None:
            self.fail("get() returned None for saved artifact")
        self.assertEqual(out.artifact_id, self.saved.artifact_id)
        self.assertEqual(out.path, str(self.tmp / "report.md"))
        # task_id is propagated by the in-memory impl.
        self.assertEqual(out.task_id, "t1")

    def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(self.svc.get("does-not-exist"))

    def test_get_blank_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.get("")


class TestArtifactServiceLatest(unittest.TestCase):
    """``latest`` returns the highest version row for a (task, path) pair."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aee72-svc-latest-"))
        self.repo = InMemoryArtifactRepository()
        self.svc = ArtifactService(self.repo)
        self.repo.save(_mk_artifact(
            "t1", "report.md", collected_at="2026-07-10T10:00:00Z",
            base_dir=self.tmp,
        ))
        self.repo.save(_mk_artifact(
            "t1", "report.md", collected_at="2026-07-10T11:00:00Z",
            base_dir=self.tmp,
        ))

    def test_returns_highest_version(self) -> None:
        path = str(self.tmp / "report.md")
        out = self.svc.latest("t1", path)
        if out is None:
            self.fail("latest() returned None for v2 artifact")
        # version 2 wins.
        self.assertEqual(out.version, 2)
        self.assertEqual(out.collected_at, "2026-07-10T11:00:00Z")

    def test_missing_returns_none(self) -> None:
        self.assertIsNone(self.svc.latest("t1", "/nope/missing.md"))

    def test_empty_inputs_raise(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.latest("", "/x")
        with self.assertRaises(ValueError):
            self.svc.latest("t1", "")


class TestArtifactServiceListPolicyEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = InMemoryArtifactRepository()
        self.svc = ArtifactService(self.repo)
        # Two accepted, one rejected — distinct decision_ids.
        self.repo.record_policy_event({
            "decision_id": "d1", "task_id": "t1",
            "code": "ok", "accepted": 1,
            "realpath": "/tmp/r.md", "original": "/tmp/r.md",
            "detail": "in-bounds", "source": "ArtifactPipeline.collect",
            "artifact_id": "a1", "recorded_at": "2026-07-10T10:00:00Z",
        })
        self.repo.record_policy_event({
            "decision_id": "d2", "task_id": "t1",
            "code": "ok", "accepted": 1,
            "realpath": "/tmp/l.log", "original": "/tmp/l.log",
            "detail": "in-bounds", "source": "ArtifactPipeline.collect",
            "artifact_id": "a2", "recorded_at": "2026-07-10T11:00:00Z",
        })
        self.repo.record_policy_event({
            "decision_id": "d3", "task_id": "t1",
            "code": "outside_allowed_roots", "accepted": 0,
            "realpath": "/etc/passwd", "original": "/etc/passwd",
            "detail": "outside", "source": "ArtifactPipeline.collect",
            "artifact_id": None, "recorded_at": "2026-07-10T12:00:00Z",
        })

    def test_returns_all_for_task(self) -> None:
        out = self.svc.list_policy_events("t1")
        self.assertEqual(len(out), 3)

    def test_accepted_filter_true(self) -> None:
        out = self.svc.list_policy_events("t1", accepted=True)
        self.assertEqual(len(out), 2)
        for ev in out:
            self.assertTrue(ev.accepted)

    def test_accepted_filter_false(self) -> None:
        out = self.svc.list_policy_events("t1", accepted=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].code, "outside_allowed_roots")

    def test_no_rows_for_unknown_task(self) -> None:
        self.assertEqual(self.svc.list_policy_events("nope"), [])

    def test_newest_first(self) -> None:
        out = self.svc.list_policy_events("t1")
        # The repository's list_policy_events sorts by
        # recorded_at desc, so the first event should be
        # the 12:00 (d3) rejection.
        self.assertEqual(out[0].decision_id, "d3")
        self.assertEqual(out[-1].decision_id, "d1")

    def test_invalid_limit_raises(self) -> None:
        for bad in (0, -5, 5000):
            with self.assertRaises(ValueError, msg=f"limit={bad}"):
                self.svc.list_policy_events("t1", limit=bad)

    def test_empty_task_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.list_policy_events("")


# ---------------------------------------------------------------------------
# Multi-version handling
# ---------------------------------------------------------------------------


class TestMultiVersionArtifacts(unittest.TestCase):
    """The in-memory repo bumps version on every save(). The
    service must surface ALL versions via ``list_by_task`` and
    the highest via ``latest()``.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aee72-svc-multi-"))
        self.repo = InMemoryArtifactRepository()
        self.svc = ArtifactService(self.repo)
        # 3 versions of the same path.
        for ts in (
            "2026-07-10T10:00:00Z",
            "2026-07-10T11:00:00Z",
            "2026-07-10T12:00:00Z",
        ):
            self.repo.save(_mk_artifact(
                "t1", "report.md", collected_at=ts,
                base_dir=self.tmp,
            ))

    def test_list_shows_three_versions(self) -> None:
        out = self.svc.list_by_task("t1")
        self.assertEqual(len(out), 3)
        versions = sorted(s.version for s in out)
        self.assertEqual(versions, [1, 2, 3])

    def test_latest_returns_v3(self) -> None:
        path = str(self.tmp / "report.md")
        latest = self.svc.latest("t1", path)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.version, 3)
        self.assertEqual(latest.collected_at, "2026-07-10T12:00:00Z")


# ---------------------------------------------------------------------------
# Security boundary — content never leaves the service
# ---------------------------------------------------------------------------


class TestSecurityBoundary(unittest.TestCase):
    """The DTO must not carry the file body, even if the
    on-disk file contains "secret" text. The service is
    read-only by construction.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aee72-svc-sec-"))
        self.repo = InMemoryArtifactRepository()
        self.svc = ArtifactService(self.repo)
        # On-disk file with sensitive content.
        secret = b"SECRET_TOKEN=sk-abc123 SUPER_SECRET_KEY\n"
        self.repo.save(_mk_artifact(
            "t1", "secret.txt", body=secret, kind=ARTIFACT_KIND_ARTIFACT,
            base_dir=self.tmp,
        ))

    def test_dto_does_not_carry_content(self) -> None:
        out = self.svc.list_by_task("t1")
        self.assertEqual(len(out), 1)
        s = out[0]
        # None of the DTO fields should hold the bytes.
        for field_name in (
            "artifact_id", "task_id", "path", "kind", "version",
            "exists", "size", "mtime", "sha256", "collected_at",
        ):
            value = getattr(s, field_name)
            if isinstance(value, str):
                self.assertNotIn(
                    "SECRET_TOKEN", value,
                    f"{field_name} leaked secret content: {value!r}",
                )
                self.assertNotIn(
                    "sk-abc123", value,
                    f"{field_name} leaked secret content: {value!r}",
                )
        # Size is the byte count, not the bytes.
        self.assertEqual(s.size, len(b"SECRET_TOKEN=sk-abc123 SUPER_SECRET_KEY\n"))

    def test_get_returns_metadata_only(self) -> None:
        saved_id = self.svc.list_by_task("t1")[0].artifact_id
        s = self.svc.get(saved_id)
        self.assertIsNotNone(s)
        for field_name, value in s.__dict__.items():
            if isinstance(value, str):
                self.assertNotIn("SECRET", value)
                self.assertNotIn("sk-abc123", value)

    def test_no_container_fields_in_dto(self) -> None:
        """Pin that the service is not pulling in the
        process environment. The DTO fields are all
        scalars or None; we assert no ``dict`` /
        ``list`` / arbitrary env values leak.
        """
        out = self.svc.list_by_task("t1")
        s = out[0]
        for field_name, value in s.__dict__.items():
            self.assertNotIsInstance(
                value, (dict, list, tuple),
                f"{field_name} unexpectedly contains a container",
            )

    def test_service_is_read_only(self) -> None:
        """No ``save`` / ``delete`` / ``update`` method on
        ArtifactService — the surface is read-only by
        construction.
        """
        for forbidden in ("save", "delete", "update", "write",
                          "create", "remove", "patch"):
            self.assertFalse(
                hasattr(self.svc, forbidden),
                f"ArtifactService must not expose {forbidden!r}",
            )


# ---------------------------------------------------------------------------
# Protocol conformance — both impls must satisfy the Protocol
# ---------------------------------------------------------------------------


class TestRepositoryProtocol(unittest.TestCase):
    """The :class:`ArtifactRepository` Protocol is the
    contract both the in-memory and the SQLite impls must
    satisfy. The AEE-7.2 finalization adds
    ``list_policy_events`` and ``update_policy_event_artifact_id``;
    both must be present on the in-memory impl so the
    service can call them.
    """

    def test_in_memory_satisfies_protocol(self) -> None:
        repo = InMemoryArtifactRepository()
        self.assertIsInstance(repo, ArtifactRepository)

    def test_in_memory_has_aee72_methods(self) -> None:
        repo = InMemoryArtifactRepository()
        for method in (
            "list_policy_events",
            "update_policy_event_artifact_id",
        ):
            self.assertTrue(
                callable(getattr(repo, method, None)),
                f"InMemoryArtifactRepository missing {method!r}",
            )

    def test_in_memory_list_policy_events_filters(self) -> None:
        repo = InMemoryArtifactRepository()
        repo.record_policy_event({
            "decision_id": "d1", "task_id": "t1",
            "code": "ok", "accepted": 1,
            "realpath": "/x", "original": "/x",
            "detail": "ok", "source": "ArtifactPipeline.collect",
            "artifact_id": None, "recorded_at": "2026-07-10T10:00:00Z",
        })
        repo.record_policy_event({
            "decision_id": "d2", "task_id": "t2",
            "code": "ok", "accepted": 1,
            "realpath": "/y", "original": "/y",
            "detail": "ok", "source": "ArtifactPipeline.collect",
            "artifact_id": None, "recorded_at": "2026-07-10T11:00:00Z",
        })
        # task_id filter
        self.assertEqual(len(repo.list_policy_events(task_id="t1")), 1)
        # accepted filter
        self.assertEqual(
            len(repo.list_policy_events(accepted=True)), 2
        )
        # code filter
        self.assertEqual(
            len(repo.list_policy_events(code="ok")), 2
        )
        # limit
        self.assertEqual(len(repo.list_policy_events(limit=1)), 1)


# ---------------------------------------------------------------------------
# Observability — module loggers emit structured fields
# ---------------------------------------------------------------------------


class _Capture(logging.Handler):
    """Capture log records for assertion. We never let
    the captured log lines touch stdout, so the test
    output stays clean.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _RejectAll(ArtifactPolicy):
    """A test-only policy that always rejects."""

    def check(self, path: str) -> PolicyDecision:  # type: ignore[override]
        return PolicyDecision(
            code=PolicyViolationCode.OUTSIDE_ROOTS,
            realpath=path,
            original=path,
            detail="forced reject for test",
        )


class TestObservabilityLogging(unittest.TestCase):
    """Pin that the AEE-7.2 observability layer emits
    structured WARNING / INFO records on the right
    module loggers, and NEVER logs token / env / secret
    strings.
    """

    def setUp(self) -> None:
        # Capture from both relevant loggers.
        self._capture_artifacts = _Capture()
        self._capture_collect = _Capture()
        self._capture_dispatcher = _Capture()
        art_log = logging.getLogger("aee.operations.artifacts")
        col_log = logging.getLogger("aee.artifacts.collect")
        dis_log = logging.getLogger("dispatcher.manager")
        for log, handler in (
            (art_log, self._capture_artifacts),
            (col_log, self._capture_collect),
            (dis_log, self._capture_dispatcher),
        ):
            log.addHandler(handler)
            log.setLevel(logging.DEBUG)
        self._art_log = art_log
        self._col_log = col_log
        self._dis_log = dis_log

    def tearDown(self) -> None:
        for log, handler in (
            (self._art_log, self._capture_artifacts),
            (self._col_log, self._capture_collect),
            (self._dis_log, self._capture_dispatcher),
        ):
            log.removeHandler(handler)

    def test_service_emits_debug_on_list_by_task(self) -> None:
        svc = ArtifactService(InMemoryArtifactRepository())
        svc.list_by_task("t1")
        # At least one DEBUG record on aee.operations.artifacts.
        recs = [r for r in self._capture_artifacts.records
                if r.levelno == logging.DEBUG]
        self.assertGreaterEqual(len(recs), 1)
        # The first record's message contains the task_id.
        self.assertIn("t1", recs[0].getMessage())

    def test_service_emits_debug_on_get_missing(self) -> None:
        svc = ArtifactService(InMemoryArtifactRepository())
        svc.get("missing-id")
        recs = [r for r in self._capture_artifacts.records
                if r.levelno == logging.DEBUG]
        self.assertGreaterEqual(len(recs), 1)
        self.assertIn("missing-id", recs[0].getMessage())

    def test_collect_emits_warning_on_policy_violation(self) -> None:
        """The collector must surface policy violations
        as WARNING with the decision_id, code, and
        realpath.
        """
        from aee.artifacts.collect import ArtifactPipeline
        repo = InMemoryArtifactRepository()
        pipe = ArtifactPipeline(
            repo=repo, policy=_RejectAll(allowed_roots=("/nope",)),
            on_policy_violation="skip_and_warn",
        )
        pipe.collect("t-x", ["/etc/passwd"])
        recs = [r for r in self._capture_collect.records
                if r.levelno == logging.WARNING]
        self.assertGreaterEqual(len(recs), 1)
        msg = recs[0].getMessage()
        # Structured fields: task_id, decision_id, code, realpath, mode.
        for needle in ("task_id=t-x",
                       "code=outside_allowed_roots",
                       "realpath=/etc/passwd",
                       "mode=skip_and_warn"):
            self.assertIn(
                needle, msg,
                f"WARNING log missing structured field {needle!r}: {msg!r}",
            )

    def test_collect_does_not_log_token_or_env(self) -> None:
        """Hardening: the WARNING line must not echo a
        bearer token or env var.
        """
        from aee.artifacts.collect import ArtifactPipeline
        repo = InMemoryArtifactRepository()
        pipe = ArtifactPipeline(
            repo=repo, policy=_RejectAll(allowed_roots=("/nope",)),
            on_policy_violation="skip_and_warn",
        )
        pipe.collect(
            "t-x",
            ["/tmp/looks-like-a-token-sk-abc123.txt"],
        )
        for rec in self._capture_collect.records:
            msg = rec.getMessage()
            self.assertNotIn("env=", msg)
            self.assertNotIn("OPENAI_API_KEY", msg)
            self.assertNotIn("HERMES_API_KEY", msg)


# ---------------------------------------------------------------------------
# Smoke: the service is constructible without FastAPI / HTTP libs
# ---------------------------------------------------------------------------


class TestServiceIsHttpFree(unittest.TestCase):
    """The service must not require FastAPI / Starlette /
    any HTTP framework. We assert by attempting to
    construct and call it with no environment vars set
    and no HTTP libs.
    """

    def test_construct_and_call(self) -> None:
        svc = ArtifactService(InMemoryArtifactRepository())
        # Exercise the full public surface.
        self.assertEqual(svc.list_by_task("nope"), [])
        self.assertIsNone(svc.get("nope"))
        self.assertIsNone(svc.latest("nope", "nope"))
        self.assertEqual(svc.list_policy_events("nope"), [])


if __name__ == "__main__":
    unittest.main()
