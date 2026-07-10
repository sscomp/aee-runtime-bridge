"""AEE-5 Runtime management API.

Exposes the Runtime Registry as a FastAPI router mounted
under both `/runtimes` and `/v1/runtimes` (AEE-4 alias
pattern from `aee/api/__init__.py`).

Auth
----
The same `_require_auth()` as `/v1/jobs` and
`/v1/workers`. AEE-5 does NOT introduce a separate
auth surface.

Endpoints
---------
* `POST   /runtimes`                       — register
* `GET    /runtimes`                       — list / filter
* `GET    /runtimes/search`                — list with label.* filters
* `GET    /runtimes/{runtime_id}`          — detail
* `PATCH  /runtimes/{runtime_id}`          — update
* `DELETE /runtimes/{runtime_id}`          — remove
* `POST   /runtimes/{runtime_id}/enable`   — set enabled=True
* `POST   /runtimes/{runtime_id}/disable`  — set enabled=False
* `POST   /runtimes/{runtime_id}/health-check`
                                             — read current health
* `PATCH  /runtimes/{runtime_id}/health`   — set health
* `GET    /runtimes/{runtime_id}/dispatches`
                                             — list recent dispatches
"""
from __future__ import annotations

import hmac as _hmac
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request

from aee.runtimes.errors import (
    RuntimeNotFoundError,
    RuntimeRegistryError,
    RuntimeValidationError,
)
from aee.runtimes.models import RuntimeDescriptor
from aee.runtimes.registry import runtime_registry


router = APIRouter()


# ---------------------------------------------------------------------------
# Auth (same shape as aee.api.jobs._require_auth — inlined to keep the
# routers importable in isolation)
# ---------------------------------------------------------------------------


def _read_api_key() -> str:
    return os.getenv("DISPATCHER_API_KEY") or os.getenv("BRIDGE_API_KEY", "")


def _require_auth(authorization: Optional[str]) -> None:
    expected = _read_api_key()
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="BRIDGE_API_KEY is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.split(" ", 1)[1].strip()
    if not _hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_runtime_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    runtime_id = body.get("runtime_id")
    runtime_type = body.get("runtime_type")
    if not isinstance(runtime_id, str) or not runtime_id.strip():
        raise HTTPException(
            status_code=400,
            detail="runtime_id is required and must be a non-empty string",
        )
    if not isinstance(runtime_type, str) or not runtime_type.strip():
        raise HTTPException(
            status_code=400,
            detail="runtime_type is required and must be a non-empty string",
        )
    return {
        "runtime_id": runtime_id.strip(),
        "runtime_type": runtime_type.strip(),
        "display_name": (body.get("display_name") or ""),
        "version": (body.get("version") or "1.0.0"),
        "enabled": bool(body.get("enabled", True)),
        "endpoint": (body.get("endpoint") or "local"),
        "capabilities": body.get("capabilities") or [],
        "labels": body.get("labels") or {},
        "limits": body.get("limits") or {},
        "health": body.get("health") or {},
    }


def _validate_patch_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    out: Dict[str, Any] = {}
    for k in (
        "runtime_type",
        "display_name",
        "version",
        "endpoint",
        "capabilities",
        "labels",
        "limits",
        "health",
    ):
        if k in body and body[k] is not None:
            out[k] = body[k]
    if "enabled" in body and body["enabled"] is not None:
        out["enabled"] = bool(body["enabled"])
    return out


