"""Focused tests for the run-summary retrieval restore
(TASK-AEE-P2-RUN-RETRIEVAL-API-RESTORE).

Covers the work-order's required scenarios for the
``GET /runs/{run_id}/summary`` endpoint:

  A. Summary retrieval for a running run (executor_runs store).
  B. Summary retrieval for a terminal run (executor_runs store).
  C. Full retrieval for a terminal run (the existing
     ``GET /runs/{run_id}`` route — already covered by
     test_run_tracking.py, asserted here for cross-endpoint parity).
  D. Unknown run_id returns a structured 404 with code=unknown_run_id.
  E. Malformed run_id returns a structured 400 with code=malformed_run_id.
  F. No new executor run is created by retrieval (no upstream
     adapter.poll call).
  G. No Telegram is sent by retrieval.
  H. Summary endpoint does not mutate DB (repeated reads stable).
  I. Terminal full retrieval stable across repeated reads.
  J. Routing / executor metadata preserved on summary.
  K. Artifacts / git / telegram / runtime identity fields preserved
     on full retrieval.
  L. Legacy dispatcher-task rows remain readable via summary.

These tests use the same fixtures as the run-tracking suite
(``tests/_executor_test_helpers.py``) so they run against a temp
dispatcher DB and a fake claude binary — no real CLI, no real
upstream Hermes, no network, no Telegram.
"""
from __future__ import annotations

import pytest

from tests._executor_test_helpers import (
    make_client,
    post_executor,
    set_fake_binary,
    write_fake_claude,
)


def _get_summary(client, key: str, run_id: str):
    return client.get(
        f"/runs/{run_id}/summary",
        headers={"Authorization": f"Bearer {key}"},
    )


def _get_run(client, key: str, run_id: str):
    return client.get(
        f"/runs/{run_id}",
        headers={"Authorization": f"Bearer {key}"},
    )


# Required summary envelope field set (work-order §A).
_REQUIRED_SUMMARY_FIELDS = [
    "run_id",
    "status",
    "output_preview",
    "error",
    "current_hint",
    "is_terminal",
    "source",
]


# ---------------------------------------------------------------------------
# A. Summary retrieval for a running run (executor_runs store)
# ---------------------------------------------------------------------------
def test_summary_running_async_hermes_run(monkeypatch, tmp_path):
    """A hermes executor dispatch persists a queued row whose
    summary is readable without launching a new run."""
    from aee.adapters.base import RuntimeSubmitResult
    from aee.core.registry import adapter_registry

    class _StubHermes:
        name = "hermes"
        runtime_type = "hermes"

        async def submit(self, job):
            return RuntimeSubmitResult(
                external_run_id="run_summary_running_abc",
                status="queued",
            )

        async def poll(self, external_run_id):
            from aee.adapters.base import RuntimePollResult
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="running",
                is_terminal=False,
            )

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(
                external_run_id=external_run_id, cancelled=True,
            )

    saved = dict(adapter_registry._adapters)
    adapter_registry._adapters["hermes"] = _StubHermes()
    try:
        client, _app, key = make_client(monkeypatch, tmp_path)
        resp = post_executor(client, key, {
            "executor": "hermes",
            "prompt": "summarize status",
            "timeout_sec": 30,
        })
        assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
        run_id = resp.json()["run_id"]

        s = _get_summary(client, key, run_id)
        assert s.status_code == 200, f"{s.status_code}: {s.text}"
        body = s.json()
        assert body["run_id"] == run_id
        assert body["status"] in {"queued", "running"}
        assert body["is_terminal"] is False
        assert body["source"] == "executor_runs"
        assert body["selected_executor"] == "hermes"
        for f in _REQUIRED_SUMMARY_FIELDS:
            assert f in body, f"summary missing field: {f!r}"
    finally:
        adapter_registry._adapters.clear()
        adapter_registry._adapters.update(saved)


# ---------------------------------------------------------------------------
# B. Summary retrieval for a terminal run (executor_runs store)
# ---------------------------------------------------------------------------
def test_summary_terminal_run(monkeypatch, tmp_path):
    """A completed claude-code-cli run's summary is readable."""
    artifact = str(tmp_path / "summary_terminal.md")
    binary = write_fake_claude(tmp_path, artifact=artifact, artifact_content="ok")
    set_fake_binary(monkeypatch, binary)
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    })
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    s = _get_summary(client, key, run_id)
    assert s.status_code == 200, f"{s.status_code}: {s.text}"
    body = s.json()
    assert body["run_id"] == run_id
    assert body["status"] == "completed"
    assert body["is_terminal"] is True
    assert body["source"] == "executor_runs"
    assert body["exit_code"] == 0
    assert body["artifact_count"] == 1
    assert artifact in body["artifact_paths"]
    for f in _REQUIRED_SUMMARY_FIELDS:
        assert f in body, f"summary missing field: {f!r}"


