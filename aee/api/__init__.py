"""AEE API layer (FastAPI routers).

AEE-1 only provided the package skeleton. AEE-2 ships the routers:

  * `workers` — POST /workers/register, /workers/{id}/heartbeat,
                GET /workers, GET /workers/{id}
  * `jobs`    — POST /jobs, GET /jobs/{id}, POST /jobs/claim,
                /jobs/{id}/heartbeat, /jobs/{id}/logs,
                /jobs/{id}/complete, /jobs/{id}/fail,
                /jobs/{id}/cancel, GET /jobs/_claimable

Both routers are mounted by `app.py` under the same `/` prefix
(they self-declare their paths in the `prefix=""` APIRouter
instances).

The `/runs` legacy endpoints remain in `app.py` for now; the plan
is to migrate them to thin aliases over `/jobs` in AEE-5
(compatibility layer). Until then both surfaces work and share
the same `BRIDGE_API_KEY`.
"""
from __future__ import annotations

from fastapi import APIRouter

from .jobs import router as jobs_router
from .workers import router as workers_router

# Combined router. FastAPI handles prefix merging.
api_router = APIRouter()
api_router.include_router(jobs_router)
api_router.include_router(workers_router)

__all__ = ["api_router"]
