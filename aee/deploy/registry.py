"""AEE Epic 9.6 §21.6.D/E — Platform Adapter registry and selection.

The registry maps host classes (§21.6.B ``KNOWN_HOST_CLASSES``) to
Platform Adapter instances. Selection uses the host's ``class`` field
(§21.6.B last paragraph: "The ``class`` field drives adapter
selection, not ``provider_hint``").

Design contract (§21.6):

* :func:`select_adapter` returns the adapter registered for the
  host's ``class``. If the class is known but no adapter is
  registered (e.g. ``cloud-vm`` without ``terraform-aws``), the
  function raises :class:`AdapterNotFoundError` so the installer can
  surface a clear error.
* :func:`register_adapter` adds an adapter to the registry. It is
  the extension point: a new host class is supported by writing (or
  reusing) a Platform Adapter and calling ``register_adapter``.
* The registry does **not** branch on ``provider_hint`` (per
  §21.6.B last paragraph and §21.6.D "A Platform Adapter MUST NOT
  branch on ``provider_hint``").

The default registry is populated with the §21.6.E reference
adapters at import time:

    ``container``      → :class:`AbacusAdapter` (M2 reference)
    ``laptop``         → :class:`MacBookAdapter` (B2 reference)
    ``docker-host``    → :class:`DockerAdapter` (generic Docker host)
    ``cloud-vm``      → :class:`TerraformAwsAdapter` (optional placeholder)
    ``cloud-container`` → :class:`TerraformAwsAdapter` (optional placeholder)

Note: the ``container`` class is shared by the Abacus (M2) and Zo
(N2) reference adapters. The registry maps ``container`` to
:class:`AbacusAdapter` by default (the M2 reference). N2 deployments
either pass ``--adapter zo`` explicitly or use a host.capabilities.yaml
with ``provider_hint: zo`` (informational only) and the installer's
``--adapter`` flag to override. The ``class``-based selection is the
default; the explicit ``--adapter`` flag is the override.
"""
from __future__ import annotations

from typing import Dict, Optional

from aee.deploy.adapters.abacus import AbacusAdapter
from aee.deploy.adapters.docker import DockerAdapter
from aee.deploy.adapters.macbook import MacBookAdapter
from aee.deploy.adapters.terraform_aws import TerraformAwsAdapter
from aee.deploy.adapters.zo import ZoAdapter
from aee.deploy.contract import HostCapabilities, KNOWN_HOST_CLASSES


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AdapterNotFoundError(KeyError):
    """Raised by :func:`select_adapter` when no adapter is registered
    for the host's ``class`` (and no explicit override was supplied).

    Subclass of :class:`KeyError` so the installer can catch it
    uniformly alongside other registry lookups.
    """


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AdapterRegistry:
    """Maps host classes (§21.6.B) to Platform Adapter instances.

    The registry is the only place host-class → adapter mappings
    live. AEE Core never imports a specific adapter; it asks the
    registry. The registry is a singleton (returned by
    :func:`get_registry`); tests may construct private instances via
    the public constructor.

    Selection rules (§21.6.B + §21.6.D):

    1. If the caller supplies an explicit ``adapter_name`` (e.g.
       ``--adapter zo``), the registry looks up the adapter by name
       (not by class). This is the override path.
    2. Otherwise, the registry looks up the adapter by the host's
       ``class`` field.
    3. If the class is known but no adapter is registered (e.g.
       ``cloud-vm`` without ``terraform-aws``), the registry raises
       :class:`AdapterNotFoundError`.
    4. The registry does **not** branch on ``provider_hint``.

    Adapters are stored by name; the class→name mapping is a separate
    dict so that ``select_adapter(class="container")`` and
    ``select_adapter(adapter_name="abacus")`` both work.
    """

    def __init__(self) -> None:
        self._by_name: Dict[str, object] = {}
        self._by_class: Dict[str, str] = {}

    def register(self, adapter: object) -> None:
        """Register an adapter by its ``name`` attribute.

        The adapter's ``name`` must be a non-empty string. Registering
        an adapter with the same name replaces the previous entry
        (the registry is idempotent for the same adapter).
        """
        name = getattr(adapter, "name", None)
        if not name or not isinstance(name, str):
            raise ValueError(
                f"adapter {adapter!r} must have a non-empty string `name`"
            )
        self._by_name[name] = adapter

    def map_class(self, host_class: str, adapter_name: str) -> None:
        """Map a host class (§21.6.B) to a registered adapter name.

        This is the extension point: a new host class is supported by
        writing (or reusing) a Platform Adapter, calling
        :meth:`register`, and calling :meth:`map_class` to bind the
        class to the adapter.
        """
        if host_class not in KNOWN_HOST_CLASSES:
            raise ValueError(
                f"unknown host class {host_class!r}; "
                f"expected one of {list(KNOWN_HOST_CLASSES)}"
            )
        if adapter_name not in self._by_name:
            raise AdapterNotFoundError(
                f"adapter {adapter_name!r} not registered; "
                f"call register() first"
            )
        self._by_class[host_class] = adapter_name

    def select(
        self,
        *,
        cap: Optional[HostCapabilities] = None,
        adapter_name: Optional[str] = None,
        host_class: Optional[str] = None,
    ) -> object:
        """Return the adapter for the given host or explicit name.

        Resolution order (per the design contract above):

        1. ``adapter_name`` (explicit override, e.g. ``--adapter zo``)
        2. ``host_class`` (caller-supplied class string)
        3. ``cap.class_`` (host capability document's ``class`` field)

        Raises :class:`AdapterNotFoundError` if no adapter is
        registered for the resolved name/class.
        """
        if adapter_name is not None:
            if adapter_name not in self._by_name:
                raise AdapterNotFoundError(
                    f"adapter {adapter_name!r} not registered"
                )
            return self._by_name[adapter_name]
        if host_class is None and cap is not None:
            host_class = cap.class_
        if host_class is None:
            raise AdapterNotFoundError(
                "no adapter_name or host_class supplied and no "
                "HostCapabilities to derive host_class from"
            )
        name = self._by_class.get(host_class)
        if name is None:
            raise AdapterNotFoundError(
                f"no adapter mapped for host class {host_class!r}; "
                f"known mappings: {dict(self._by_class)}"
            )
        if name not in self._by_name:
            raise AdapterNotFoundError(
                f"adapter {name!r} mapped for class {host_class!r} "
                f"but not registered"
            )
        return self._by_name[name]

    def list_adapters(self) -> Dict[str, object]:
        """Return a copy of the name → adapter mapping."""
        return dict(self._by_name)

    def list_class_mappings(self) -> Dict[str, str]:
        """Return a copy of the class → adapter-name mapping."""
        return dict(self._by_class)