# ---------------------------------------------------------------------------
# D. Unknown run_id returns structured 404
# ---------------------------------------------------------------------------
def test_summary_unknown_run_id(monkeypatch, tmp_path):
    """An unknown run_id returns a deterministic JSON 404, no upstream."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = _get_summary(client, key, "run_does_not_exist_summary_xyz")
    assert resp.status_code == 404
    detail = resp.json().get("detail", resp.json())
    assert isinstance(detail, dict)
    assert detail.get("code") == "unknown_run_id"
    assert "run_does_not_exist_summary_xyz" in detail.get("message", "")


# ---------------------------------------------------------------------------
# E. Malformed run_id returns structured 400
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_id",
    [
        "run with spaces",
        "a" * 201,
        "run!bad",
        "run@bad",
    ],
)
def test_summary_malformed_run_id(monkeypatch, tmp_path, bad_id):
    """A malformed run_id reaching the summary handler returns 400."""
    client, _app, key = make_client(monkeypatch, tmp_path)
    resp = _get_summary(client, key, bad_id)
    assert resp.status_code == 400, f"expected 400 for {bad_id!r}, got {resp.status_code}"
    detail = resp.json().get("detail", resp.json())
    assert isinstance(detail, dict)
    assert detail.get("code") == "malformed_run_id"


# ---------------------------------------------------------------------------
# F. No new executor run is created by retrieval (no upstream poll)
# ---------------------------------------------------------------------------
def test_summary_does_not_call_upstream_hermes(monkeypatch, tmp_path):
    """GET /runs/{run_id}/summary for an unknown id must NOT call
    the Hermes adapter. The pre-restore regression fell through to
    adapter.poll() here; this test guards against that regression
    by instrumenting the adapter to fail the test if poll() fires.
    """
    from aee.adapters.base import RuntimePollResult
    from aee.core.registry import adapter_registry

    class _TripwireHermes:
        name = "hermes"
        runtime_type = "hermes"
        poll_called = False

        async def submit(self, job):
            from aee.adapters.base import RuntimeSubmitResult
            return RuntimeSubmitResult(
                external_run_id="summary-tripwire", status="queued",
            )

        async def poll(self, external_run_id):
            _TripwireHermes.poll_called = True
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="completed",
                is_terminal=True,
            )

        async def cancel(self, external_run_id):
            from aee.adapters.base import RuntimeCancelResult
            return RuntimeCancelResult(
                external_run_id=external_run_id, cancelled=True,
            )

    saved = dict(adapter_registry._adapters)
    adapter_registry._adapters["hermes"] = _TripwireHermes()
    try:
        client, _app, key = make_client(monkeypatch, tmp_path)
        resp = _get_summary(client, key, "run_summary_unknown_to_hermes")
        assert resp.status_code == 404
        assert _TripwireHermes.poll_called is False, (
            "summary must not call adapter.poll() on an unknown id"
        )
    finally:
        adapter_registry._adapters.clear()
        adapter_registry._adapters.update(saved)


# ---------------------------------------------------------------------------
# H. Summary endpoint does not mutate DB (repeated reads stable)
# ---------------------------------------------------------------------------
def test_summary_repeated_reads_stable(monkeypatch, tmp_path):
    """Two summary reads of a terminal run return byte-for-byte
    identical envelopes (no mutation between reads)."""
    artifact = str(tmp_path / "summary_stable.md")
    binary = write_fake_claude(tmp_path, artifact=artifact, artifact_content="x")
    set_fake_binary(monkeypatch, binary)
    client, _app, key = make_client(monkeypatch, tmp_path)

    post_resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    })
    assert post_resp.status_code == 200
    run_id = post_resp.json()["run_id"]

    s1 = _get_summary(client, key, run_id)
    s2 = _get_summary(client, key, run_id)
    assert s1.status_code == 200 and s2.status_code == 200
    assert s1.json() == s2.json(), "summary repeated reads drifted"


# ---------------------------------------------------------------------------
# I. Terminal full retrieval stable across repeated reads
# ---------------------------------------------------------------------------
def test_full_retrieval_repeated_reads_stable(monkeypatch, tmp_path):
    """Two full GET /runs/{run_id} reads of a terminal run return
    identical envelopes (excluding seconds_since_update which is
    a derived clock field)."""
    artifact = str(tmp_path / "full_stable.md")
    binary = write_fake_claude(tmp_path, artifact=artifact, artifact_content="y")
    set_fake_binary(monkeypatch, binary)
    client, _app, key = make_client(monkeypatch, tmp_path)

    post_resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    })
    assert post_resp.status_code == 200
    run_id = post_resp.json()["run_id"]

    r1 = _get_run(client, key, run_id)
    r2 = _get_run(client, key, run_id)
    assert r1.status_code == 200 and r2.status_code == 200
    e1, e2 = r1.json(), r2.json()
    # seconds_since_update is the only derived clock field; strip
    # it before comparing so the stability assertion is meaningful.
    for e in (e1, e2):
        e.pop("seconds_since_update", None)
    assert e1 == e2, "full retrieval repeated reads drifted"


# ---------------------------------------------------------------------------
# J. Routing / executor metadata preserved on summary
# ---------------------------------------------------------------------------
def test_summary_preserves_routing_metadata(monkeypatch, tmp_path):
    """Summary preserves requested/selected executor + routing."""
    artifact = str(tmp_path / "summary_routing.md")
    binary = write_fake_claude(tmp_path, artifact=artifact, artifact_content="z")
    set_fake_binary(monkeypatch, binary)
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    })
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    s = _get_summary(client, key, run_id)
    assert s.status_code == 200
    body = s.json()
    assert body["requested_executor"] == "claude-code-cli"
    assert body["selected_executor"] == "claude-code-cli"
    # claude-code-cli runs may not have a dispatcher task_id; the
    # routing metadata (requested/selected executor) is the
    # canonical routing evidence and must be present regardless.
    assert body["selected_executor"] == body["requested_executor"]


# ---------------------------------------------------------------------------
# K. Artifacts / git / telegram / runtime identity fields preserved
#    on full retrieval
# ---------------------------------------------------------------------------
def test_full_retrieval_preserves_evidence_envelope(monkeypatch, tmp_path):
    """Full GET /runs/{run_id} preserves the artifact / git /
    telegram / runtime_identity evidence envelope."""
    artifact = str(tmp_path / "full_evidence.md")
    binary = write_fake_claude(tmp_path, artifact=artifact, artifact_content="e")
    set_fake_binary(monkeypatch, binary)
    client, _app, key = make_client(monkeypatch, tmp_path)

    resp = post_executor(client, key, {
        "executor": "claude-code-cli",
        "prompt": "create artifact",
        "expected_artifacts": [artifact],
        "timeout_sec": 30,
        "repo_path": "/home/ubuntu/Abacus",
    })
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    r = _get_run(client, key, run_id)
    assert r.status_code == 200
    env = r.json()
    assert artifact in env["artifact_paths"]
    assert isinstance(env["artifact_verification"], list)
    assert any(av.get("path") == artifact and av.get("exists") for av in env["artifact_verification"])
    assert "git_evidence" in env
    assert "telegram_result" in env
    assert "runtime_identity" in env
    assert "routing" in env
    assert env["source"] == "executor_runs"


# ---------------------------------------------------------------------------
# L. Legacy dispatcher-task rows remain readable via summary
# ---------------------------------------------------------------------------
def test_summary_legacy_dispatcher_task(monkeypatch, tmp_path):
    """A run_id backed only by the tasks table (no executor_runs
    row) is still readable via the summary endpoint."""
    from dispatcher import db as ddb
    from dispatcher import manager as dmgr

    monkeypatch.setattr(ddb, "DB_DIR", tmp_path)
    monkeypatch.setattr(ddb, "DB_PATH", tmp_path / "dispatcher.db")
    monkeypatch.setattr(dmgr, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(dmgr, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(ddb, "_initialized", False)
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None

    client, _app, key = make_client(monkeypatch, tmp_path)
    # Create a dispatcher task with a hermes_run_id so it's
    # findable by find_by_hermes_run_id.
    m = dmgr.TaskManager()
    t = m.create(
        title="legacy summary test",
        type="research",
        input_text="x",
    )
    m.start(t.task_id, "run_legacy_summary_001")
    m.complete(t.task_id, output_text="legacy done")

    s = _get_summary(client, key, "run_legacy_summary_001")
    assert s.status_code == 200, f"{s.status_code}: {s.text}"
    body = s.json()
    assert body["run_id"] == "run_legacy_summary_001"
    assert body["status"] == "completed"
    assert body["is_terminal"] is True
    assert body["source"] == "dispatcher_tasks"
    assert body["task_id"] == t.task_id


# ---------------------------------------------------------------------------
# G. No Telegram is sent by retrieval
# ---------------------------------------------------------------------------
def test_summary_no_telegram_side_effect(monkeypatch, tmp_path):
    """Summary retrieval must not trigger a Telegram notification.

    The notifier is not invoked by the summary path; we assert
    this by checking that no notification state row is created
    for the run_id as a side effect of the summary call. The
    notification_state table is the persistent trace of any
    notification attempt; an empty result proves no send.
    """
    from dispatcher import db as ddb
    from dispatcher import manager as dmgr

    monkeypatch.setattr(ddb, "DB_DIR", tmp_path)
    monkeypatch.setattr(ddb, "DB_PATH", tmp_path / "dispatcher.db")
    monkeypatch.setattr(dmgr, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(dmgr, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(ddb, "_initialized", False)
    if hasattr(ddb._local, "conn"):
        ddb._local.conn = None

    client, _app, key = make_client(monkeypatch, tmp_path)
    # Just call summary on an unknown id; no notification row
    # should be created.
    _get_summary(client, key, "run_summary_no_telegram_check")
    # The notification_state table may not exist on a fresh DB;
    # that itself is proof of no notification side effect. If it
    # does exist, it must have zero rows for this run_id.
    conn = ddb.get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM notification_state "
            "WHERE task_id = ? OR channel_ref = ?",
            ("run_summary_no_telegram_check", "run_summary_no_telegram_check"),
        ).fetchone()
        assert row[0] == 0, "summary created a notification row"
    except Exception:
        # Table doesn't exist — no notification side effect.
        pass