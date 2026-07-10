"""AEE-2 / AEE-4 workers API — register a worker, heartbeat, list, get.

Worker registration is idempotent: re-registering the same
`worker_id` updates the capabilities / max_concurrent / allowlist
/ metadata but preserves `registered_at`. The API is intentionally
minimal — there is no separate "deregister" endpoint in AEE-2
(operators can delete a row via the CLI if needed; a proper
deregister lands in AEE-5 once we have a UI for the worker fleet).

AEE-4 changes (Worker Runtime Contract):
  * `POST /workers/register` accepts 8 new optional metadata fields
    (runtime_name, runtime_version, operating_system, architecture,
    python_version, node_version, git_version, start_time) and an
    initial `status` / `status_message`. Persisted as-is.
  * `POST /workers/{id}/heartbeat` accepts `status` (5 canonical
    values) and `status_message`; `status` changes are stamped with
    `last_status_change_at`. Invalid statuses are rejected with 400
    (unlike the DB-level coercion — the API is the contract).
  * The same handlers are mounted under `/v1/...` for forward
    compatibility (see `aee/api/__init__.py`).
"""
from __future__ import annotations

import hmac as _hmac
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Header, HTTPException

from dispatcher import db


router = APIRouter()


# Worker id is a free-form string, but we restrict the charset so
# it can safely appear in URLs and DB primary keys.
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


# AEE-4: canonical worker-status vocabulary. Mirrored from
# `dispatcher.db.WORKER_STATUSES` so we can validate at the API
# boundary before the value reaches the DB layer. The DB layer
# silently coerces unknown values; the API rejects them with
# HTTP 400, per the Worker Runtime Contract.
_VALID_STATUSES = frozenset(db.WORKER_STATUSES)


def _read_api_key() -> str:
    return os.getenv("DISPATCHER_API_KEY") or os.getenv("BRIDGE_API_KEY", "")


def _require_auth(authorization: Optional[str]) -> None:
    expected = _read_api_key()
    if not expected:
        raise HTTPException(status_code=500, detail="BRIDGE_API_KEY is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.split(" ", 1)[1].strip()
    if not _hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _validate_worker_id(worker_id: str) -> str:
    if not _WORKER_ID_RE.match(worker_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "worker_id must match [A-Za-z0-9_.-]{1,64}; got "
                f"{worker_id!r}"
            ),
        )
    return worker_id


def _validate_optional_str(body: Dict[str, Any], field: str) -> Optional[str]:
    """AEE-4 helper: pull an optional string field. Reject non-string,
    non-None values with HTTP 400. None and missing both yield None.
    """
    val = body.get(field)
    if val is None:
        return None
    if not isinstance(val, str):
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be a string (or omitted); got {type(val).__name__}",
        )
    return val


# ---------------------------------------------------------------------------
# POST /workers/register
# ---------------------------------------------------------------------------


@router.post("/workers/register")
async def register_worker(
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    # worker_id is auto-derived from worker_name when not supplied.
    raw_id = body.get("worker_id")
    if raw_id is None or not str(raw_id).strip():
        name = (body.get("worker_name") or "").strip()
        if not name:
            raise HTTPException(
                status_code=400,
                detail="worker_id or worker_name is required",
            )
        # Derive a stable id from the name (lowercased, non-alnum → "_").
        derived = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name).strip("_")
        worker_id = _validate_worker_id(derived)
    else:
        worker_id = _validate_worker_id(str(raw_id).strip())
    worker_name = (body.get("worker_name") or worker_id).strip()
    worker_type = (body.get("worker_type") or "").strip()
    if not worker_type:
        raise HTTPException(status_code=400, detail="worker_type is required")
    capabilities = body.get("capabilities") or []
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise HTTPException(status_code=400, detail="capabilities must be a list of strings")
    workdir_allowlist = body.get("workdir_allowlist") or []
    if not isinstance(workdir_allowlist, list) or not all(isinstance(p, str) for p in workdir_allowlist):
        raise HTTPException(status_code=400, detail="workdir_allowlist must be a list of strings")
    max_concurrent = int(body.get("max_concurrent") or 1)
    # AEE-4: 8 optional metadata fields + initial status.
    # All are strings or None; reject only the wrong-type case so a
    # worker that sends a stale schema (e.g. AEE-2 client) still works.
    metadata = {
        k: _validate_optional_str(body, k)
        for k in (
            "runtime_name",
            "runtime_version",
            "operating_system",
            "architecture",
            "python_version",
            "node_version",
            "git_version",
            "start_time",
        )
    }
    initial_status = _validate_optional_str(body, "status")
    if initial_status is not None and initial_status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"status must be one of {sorted(_VALID_STATUSES)}; "
                f"got {initial_status!r}"
            ),
        )
    initial_status_message = _validate_optional_str(body, "status_message")
    record = db.upsert_worker(
        worker_id=worker_id,
        worker_name=worker_name,
        worker_type=worker_type,
        hostname=body.get("hostname"),
        capabilities=[c.strip() for c in capabilities if c.strip()],
        workdir_allowlist=[p.strip() for p in workdir_allowlist if p.strip()],
        max_concurrent=max(1, max_concurrent),
        **metadata,
        status=initial_status,
        status_message=initial_status_message,
    )
    return {
        "version": "v1",
        "worker_id": record["worker_id"],
        "registered": True,
        "registered_at": record["registered_at"],
        "worker_type": record["worker_type"],
    }


# ---------------------------------------------------------------------------
# POST /workers/{worker_id}/heartbeat
# ---------------------------------------------------------------------------


@router.post("/workers/{worker_id}/heartbeat")
async def heartbeat_worker(
    worker_id: str,
    body: Optional[Dict[str, Any]] = Body(default=None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    worker_id = _validate_worker_id(worker_id)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    job_id = body.get("job_id")
    # AEE-4: status / status_message. Validate the status at the
    # API boundary; the DB layer is forgiving but the contract
    # isn't. An unknown status is HTTP 400, not silently coerced.
    status_val = body.get("status")
    if status_val is not None and (
        not isinstance(status_val, str) or status_val not in _VALID_STATUSES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"status must be one of {sorted(_VALID_STATUSES)}; "
                f"got {status_val!r}"
            ),
        )
    status_message = body.get("status_message")
    if status_message is not None and not isinstance(status_message, str):
        raise HTTPException(
            status_code=400,
            detail="status_message must be a string (or omitted)",
        )
    record = db.update_worker_heartbeat(
        worker_id,
        job_id=job_id,
        status=status_val,
        status_message=status_message,
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"worker {worker_id!r} not registered; POST /workers/register first",
        )
    return {
        "version": "v1",
        "worker_id": record["worker_id"],
        "last_heartbeat_at": record["last_heartbeat_at"],
        "last_job_id": record["last_job_id"],
        "status": record["status"],
        "status_message": record["status_message"],
        "last_status_change_at": record["last_status_change_at"],
    }


# ---------------------------------------------------------------------------
# GET /workers (list)
# ---------------------------------------------------------------------------


@router.get("/workers")
async def list_workers_endpoint(
    worker_type: Optional[str] = None,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    rows = db.list_workers(worker_type=worker_type)
    return {"version": "v1", "count": len(rows), "workers": rows}


# ---------------------------------------------------------------------------
# GET /workers/{worker_id}
# ---------------------------------------------------------------------------


@router.get("/workers/{worker_id}")
async def get_worker_endpoint(
    worker_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    worker_id = _validate_worker_id(worker_id)
    rec = db.get_worker(worker_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"worker {worker_id!r} not found")
    return {"version": "v1", **rec}


__all__ = ["router"]
