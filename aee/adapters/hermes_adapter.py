"""HermesAdapter — wraps the Hermes M2 `/v1/runs` API.

Extracted from `app.py` (AEE-1) so that the dispatcher can talk to
Hermes the same way it will talk to the AEE Lightweight Agent Runtime / Claude Code Agent
in later phases. The wire-level semantics are unchanged.

Endpoints wrapped
-----------------
* `POST {HERMES_BASE_URL}/v1/runs`           — submit()
* `GET  {HERMES_BASE_URL}/v1/runs/{id}`      — poll()
* `POST {HERMES_BASE_URL}/v1/runs/{id}/stop` — cancel()

Configuration
-------------
* `HERMES_BASE_URL` — env var, default `http://127.0.0.1:8642`.
* `HERMES_API_KEY`  — env var; missing key causes submit/poll/cancel
                      to raise `RuntimeError` (caller surfaces as 500
                      / 502 just like the old inline implementation).
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional

import httpx

from aee.adapters.base import (
    AdapterNotFoundError,
    RuntimeAdapter,
    RuntimeCancelResult,
    RuntimeError,
    RuntimePollResult,
    RuntimeSubmitResult,
    UnknownExternalRunError,
)


DEFAULT_BASE_URL = "http://127.0.0.1:8642"


# Status vocabulary we surface to AEE. Hermes' own status strings
# include "queued", "running", "completed", "failed", "cancelled",
# "cancelling" — we keep them as-is so the dispatcher's state machine
# stays the single source of truth.
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timeout"}


def _read_config() -> tuple[str, str]:
    base = os.getenv("HERMES_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    key = os.getenv("HERMES_API_KEY", "").strip()
    return base, key


def _headers(api_key: str) -> dict[str, str]:
    if not api_key:
        raise RuntimeError("HERMES_API_KEY is not configured")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


class HermesAdapter:
    """AEE RuntimeAdapter implementation for Hermes M2.

    The class is intentionally lightweight: no FastAPI dependencies, no
    dispatcher state — only the three async methods required by
    `RuntimeAdapter` plus a `health()` helper used by `/health`.
    """

    name = "hermes"
    runtime_type = "hermes"

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env_base, env_key = _read_config()
        self._base_url = (base_url or env_base).rstrip("/")
        self._api_key = api_key if api_key is not None else env_key
        # Allow tests to inject a custom client (e.g. with mock transport).
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30)

    # -- Lifecycle helpers ------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "HermesAdapter":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        await self.aclose()

    # -- Health -----------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """Best-effort health probe — used by the bridge `/health`."""
        try:
            r = await self._client.get(
                f"{self._base_url}/health",
                headers=_headers(self._api_key),
            )
            return {
                "ok": r.status_code == 200,
                "status_code": r.status_code,
            }
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    # -- RuntimeAdapter protocol -----------------------------------------

    async def submit(self, job: "Job") -> RuntimeSubmitResult:  # noqa: F821
        """POST /v1/runs — returns the Hermes `run_id` we should track.

        The AEE `Job` dataclass is type-hinted only for tooling; this
        method only reads the attributes documented on `Job` so any
        duck-typed substitute (e.g. a Pydantic model) also works.
        """
        payload = self._build_submit_payload(job)
        try:
            r = await self._client.post(
                f"{self._base_url}/v1/runs",
                headers=_headers(self._api_key),
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"hermes submit timeout: {type(exc).__name__}",
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"hermes submit error: {type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc

        if r.status_code >= 400:
            raise RuntimeError(
                f"hermes submit HTTP {r.status_code}: {r.text[:200]}"
            )

        data = r.json() if r.content else {}
        run_id = (data or {}).get("run_id") or (data or {}).get("id")
        if not run_id:
            raise RuntimeError(
                f"hermes submit returned no run_id: {data!r}"
            )
        return RuntimeSubmitResult(
            external_run_id=run_id,
            status=(data or {}).get("status", "queued"),
            raw=data,
        )

    async def poll(self, external_run_id: str) -> RuntimePollResult:
        try:
            r = await self._client.get(
                f"{self._base_url}/v1/runs/{external_run_id}",
                headers=_headers(self._api_key),
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"hermes poll error: {type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc

        if r.status_code == 404:
            raise UnknownExternalRunError(
                f"hermes run {external_run_id!r} not found"
            )
        if r.status_code >= 400:
            raise RuntimeError(
                f"hermes poll HTTP {r.status_code}: {r.text[:200]}"
            )

        data = r.json() if r.content else {}
        status = (data or {}).get("status", "unknown")
        return RuntimePollResult(
            external_run_id=external_run_id,
            status=status,
            is_terminal=status in _TERMINAL_STATUSES,
            output=(data or {}).get("output"),
            error=(data or {}).get("error"),
            usage=(data or {}).get("usage"),
            raw=data,
        )

    async def cancel(self, external_run_id: str) -> RuntimeCancelResult:
        try:
            r = await self._client.post(
                f"{self._base_url}/v1/runs/{external_run_id}/stop",
                headers=_headers(self._api_key),
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"hermes cancel error: {type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc

        if r.status_code == 404:
            # Already gone — treat as cancelled from our side.
            return RuntimeCancelResult(
                external_run_id=external_run_id,
                cancelled=True,
                reason="run not found (already gone)",
            )
        if r.status_code >= 400:
            raise RuntimeError(
                f"hermes cancel HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {}
        return RuntimeCancelResult(
            external_run_id=external_run_id,
            cancelled=True,
            reason=(data or {}).get("status", "stop_requested"),
            raw=data,
        )

    # -- Internals --------------------------------------------------------

    def _build_submit_payload(self, job: "Job") -> dict[str, Any]:  # noqa: F821
        """Translate an AEE Job into a Hermes `/v1/runs` body.

        Notes:
            * The `instructions` text is the same as the legacy
              `app.py` build — kept verbatim so existing behaviour
              is preserved.
            * `metadata` carries mode/client_source/routing so the
              upstream audit log keeps working.
        """
        metadata: dict[str, Any] = {
            "client_source": getattr(job, "client_source", None),
            "model_name": getattr(job, "model_name", None),
        }
        mode = getattr(job, "mode", None)
        if mode and mode != "normal":
            metadata["mode"] = mode
        metadata = {k: v for k, v in metadata.items() if v is not None}

        payload: dict[str, Any] = {
            "input": getattr(job, "input_text", None) or getattr(job, "input", ""),
            "session_id": getattr(job, "session_id", None),
            "instructions": (
                "You are Hermes M2 (Abacus.ai) runtime executing tasks for the Dingde "
                "ChatGPT Orchestrator. Be careful, concise, and always return concrete "
                "results. When you run shell commands, return stdout/stderr verbatim. "
                "Never echo API keys, tokens, or contents of ~/.hermes/.env. For any "
                "high-risk or destructive operation, ask the user to confirm first."
            ),
        }
        if metadata:
            payload["metadata"] = metadata
        return payload


def build_default() -> HermesAdapter:
    """Build a HermesAdapter from the current env (no client override)."""
    return HermesAdapter()


__all__ = ["HermesAdapter", "build_default"]
