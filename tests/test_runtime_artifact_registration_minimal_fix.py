"""WO-RUNTIME-ARTIFACT-REGISTRATION-MINIMAL-FIX: regression test.

Reproduces the original bug (Hermes executor task produced and
verified a durable artifact on disk, but the final result mapping
returned ``artifact_paths=[]``, ``artifact_verification=[]``,
``artifact_count=0``) and verifies the minimal fix in
``_collect_task_evidence`` (output_text absolute-path scan via
``verify_artifacts``).

Scope:
  * Only exercises ``app._collect_task_evidence`` and
    ``app._merge_task_evidence_into_envelope`` — the exact
    artifact registration / result mapping code path named in the
    work-order. Does NOT touch the dispatcher, executor, queue,
    lifecycle, or any business logic.
  * Uses a temporary on-disk artifact file so the ``exists=True``
    branch of ``verify_artifacts`` is exercised end-to-end.
  * Also covers the negative case (path named in output_text but
    NOT present on disk → must NOT be registered) to pin the
    "durable artifact on disk" contract.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def _stub_task_outputs(monkeypatch, db_path: str, task_id: str,
                       output_text: str, delivery_json=None,
                       notification_json=None, artifact_rows=None):
    """Install a stub ``dispatcher.db.get_conn`` + seed the task_outputs
    and artifacts rows the helper reads. Mirrors the shape
    ``test_wo_fix_telegram_result_sync`` uses but is self-contained
    so this test does not depend on the global dispatcher DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_outputs ("
        "  task_id TEXT PRIMARY KEY,"
        "  output_text TEXT,"
        "  usage_json TEXT,"
        "  raw_json TEXT,"
        "  delivery_json TEXT,"
        "  notification_json TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifacts ("
        "  artifact_id TEXT PRIMARY KEY,"
        "  task_id TEXT,"
        "  path TEXT,"
        "  sha256 TEXT,"
        "  size INTEGER,"
        "  mtime TEXT,"
        "  file_exists INTEGER,"
        "  kind TEXT,"
        "  collected_at TEXT"
        ")"
    )
    conn.execute(
        "INSERT OR REPLACE INTO task_outputs "
        "(task_id, output_text, delivery_json, notification_json) "
        "VALUES (?, ?, ?, ?)",
        (task_id, output_text,
         json.dumps(delivery_json) if delivery_json is not None else None,
         json.dumps(notification_json) if notification_json is not None else None),
    )
    for row in (artifact_rows or []):
        conn.execute(
            "INSERT OR REPLACE INTO artifacts "
            "(artifact_id, task_id, path, sha256, size, mtime, file_exists, kind, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("artifact_id", f"art-{row['path']}"),
                task_id,
                row["path"],
                row.get("sha256"),
                row.get("size"),
                row.get("mtime"),
                1 if row.get("file_exists", True) else 0,
                row.get("kind", "file"),
                row.get("collected_at", "2026-08-06T00:00:00Z"),
            ),
        )
    conn.commit()

    import dispatcher.db as dbmod
    monkeypatch.setattr(dbmod, "get_conn", lambda: conn)
    # ``app._collect_task_evidence`` imports ``get_conn`` lazily
    # inside the function body, so patching the module attribute is
    # sufficient — no need to patch the symbol already imported
    # anywhere else.


def test_output_text_artifact_registered_when_file_exists(monkeypatch, tmp_path):
    """Reproduces the original bug scenario and verifies the fix.

    A Hermes-executor task has:
      * empty ``expected_artifacts`` (caller did not declare any)
      * NULL ``delivery_json`` (``_verify_expected_delivery`` had
        nothing to scan in ``input_text``)
      * empty ``artifacts`` table
      * ``output_text`` naming a real durable artifact on disk

    Pre-fix: ``_collect_task_evidence`` returned
    ``artifact_paths=[]`` / ``artifact_verification=[]``.
    Post-fix: the output_text scan registers the on-disk artifact.
    """
    # Create a real artifact file on disk so verify_artifacts'
    # exists=True branch fires.
    artifact_file = tmp_path / "report.md"
    artifact_file.write_text("# Durable report\nLine 2\nLine 3\n")

    db_path = str(tmp_path / "dispatcher.db")
    task_id = "TASK-FIX-0001"
    output_text = (
        "The report is complete.\n"
        f"**Artifact:** `{artifact_file}` (3 lines, durable)\n"
        "SHA256 verified on disk."
    )
    _stub_task_outputs(monkeypatch, db_path, task_id, output_text)

    from app import _collect_task_evidence, _merge_task_evidence_into_envelope

    evidence = _collect_task_evidence(task_id)
    # Core assertion: the on-disk artifact named in output_text is
    # now registered as task evidence.
    assert evidence is not None, (
        "_collect_task_evidence returned None — output_text scan did "
        "not fire or found no candidates"
    )
    assert str(artifact_file) in evidence["artifact_paths"], (
        f"artifact_paths={evidence['artifact_paths']!r} missing "
        f"{str(artifact_file)!r}"
    )
    assert any(
        v["path"] == str(artifact_file) and v["exists"] is True
        for v in evidence["artifact_verification"]
    ), (
        f"artifact_verification missing exists=True entry for "
        f"{str(artifact_file)!r}; got {evidence['artifact_verification']!r}"
    )

    # End-to-end through the merge helper: an evidence-empty
    # executor_runs stub (the Hermes lifecycle-sync shape observed
    # in run_8555085966d04a77b69255b6846b9ffb) must surface the
    # artifact in the final mapped envelope.
    envelope = {
        "run_id": "run_test_fix_0001",
        "task_id": task_id,
        "selected_executor": "hermes",
        "status": "completed",
        "stdout_summary": "",
        "artifact_paths": [],
        "artifact_verification": [],
        "git_evidence": None,
        "telegram_result": {},
    }
    merged = _merge_task_evidence_into_envelope(envelope)
    assert str(artifact_file) in merged["artifact_paths"], (
        f"merged.artifact_paths={merged['artifact_paths']!r}"
    )
    assert any(
        v["path"] == str(artifact_file) for v in merged["artifact_verification"]
    ), f"merged.artifact_verification missing entry: {merged['artifact_verification']!r}"
    assert merged["source"] == "executor_runs+tasks_merge"


