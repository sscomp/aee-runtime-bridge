"""AEE API layer (FastAPI routers).

AEE-1 only provided the package skeleton. AEE-2 ships the routers:

  * `workers` — POST /workers/register, /workers/{id}/heartbeat,
                GET /workers, GET /workers/{id}
  * `jobs`    — POST /jobs, GET /jobs/{id}, POST /jobs/claim,
                /jobs/{id}/heartbeat, /jobs/{id}/logs,
                /jobs/{id}/complete, /jobs/{id}/fail,
                /jobs/{id}/cancel, GET /jobs/_claimable
  * `runtimes` (AEE-5) — POST /runtimes, GET /runtimes,
                GET /runtimes/{id}, PATCH /runtimes/{id},
                DELETE /runtimes/{id}, POST /runtimes/{id}/enable,
                POST /runtimes/{id}/disable,
                POST /runtimes/{id}/health-check,
                PATCH /runtimes/{id}/health,
                GET /runtimes/{id}/dispatches

AEE-4 adds `/v1/...` aliases (see ADR-007). The same handlers are
re-mounted under a `prefix="/v1"` router so:

  * `POST /v1/workers/register` and `POST /workers/register`
    share the same handler (and the same auth + validation).
  * `/v1/...` is the canonical forward path documented in
    `docs/runtime/Worker_Runtime_Contract.md` §2.
  * `/jobs/...` and `/workers/...` are kept as legacy aliases
    for backward compat with the existing AEE-2 / AEE-3 surface
    and the GPT-Action compat layer (`/runs`).

The `/runs` legacy endpoints remain in `app.py` for now; the plan
is to migrate them to thin aliases over `/jobs` in AEE-5
(compatibility layer). Until then both surfaces work and share
the same `BRIDGE_API_KEY`.
"""
from __future__ import annotations

from fastapi import APIRouter

from .jobs import router as jobs_router
from .runtimes import router as runtimes_router
from .workers import router as workers_router

# Combined router. FastAPI handles prefix merging.
api_router = APIRouter()
api_router.include_router(jobs_router)
api_router.include_router(workers_router)
# AEE-5: Runtime management + query endpoints. Mounted
# under both `/runtimes` and `/v1/runtimes` (the v1
# alias is added below).
api_router.include_router(runtimes_router)

# AEE-4: `/v1/...` aliases (ADR-007). Re-include the same routers
# under a prefix; FastAPI re-uses the same handler objects, so the
# behaviour is identical to the legacy paths. Both the canonical
# and the legacy URLs work; the contract document is explicit that
# `/v1/...` is the forward-looking path. A future `/v2/...` would
# similarly re-include (or replace) the router under a new prefix.
v1_router = APIRouter(prefix="/v1")
v1_router.include_router(jobs_router)
v1_router.include_router(workers_router)
v1_router.include_router(runtimes_router)

# Wire the v1 prefix into the combined router. Now both
# `api_router` and the `v1_router` are mounted by `app.py`.
api_router.include_router(v1_router)

__all__ = ["api_router", "v1_router"]