def _validate_health_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    status = body.get("status")
    if not isinstance(status, str) or not status.strip():
        raise HTTPException(
            status_code=400,
            detail="status is required and must be a string",
        )
    return {
        "status": status.strip(),
        "message": body.get("message"),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/runtimes")
async def create_runtime(
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Register a new Runtime."""
    _require_auth(authorization)
    payload = _validate_runtime_payload(body)
    try:
        descriptor = RuntimeDescriptor.from_dict(payload)
        runtime_registry.register_runtime(descriptor)
    except RuntimeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return descriptor.to_dict()


@router.get("/runtimes")
async def list_runtimes(
    enabled: Optional[bool] = Query(None),
    runtime_type: Optional[str] = Query(None),
    capability: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """List / filter Runtimes.

    Filter query strings (AEE-5 task spec §5.2):
      * `enabled=true|false`
      * `runtime_type=aee_lightweight`
      * `capability=task.shell`

    For `label.<key>=<value>` filters, use
    `GET /runtimes/search`.
    """
    _require_auth(authorization)
    runtimes = runtime_registry.list_runtimes(
        enabled=enabled, runtime_type=runtime_type
    )
    if capability:
        target = capability.strip().lower()
        runtimes = [
            r
            for r in runtimes
            if target in set(c.lower() for c in r.capabilities.to_list())
        ]
    return {
        "version": "v1",
        "count": len(runtimes),
        "runtimes": [r.to_dict() for r in runtimes],
    }


@router.get("/runtimes/search")
async def search_runtimes(
    request: Request,
    enabled: Optional[bool] = Query(None),
    runtime_type: Optional[str] = Query(None),
    capability: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """List Runtimes with `label.<key>=<value>` filter support.

    Example: `GET /v1/runtimes/search?label.environment=local`.
    """
    _require_auth(authorization)
    label_filters: Dict[str, str] = {}
    for k, v in request.query_params.multi_items():
        if k.startswith("label."):
            label_filters[k[len("label."):]] = v
    runtimes = runtime_registry.list_runtimes(
        enabled=enabled, runtime_type=runtime_type
    )
    if capability:
        target = capability.strip().lower()
        runtimes = [
            r
            for r in runtimes
            if target in set(c.lower() for c in r.capabilities.to_list())
        ]
    if label_filters:
        wanted = {
            k.strip().lower(): str(v).strip().lower()
            for k, v in label_filters.items()
        }
        runtimes = [
            r
            for r in runtimes
            if all(
                {
                    k.strip().lower(): str(v).strip().lower()
                    for k, v in r.labels.items()
                }.get(kk) == vv
                for kk, vv in wanted.items()
            )
        ]
    return {
        "version": "v1",
        "count": len(runtimes),
        "runtimes": [r.to_dict() for r in runtimes],
    }


@router.get("/runtimes/{runtime_id}")
async def get_runtime(
    runtime_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    try:
        r = runtime_registry.get_runtime(runtime_id)
    except RuntimeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    return r.to_dict()


@router.patch("/runtimes/{runtime_id}")
async def patch_runtime(
    runtime_id: str,
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    payload = _validate_patch_payload(body)
    try:
        r = runtime_registry.update_runtime(runtime_id, payload)
    except RuntimeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except RuntimeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return r.to_dict()


@router.delete("/runtimes/{runtime_id}")
async def delete_runtime(
    runtime_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    ok = runtime_registry.unregister_runtime(runtime_id)
    if not ok:
        raise HTTPException(
            status_code=404, detail=f"runtime_id {runtime_id!r} not found"
        )
    return {"version": "v1", "runtime_id": runtime_id, "deleted": True}


@router.post("/runtimes/{runtime_id}/enable")
async def enable_runtime(
    runtime_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    try:
        r = runtime_registry.set_runtime_enabled(runtime_id, True)
    except RuntimeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    return r.to_dict()


@router.post("/runtimes/{runtime_id}/disable")
async def disable_runtime(
    runtime_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    _require_auth(authorization)
    try:
        r = runtime_registry.set_runtime_enabled(runtime_id, False)
    except RuntimeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    return r.to_dict()


@router.post("/runtimes/{runtime_id}/health-check")
async def health_check_runtime(
    runtime_id: str,
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Read the Runtime's current health status.

    AEE-5 task spec §4.7: a minimal health-check
    interface. The v1 implementation returns the
    stored health; a future AEE-6+ may probe the
    Runtime's wire endpoint. The shape is the same
    so callers don't need to change.
    """
    _require_auth(authorization)
    try:
        health = runtime_registry.check_runtime_health(runtime_id)
    except RuntimeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    return {
        "version": "v1",
        "runtime_id": runtime_id,
        "health": health,
    }


@router.patch("/runtimes/{runtime_id}/health")
async def patch_runtime_health(
    runtime_id: str,
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Update a Runtime's health (operator- or
    probe-driven)."""
    _require_auth(authorization)
    payload = _validate_health_payload(body)
    try:
        r = runtime_registry.update_runtime_health(
            runtime_id,
            status=payload["status"],
            message=payload.get("message"),
        )
    except RuntimeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    except RuntimeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return r.to_dict()


@router.get("/runtimes/{runtime_id}/dispatches")
async def list_runtime_dispatches(
    runtime_id: str,
    limit: int = Query(50, ge=1, le=500),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """List the most recent dispatch records for a Runtime."""
    _require_auth(authorization)
    try:
        runtime_registry.get_runtime(runtime_id)
    except RuntimeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict())
    from aee.dispatch.service import dispatch_service
    records = dispatch_service.list_dispatches(
        runtime_id=runtime_id, limit=limit
    )
    return {
        "version": "v1",
        "runtime_id": runtime_id,
        "count": len(records),
        "dispatches": [r.to_dict() for r in records],
    }


__all__ = ["router"]