def test_output_text_path_not_on_disk_not_registered(monkeypatch, tmp_path):
    """Negative case: a path named in output_text but NOT present on
    disk must NOT be registered as an artifact (the "durable artifact
    on disk" contract). Guards against false positives from prose
    like ``/tmp/never_written.md`` that the agent mentioned but did
    not actually produce."""
    db_path = str(tmp_path / "dispatcher.db")
    task_id = "TASK-FIX-0002"
    nonexistent = str(tmp_path / "missing.md")
    output_text = (
        "I considered writing to "
        f"`{nonexistent}` but the file does not exist."
    )
    _stub_task_outputs(monkeypatch, db_path, task_id, output_text)

    from app import _collect_task_evidence

    evidence = _collect_task_evidence(task_id)
    # output_text is non-empty so evidence is non-None, but the
    # named path does not exist on disk → must not be registered.
    if evidence is not None:
        assert nonexistent not in evidence["artifact_paths"], (
            f"non-existent path leaked into artifact_paths: "
            f"{evidence['artifact_paths']!r}"
        )
        assert all(
            v["path"] != nonexistent for v in evidence["artifact_verification"]
        ), (
            f"non-existent path leaked into artifact_verification: "
            f"{evidence['artifact_verification']!r}"
        )


def test_artifact_table_paths_preserved_alongside_output_scan(monkeypatch, tmp_path):
    """Coexistence: when the ``artifacts`` table already has rows
    (the explicit-declaration path) AND output_text names additional
    on-disk paths, both sources contribute — no duplication, no
    clobbering. This pins that the minimal fix is purely additive."""
    # Pre-existing artifact-table entry
    table_file = tmp_path / "from_table.md"
    table_file.write_text("from artifacts table\n")

    # Additional on-disk path only named in output_text
    out_file = tmp_path / "from_output.md"
    out_file.write_text("from output_text scan\n")

    db_path = str(tmp_path / "dispatcher.db")
    task_id = "TASK-FIX-0003"
    output_text = f"Wrote {out_file} via the output path."
    _stub_task_outputs(
        monkeypatch, db_path, task_id, output_text,
        artifact_rows=[{
            "path": str(table_file),
            "sha256": "abc",
            "size": 20,
            "mtime": "100",
            "file_exists": True,
        }],
    )

    from app import _collect_task_evidence

    evidence = _collect_task_evidence(task_id)
    assert evidence is not None
    assert str(table_file) in evidence["artifact_paths"], (
        f"artifacts-table path lost: {evidence['artifact_paths']!r}"
    )
    assert str(out_file) in evidence["artifact_paths"], (
        f"output_text-scan path missing: {evidence['artifact_paths']!r}"
    )
    # No duplicates
    assert len(evidence["artifact_paths"]) == len(set(evidence["artifact_paths"])), (
        f"duplicate paths: {evidence['artifact_paths']!r}"
    )
    # Both have exists=True verification entries
    verified_paths = {v["path"] for v in evidence["artifact_verification"]
                      if v.get("exists") is True}
    assert str(table_file) in verified_paths
    assert str(out_file) in verified_paths


