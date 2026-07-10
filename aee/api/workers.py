"""AEE-2 workers API — register a worker, heartbeat, list, get.

Worker registration is idempotent: re-registering the same
`worker_id` updates the capabilities / max_concurrent / allowlist
but preserves `registered_at`. The API is intentionally minimal —
there is no separate "deregister" endpoint in AEE-2 (operators can
delete a row via the CLI if needed; a proper deregister lands in
AEE-3 once we have a UI for the worker fleet).
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
    record = db.upsert_worker(
        worker_id=worker_id,
        worker_name=worker_name,
        worker_type=worker_type,
        hostname=body.get("hostname"),
        capabilities=[c.strip() for c in capabilities if c.strip()],
        workdir_allowlist=[p.strip() for p in workdir_allowlist if p.strip()],
        max_concurrent=max(1, max_concurrent),
    )
    return {
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
    job_id = (body or {}).get("job_id") if isinstance(body, dict) else None
    record = db.update_worker_heartbeat(worker_id, job_id=job_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"worker {worker_id!r} not registered; POST /workers/register first",
        )
    return {
        "worker_id": record["worker_id"],
        "last_heartbeat_at": record["last_heartbeat_at"],
        "last_job_id": record["last_job_id"],
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
    return {"count": len(rows), "workers": rows}


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
    return rec


__all__ = ["router"]