# ---------------------------------------------------------------------------
# Default registry (populated at import time)
# ---------------------------------------------------------------------------


def _build_default_registry() -> AdapterRegistry:
    """Build the default registry with the §21.6.E reference adapters.

    The default mappings are:

        ``container``       → ``abacus`` (M2 reference)
        ``laptop``          → ``macbook`` (B2 reference)
        ``docker-host``     → ``docker`` (generic Docker host)
        ``cloud-vm``        → ``terraform-aws`` (optional placeholder)
        ``cloud-container`` → ``terraform-aws`` (optional placeholder)

    The ``zo`` adapter is registered by name so operators can pass
    ``--adapter zo`` explicitly; it is not the default for the
    ``container`` class (Abacus is the reference ``container`` host
    per §21.6.F).
    """
    reg = AdapterRegistry()
    reg.register(AbacusAdapter())
    reg.register(ZoAdapter())
    reg.register(MacBookAdapter())
    reg.register(DockerAdapter())
    reg.register(TerraformAwsAdapter())
    reg.map_class("container", "abacus")
    reg.map_class("laptop", "macbook")
    reg.map_class("docker-host", "docker")
    reg.map_class("cloud-vm", "terraform-aws")
    reg.map_class("cloud-container", "terraform-aws")
    return reg


_REGISTRY: AdapterRegistry = _build_default_registry()


def get_registry() -> AdapterRegistry:
    """Return the process-wide default :class:`AdapterRegistry`.

    Tests that need isolation should construct a private
    :class:`AdapterRegistry` rather than mutating the global one.
    """
    return _REGISTRY


def register_adapter(adapter: object) -> None:
    """Register an adapter with the default registry."""
    _REGISTRY.register(adapter)


def select_adapter(
    *,
    cap: Optional[HostCapabilities] = None,
    adapter_name: Optional[str] = None,
    host_class: Optional[str] = None,
) -> object:
    """Select an adapter from the default registry (see
    :meth:`AdapterRegistry.select`)."""
    return _REGISTRY.select(
        cap=cap, adapter_name=adapter_name, host_class=host_class
    )


__all__ = [
    "AdapterRegistry",
    "AdapterNotFoundError",
    "get_registry",
    "register_adapter",
    "select_adapter",
]