def test_get_run_envelope_surfaces_artifact_count_positive(monkeypatch, tmp_path):
    """Acceptance criterion: at least one controlled fixture proves
    ``artifact_count > 0``, ``artifact_paths`` non-empty, and
    ``artifact_verification`` non-empty with correct content.

    Drives the full GET /runs/{run_id} read path with a stub Hermes
    adapter that reports a terminal completed run, then seeds a
    task_outputs row naming an on-disk artifact. Asserts the final
    response envelope registers the artifact.

    Mirrors the thread-safety pattern from
    ``test_wo_fix_telegram_result_sync.py::test_get_run_returns_confirmed_telegram_after_reconcile``
    — the FastAPI test client runs the app in a separate thread, so
    the dispatcher's task-create path hits a cross-thread sqlite3
    error and ``task_id`` ends up None on the executor_runs mapping
    row. Rather than assert ``task_id is not None`` (which the
    reference test does NOT do — it uses ``if task_id:``), we seed
    BOTH the executor_runs row and the task_outputs row directly via
    ``get_conn()`` in the test thread, which is the same connection
    the GET handler will use for the merge lookup. This isolates the
    artifact-registration fix from the unrelated thread-safety
    limitation in the Hermes executor submit path.
    """
    from tests._executor_test_helpers import make_client

    artifact_file = tmp_path / "durable_report.md"
    artifact_file.write_text("# Durable\nLine 2\n")

    client, app_module, key = make_client(monkeypatch, tmp_path)

    from aee.adapters.base import RuntimePollResult
    from aee.core.registry import adapter_registry

    class _StubHermesAdapter:
        async def submit(self, job):
            from aee.adapters.base import RuntimeSubmitResult
            return RuntimeSubmitResult(
                external_run_id="run_stub_fix_acceptance",
                status="queued",
            )

        async def poll(self, external_run_id):
            return RuntimePollResult(
                external_run_id=external_run_id,
                status="completed",
                output=f"done; artifact at {artifact_file}",
                error=None,
                is_terminal=True,
                raw={"status": "completed"},
            )

    original = adapter_registry._adapters.get("hermes")
    adapter_registry._adapters["hermes"] = _StubHermesAdapter()
    run_id = "run_stub_fix_acceptance"
    task_id = "TASK-FIX-ACCEPTANCE-0001"
    try:
        # Seed the executor_runs + task_outputs rows directly in the
        # test thread via the same get_conn() the GET handler uses.
        # This bypasses the cross-thread submit-path limitation
        # (documented above) and isolates the artifact-registration
        # fix under test.
        from dispatcher.db import get_conn
        conn = get_conn()
        # Insert the tasks row FIRST so the task_outputs FK is
        # satisfied. Use the minimal column set the schema requires
        # (task_id is PRIMARY KEY; other NOT NULL columns have
        # schema defaults or are nullable).
        conn.execute(
            "INSERT OR REPLACE INTO tasks "
            "(task_id, title, type, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, "acceptance fixture", "review",
             "completed", "2026-08-06T00:00:00Z"),
        )
        # Ensure the executor_runs row exists with the Hermes
        # lifecycle-sync stub shape (empty evidence + task_id link),
        # matching the observed run_8555085966d04a77b69255b6846b9ffb
        # shape that the fix targets.
        from dispatcher.executor_runs import upsert_run
        upsert_run(
            conn,
            run_id=run_id,
            requested_executor=None,
            selected_executor="hermes",
            task_id=task_id,
            status="completed",
            progress=1.0,
            routing={"selected_executor": "hermes",
                     "selection_source": "lifecycle_sync"},
            stdout_summary="",
            artifact_paths=[],
            artifact_verification=[],
            telegram_result={},
            current_step="completed",
            phase="terminal",
        )
        # Seed the task_outputs row with output_text naming the
        # on-disk artifact — the implicit-output path the fix adds.
        conn.execute(
            "INSERT OR REPLACE INTO task_outputs "
            "(task_id, output_text, notification_json) VALUES (?, ?, ?)",
            (task_id,
             f"Report complete. Artifact: `{artifact_file}` (2 lines).",
             json.dumps({"sent": False})),
        )
        conn.commit()

        get_resp = client.get(
            f"/runs/{run_id}",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert get_resp.status_code == 200, (
            f"GET /runs/{run_id} returned {get_resp.status_code}: "
            f"{get_resp.text[:500]}"
        )
        env = get_resp.json()
        # Acceptance criteria from the work-order:
        assert env.get("artifact_paths"), (
            f"artifact_paths empty: {env!r}"
        )
        assert str(artifact_file) in env["artifact_paths"], (
            f"artifact_paths={env['artifact_paths']!r}"
        )
        # The summary endpoint surfaces artifact_count explicitly.
        summary = client.get(
            f"/runs/{run_id}/summary",
            headers={"Authorization": f"Bearer {key}"},
        ).json()
        assert summary.get("artifact_count", 0) > 0, (
            f"artifact_count not > 0: {summary!r}"
        )
        assert summary.get("artifact_paths"), (
            f"summary.artifact_paths empty: {summary!r}"
        )
    finally:
        if original is not None:
            adapter_registry._adapters["hermes"] = original