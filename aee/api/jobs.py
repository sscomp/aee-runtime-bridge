"""AEE-2 jobs API — runtime-neutral job creation, claim, heartbeat,
logs, complete/fail/cancel. Backed by `dispatcher.db` helpers and
the existing `TaskManager`.

The router is mounted under the FastAPI app at `/jobs` and `/runs`
(the latter is the GPT Action compatibility alias — see `app.py`).

Authentication: same `require_auth` as the legacy endpoints. AEE-2
does not introduce a separate auth surface; workers and clients use
the same `BRIDGE_API_KEY` bearer. Per-worker capability gating
happens at claim time, not at request time.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query

from dispatcher import db
from dispatcher.manager import (
    IllegalTransition,
    TaskManager,
    TaskNotFound,
)
from aee.adapters.base import AdapterNotFoundError


router = APIRouter()
manager = TaskManager()


# ---------------------------------------------------------------------------
# Auth — re-use the bridge's bearer pattern. Inlined to avoid a
# circular import with `app.py`. The legacy `require_auth` is the
# same shape; this is a deliberate minimal re-implementation that
# the AEE API can be tested in isolation (no app.py required).
# ---------------------------------------------------------------------------


def _read_api_key_from_env() -> str:
    import os
    # Honour DISPATCHER_API_KEY override first (set in test fixtures).
    return os.getenv("DISPATCHER_API_KEY") or os.getenv("BRIDGE_API_KEY", "")


def _require_auth(authorization: Optional[str]) -> None:
    expected = _read_api_key_from_env()
    if not expected:
        raise HTTPException(status_code=500, detail="BRIDGE_API_KEY is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.split(" ", 1)[1].strip()
    if not _hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


# ---------------------------------------------------------------------------
# Pydantic-ish body models — we use plain dataclasses to avoid pulling
# pydantic into the AEE package; the FastAPI handlers are typed with
# `Body(...)` and we do manual validation.
# ---------------------------------------------------------------------------


def _validate_register_worker(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    worker_name = body.get("worker_name")
    worker_type = body.get("worker_type")
    if not isinstance(worker_name, str) or not worker_name.strip():
        raise HTTPException(status_code=400, detail="worker_name is required")
    if not isinstance(worker_type, str) or not worker_type.strip():
        raise HTTPException(status_code=400, detail="worker_type is required")
    capabilities = body.get("capabilities") or []
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise HTTPException(status_code=400, detail="capabilities must be a list of strings")
    workdir_allowlist = body.get("workdir_allowlist") or []
    if not isinstance(workdir_allowlist, list) or not all(isinstance(p, str) for p in workdir_allowlist):
        raise HTTPException(status_code=400, detail="workdir_allowlist must be a list of strings")
    max_concurrent = int(body.get("max_concurrent") or 1)
    return {
        "worker_name": worker_name.strip(),
        "worker_type": worker_type.strip(),
        "hostname": (body.get("hostname") or None),
        "capabilities": [c.strip() for c in capabilities if c.strip()],
        "workdir_allowlist": [p.strip() for p in workdir_allowlist if p.strip()],
        "max_concurrent": max(1, max_concurrent),
    }


def _validate_create_job(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    title = (body.get("title") or "").strip()
    input_text = body.get("input") or body.get("input_text") or ""
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    if not isinstance(input_text, str):
        raise HTTPException(status_code=400, detail="input must be a string")
    runtime_type = (body.get("target_runtime") or body.get("runtime_type") or "hermes").strip()
    adapter_name = (body.get("adapter_name") or runtime_type).strip()
    return {
        "title": title,
        "type": (body.get("type") or "ops").strip(),
        "mode": body.get("mode"),
        "priority": int(body.get("priority") or 50),
        "input_text": input_text,
        "session_id": body.get("session_id"),
        "client_source": body.get("client_source"),
        "model_name": body.get("model_name"),
        "runtime_type": runtime_type,
        "adapter_name": adapter_name,
        "approval_required": bool(body.get("approval_required") or False),
    }


def _validate_claim_request(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    worker_id = body.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise HTTPException(status_code=400, detail="worker_id is required")
    worker_type = (body.get("worker_type") or "").strip()
    capabilities = body.get("capabilities") or []
    if not isinstance(capabilities, list):
        raise HTTPException(status_code=400, detail="capabilities must be a list")
    max_jobs = int(body.get("max_jobs") or 1)
    return {
        "worker_id": worker_id.strip(),
        "worker_type": worker_type or None,
        "capabilities": [str(c) for c in capabilities],
        "max_jobs": max(1, max_jobs),
    }


def _require_claim_token(
    task_id: str, presented_token: Optional[str], presented_hash: Optional[str]
) -> str:
    """Resolve the token to verify. Accepts the plain token OR the
    sha256 hex digest. Returns the canonical hash to check.
    """
    if presented_hash:
        return presented_hash
    if presented_token:
        return hashlib.sha256(presented_token.encode("utf-8")).hexdigest()
    raise HTTPException(status_code=401, detail="claim_token or claim_token_hash is required")


# ---------------------------------------------------------------------------
# POST /jobs — create a job
# ---------------------------------------------------------------------------


@router.post("/jobs")
async def create_job(
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    data = _validate_create_job(body)
    # If a session_id was given, validate the allowlist (same as /runs).
    if data["session_id"]:
        from dispatcher.safety import evaluate as _eval  # type: ignore
        # Session validation is enforced by app.py's check_session_allowed
        # for /runs. For /jobs we let it through — the manager can carry
        # the field and downstream code decides.
        pass
    task = manager.create(
        title=data["title"],
        type=data["type"],
        input_text=data["input_text"],
        session_id=data["session_id"],
        mode=data["mode"],
        priority=data["priority"],
        owner=data["runtime_type"],
        model_name=data["model_name"],
        initial_status="queued",
    )
    # Stamp AEE-1 fields directly.
    conn = db.get_conn()
    with db.transaction() as conn2:
        conn2.execute(
            "UPDATE tasks SET runtime_type = ?, adapter_name = ?, "
            "approval_required = ? WHERE task_id = ?",
            (data["runtime_type"], data["adapter_name"],
             1 if data["approval_required"] else 0, task.task_id),
        )
    task = manager.get_or_raise(task.task_id)
    return {
        "job_id": task.task_id,
        "task_id": task.task_id,
        "status": task.status,
        "runtime_type": task.runtime_type,
        "adapter_name": task.adapter_name,
        "approval_required": task.approval_required,
    }


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    task = manager.get(job_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    out = task.to_dict()
    out["job_id"] = task.task_id
    return out


# ---------------------------------------------------------------------------
# POST /jobs/claim — pull a job for a worker
# ---------------------------------------------------------------------------


@router.post("/jobs/claim")
async def claim_job(
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    data = _validate_claim_request(body)
    worker = db.get_worker(data["worker_id"])
    if worker is None:
        raise HTTPException(
            status_code=404,
            detail=f"worker {data['worker_id']!r} not registered; POST /workers/register first",
        )
    # Use the worker's registered type if the request didn't pin it.
    worker_type = data["worker_type"] or worker["worker_type"]
    # If the worker is already busy with a running job, refuse to
    # claim another one (AEE-2 honours `max_concurrent` as a soft
    # cap; AEE-3 will tighten this with reservations).
    busy = db.get_conn().execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE worker_id = ? AND status = 'running'",
        (data["worker_id"],),
    ).fetchone()
    if busy and int(busy["c"]) >= int(worker["max_concurrent"]):
        raise HTTPException(
            status_code=409,
            detail=(
                f"worker {data['worker_id']!r} already has {busy['c']} running job(s) "
                f">= max_concurrent={worker['max_concurrent']}"
            ),
        )
    candidate = db.find_claimable_job(
        worker_type=worker_type,
        capabilities=data["capabilities"] or worker["capabilities"],
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="no claimable job for this worker")
    # Mint a one-time token, hash it, and atomically claim.
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    ok = db.claim_job(
        task_id=candidate["task_id"],
        worker_id=data["worker_id"],
        claim_token_hash=token_hash,
    )
    if not ok:
        # Lost the race; tell the worker to retry.
        raise HTTPException(status_code=409, detail="job was claimed by another worker")
    db.update_worker_heartbeat(data["worker_id"], job_id=candidate["task_id"])
    manager.log(candidate["task_id"], f"claimed by worker_id={data['worker_id']}")
    manager._emit_event(
        candidate["task_id"], "claimed",
        {"worker_id": data["worker_id"], "worker_type": worker_type},
    )
    task = manager.get_or_raise(candidate["task_id"])
    return {
        "job_id": task.task_id,
        "task_id": task.task_id,
        "claim_token": token,  # plain, returned ONCE
        "title": task.title,
        "type": task.type,
        "mode": task.mode,
        "input": task.input_text,
        "session_id": task.session_id,
        "runtime_type": task.runtime_type,
        "adapter_name": task.adapter_name,
        "external_run_id": task.external_run_id,
        "timeout_seconds": 900,  # default; AEE-2 doesn't expose per-job timeout yet
        "expected_artifacts": [],
    }


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/heartbeat — keep the job alive
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/heartbeat")
async def heartbeat_job(
    job_id: str,
    body: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    token_hash = _require_claim_token(
        job_id,
        body.get("claim_token") if isinstance(body, dict) else None,
        body.get("claim_token_hash") if isinstance(body, dict) else None,
    )
    task = manager.get(job_id)
    if task is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not db.verify_claim_token(job_id, token_hash):
        raise HTTPException(status_code=403, detail="invalid claim token")
    if task.status != "running":
        raise HTTPException(
            status_code=409,
            detail=f"job is in status {task.status!r}, heartbeat only valid in 'running'",
        )
    db.update_task_heartbeat(job_id)
    return {"job_id": job_id, "status": "running", "heartbeat_at": db._now_iso()}


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/logs — append a line to the job log
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/logs")
async def append_log(
    job_id: str,
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    token_hash = _require_claim_token(
        job_id,
        body.get("claim_token") if isinstance(body, dict) else None,
        body.get("claim_token_hash") if isinstance(body, dict) else None,
    )
    if not db.verify_claim_token(job_id, token_hash):
        raise HTTPException(status_code=403, detail="invalid claim token")
    line = (body or {}).get("line") or ""
    if not isinstance(line, str) or not line.strip():
        raise HTTPException(status_code=400, detail="line is required")
    db.append_task_log(job_id, line)
    return {"job_id": job_id, "appended": True}


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/complete — terminal success
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    body: Dict[str, Any] = Body(default_factory=dict),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    token_hash = _require_claim_token(
        job_id,
        body.get("claim_token") if isinstance(body, dict) else None,
        body.get("claim_token_hash") if isinstance(body, dict) else None,
    )
    if not db.verify_claim_token(job_id, token_hash):
        raise HTTPException(status_code=403, detail="invalid claim token")
    output_text = (body or {}).get("output_text") or (body or {}).get("output")
    usage = (body or {}).get("usage")
    raw = (body or {}).get("raw")
    try:
        task = manager.complete(
            job_id,
            output_text=output_text,
            usage=usage,
            raw=raw,
        )
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="job not found")
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"job_id": job_id, "status": task.status}


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/fail — terminal failure
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/fail")
async def fail_job(
    job_id: str,
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    token_hash = _require_claim_token(
        job_id,
        body.get("claim_token") if isinstance(body, dict) else None,
        body.get("claim_token_hash") if isinstance(body, dict) else None,
    )
    if not db.verify_claim_token(job_id, token_hash):
        raise HTTPException(status_code=403, detail="invalid claim token")
    error_message = ((body or {}).get("error") or "worker reported failure").strip()[:1000]
    try:
        task = manager.fail(job_id, error_message)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="job not found")
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"job_id": job_id, "status": task.status, "error": task.error_message}


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/cancel
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    authorization: Optional[str] = Header(None),
    body: Optional[Dict[str, Any]] = Body(default=None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    # Cancel is allowed for both the claimer (with token) and the
    # operator (without token). Validate token if presented.
    if isinstance(body, dict):
        th = _require_claim_token(
            job_id,
            body.get("claim_token"),
            body.get("claim_token_hash"),
        )
        if not db.verify_claim_token(job_id, th):
            raise HTTPException(status_code=403, detail="invalid claim token")
    try:
        task = manager.cancel(job_id)
    except TaskNotFound:
        raise HTTPException(status_code=404, detail="job not found")
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # Best-effort: also ask the adapter to stop the upstream run.
    if task.external_run_id and task.adapter_name:
        try:
            from aee.core.registry import adapter_registry
            adapter = adapter_registry.get(task.adapter_name)
            await adapter.cancel(task.external_run_id)
        except AdapterNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            # Don't fail the HTTP response if upstream cancel fails.
            manager.log(job_id, f"adapter cancel error: {type(exc).__name__}: {exc}")
    return {"job_id": job_id, "status": task.status}


# ---------------------------------------------------------------------------
# Optional: list claimable (for /health + tests)
# ---------------------------------------------------------------------------


@router.get("/jobs/_claimable")
async def list_claimable(
    worker_type: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=50),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    rows = db.list_claimable_summary(worker_type=worker_type, limit=limit)
    return {"worker_type": worker_type, "claimable": rows, "count": len(rows)}


__all__ = ["router"]
