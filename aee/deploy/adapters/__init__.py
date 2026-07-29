"""AEE Epic 9.6 §21.6 — Platform Adapters package.

This package holds the Platform Adapter implementations. The
:class:`~aee.deploy.contract.PlatformAdapter` Protocol and the
contract data models live in :mod:`aee.deploy.contract`. Each
adapter lives in its own module and is the only place host-specific
knowledge lives; AEE Core is adapter-agnostic.

Reference adapters (§21.6.E table):

* :mod:`aee.deploy.adapters.abacus`     — `container` (Abacus.ai) — supervisord + Cloudflare Tunnel
* :mod:`aee.deploy.adapters.zo`        — `container` (Zo.computer) — Zo-native supervisor
* :mod:`aee.deploy.adapters.macbook`   — `laptop` (MacBook Pro) — launchd / tmux
* :mod:`aee.deploy.adapters.docker`    — `docker-host` — docker-compose
* :mod:`aee.deploy.adapters.terraform_aws` — `cloud-vm`/`cloud-container` (optional, placeholder)
* :mod:`aee.deploy.adapters.windows`      — Windows host (skeleton, W1 §16)

Design invariants (§21.6.D):

1. Adapters MUST NOT import cloud SDKs into AEE Core (they live in
   ``aee/deploy/adapters/``, not in core).
2. Adapters MUST NOT mutate AEE Core source, the Docker image, or
   the profile descriptor.
3. Adapters MUST NOT branch on ``provider_hint`` (selection uses
   ``class``).
4. Adapters MUST NOT assume inbound connectivity is available (the
   default is egress-only).
5. Adapters MAY shell out to host-native tooling (``supervisorctl``,
   ``launchctl``, ``docker``, ``terraform``).
6. Adapters MAY write host-specific supervisor unit / tunnel config
   files (the result of :meth:`materialize`).
"""
from __future__ import annotations

from aee.deploy.adapters.base import PlatformAdapter
from aee.deploy.adapters.abacus import AbacusAdapter
from aee.deploy.adapters.zo import ZoAdapter
from aee.deploy.adapters.macbook import MacBookAdapter
from aee.deploy.adapters.docker import DockerAdapter
from aee.deploy.adapters.terraform_aws import TerraformAwsAdapter
from aee.deploy.adapters.windows import WindowsAdapter

__all__ = [
    "PlatformAdapter",
    "AbacusAdapter",
    "ZoAdapter",
    "MacBookAdapter",
    "DockerAdapter",
    "TerraformAwsAdapter",
    "WindowsAdapter",
]