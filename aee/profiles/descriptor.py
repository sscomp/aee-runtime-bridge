"""AEE-8.1 — Profile descriptor plumbing (read-only).

This module is the **single source of truth** for AEE profile
descriptors. It is intentionally read-only: it can read, parse,
validate, and expose profile descriptors to callers, but it
performs **no** installation, switching, write-back, migration,
or runtime mutation.

Profiles are **runtime selections**, not source forks (per the
AEE Profile Unification Decision MINI, §1–§3). Four profiles are
recognized:

    full        — complete product surface
    mini        — lightweight dispatch surface (subsumes AEE-MINI)
    edge        — read-only inspection / reporting surface
    developer  — local development / test sandbox

This module provides:

* :data:`KNOWN_PROFILES` — the immutable set of valid profile names.
* :data:`DEFAULT_PROFILE` — the profile assumed when none is supplied.
* :class:`ProfileDescriptor` — a typed, frozen dataclass carrying
  the parsed descriptor fields.
* :func:`parse_profile` — coerce a raw string to a known profile
  name, applying the default and rejecting unknown values.
* :func:`get_descriptor` — return the :class:`ProfileDescriptor`
  for a profile name, or raise :class:`UnknownProfileError`.
* :func:`is_known_profile` — boolean predicate.
* :func:`safety_tier_for` — return the safety tier string for a
  profile (read from the descriptor, not from live config).

Design invariants (AEE-8.1 contract):

1. **No mutation.** No function in this module writes to disk,
   the DB, the runtime, or any service. All functions are pure.
2. **No imports of** ``dispatcher``, ``sqlite3``, ``subprocess``,
   ``os.environ`` / ``os.getenv``, ``requests``, ``urllib``,
   ``httpx``, or ``http.client``. The module is isolation-safe.
3. **Default is** ``full``. When no profile is supplied, the
   descriptor for ``full`` is returned. This preserves the
   existing behavior of every call site that does not opt in.
4. **Unknown profiles raise.** ``parse_profile("bogus")`` raises
   :class:`UnknownProfileError`; it does not silently fall back.
5. **No call site is changed.** This module only *provides* the
   plumbing; activation (passing ``profile=mini`` from a real
   dispatch call site) is a later phase.

Run: ``PYTHONPATH=. python3 -m unittest discover -s aee/tests -p 'test_aee81*' -v``
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The canonical set of valid profile names. Order is the declaration
#: order from the Decision MINI §3 (full, mini, edge, developer).
KNOWN_PROFILES: Tuple[str, ...] = ("full", "mini", "edge", "developer")

#: The profile assumed when none is supplied. Per Decision MINI §5,
#: the new ``profile`` field defaults to ``full`` so current
#: behavior is preserved byte-for-byte.
DEFAULT_PROFILE: str = "full"

#: Frozen set form for O(1) membership checks.
_KNOWN_SET: FrozenSet[str] = frozenset(KNOWN_PROFILES)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class UnknownProfileError(ValueError):
    """Raised when a profile name is not in :data:`KNOWN_PROFILES`.

    This is a ``ValueError`` subclass so callers that already catch
    ``ValueError`` for input validation get the right behavior
    automatically. The ``profile`` attribute carries the rejected
    value for diagnostics.
    """

    def __init__(self, profile: Any) -> None:
        self.profile = profile
        super().__init__(
            f"unknown profile: {profile!r}; "
            f"expected one of {list(KNOWN_PROFILES)}"
        )


class InvalidDescriptorError(ValueError):
    """Raised when a profile descriptor's internal data is inconsistent.

    This is a defensive error: the in-memory descriptor table is a
    module-level constant, so this should never fire in practice.
    It exists so that callers can distinguish "unknown profile"
    (caller error) from "descriptor table corrupted" (programmer
    error).
    """


# ---------------------------------------------------------------------------
# Descriptor dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileDescriptor:
    """Typed, immutable description of a single AEE profile.

    Fields mirror Decision MINI §3. The descriptor is a *read-only
    snapshot* of what the profile means; it does not carry live
    runtime state and does not enforce anything by itself. Enforcement
    is the job of the safety gate (a later phase).
    """

    name: str
    purpose: str
    audience: str
    runtime_footprint: str
    safety_tier: str
    #: Optional toolset restriction description (human-readable).
    #: Empty string means "no restriction beyond the canonical set".
    toolset_restriction: str = ""
    #: Whether the profile may create cron jobs. Per Decision MINI
    #: §3 mini, mini cannot. ``full`` can. ``edge`` and ``developer``
    #: cannot (edge is read-only; developer is sandboxed).
    can_create_cron: bool = False
    #: Whether the profile may delegate subagents. Per Decision MINI
    #: §3 mini, mini cannot. ``full`` can.
    can_delegate_subagents: bool = False
    #: Whether the profile is read-only with respect to the DB.
    #: ``edge`` is the only profile where this is ``True``.
    is_read_only: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable dict representation.

        The shape is stable and additive: new fields are only ever
        appended to the end. Callers must not rely on key order.
        """
        return {
            "name": self.name,
            "purpose": self.purpose,
            "audience": self.audience,
            "runtime_footprint": self.runtime_footprint,
            "safety_tier": self.safety_tier,
            "toolset_restriction": self.toolset_restriction,
            "can_create_cron": self.can_create_cron,
            "can_delegate_subagents": self.can_delegate_subagents,
            "is_read_only": self.is_read_only,
        }


# ---------------------------------------------------------------------------
# Descriptor table (module-level constant)
# ---------------------------------------------------------------------------

# The descriptor table is a module-level constant. It is built once at
# import time and never mutated. Every entry is a frozen dataclass, so
# accidental in-place mutation is impossible (raises FrozenInstanceError).
#
# Sources for each field:
#   - Decision MINI §3 (full / mini / edge / developer)
#   - Decision MINI §4 invariant 6 ("No silent profile escape"):
#     mini cannot do cron / subagent / long pipeline; the
#     can_create_cron / can_delegate_subagents flags encode this.
#   - Decision MINI §3 edge: "Read-only DB access (PRAGMA
#     query_only=1), no dispatcher writes, no bridge writes" →
#     is_read_only=True.
#   - Decision MINI §3 developer: "Sandboxed tempdir, throwaway
#     SQLite, no production DB access" → is_read_only=False
#     (developer may mutate, just not in production).
_DESCRIPTORS: Dict[str, ProfileDescriptor] = {
    "full": ProfileDescriptor(
        name="full",
        purpose=(
            "Complete product surface. All features, all adapters, "
            "all scorers, all graph queries, all observability."
        ),
        audience=(
            "M2 agent + direct human operators running the full "
            "intelligence / orchestration stack."
        ),
        runtime_footprint=(
            "Full venv, full SQLite graph store, full dispatcher, "
            "full bridge."
        ),
        safety_tier="standard",
        toolset_restriction="",
        can_create_cron=True,
        can_delegate_subagents=True,
        is_read_only=False,
    ),
    "mini": ProfileDescriptor(
        name="mini",
        purpose=(
            "Lightweight dispatch surface for external orchestrators "
            "(e.g. ChatGPT Custom GPT Action sending workorders via "
            "the bridge). Subsumes what was previously called "
            "AEE-MINI."
        ),
        audience=(
            "GPT orchestrator → bridge → AEE dispatcher. Minimal "
            "agent loop, minimal toolset, short-lived tasks."
        ),
        runtime_footprint=(
            "Same codebase, restricted toolset (terminal, file, "
            "web subset), short timeout, no long-running pipelines."
        ),
        safety_tier="strict",
        toolset_restriction=(
            "terminal, file, web subset; no cron creation, no "
            "subagent delegation"
        ),
        can_create_cron=False,
        can_delegate_subagents=False,
        is_read_only=False,
    ),
    "edge": ProfileDescriptor(
        name="edge",
        purpose=(
            "Read-only inspection / reporting surface. No mutations, "
            "no dispatch, no side effects."
        ),
        audience=(
            "Status dashboards, audit reviewers, external monitors "
            "pulling reports."
        ),
        runtime_footprint=(
            "Read-only DB access (PRAGMA query_only=1), no "
            "dispatcher writes, no bridge writes."
        ),
        safety_tier="strictest",
        toolset_restriction="read-only; any write attempt is a policy violation",
        can_create_cron=False,
        can_delegate_subagents=False,
        is_read_only=True,
    ),
    "developer": ProfileDescriptor(
        name="developer",
        purpose=(
            "Local development / test sandbox. May mutate working "
            "tree, may run uncommitted code, may create test "
            "fixtures."
        ),
        audience=(
            "M2 agent during AEE slice development, subagents "
            "running characterization tests."
        ),
        runtime_footprint=(
            "Sandboxed tempdir, throwaway SQLite, no production DB "
            "access, no production bridge."
        ),
        safety_tier="relaxed_within_sandbox",
        toolset_restriction="production access blocked at the profile boundary",
        can_create_cron=False,
        can_delegate_subagents=True,
        is_read_only=False,
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_known_profile(profile: Any) -> bool:
    """Return ``True`` iff ``profile`` is a known profile name.

    The check is case-sensitive and type-strict: only a ``str``
    that exactly matches one of :data:`KNOWN_PROFILES` returns
    ``True``. ``None``, integers, empty strings, and wrong-case
    variants all return ``False``.
    """
    return isinstance(profile, str) and profile in _KNOWN_SET


def parse_profile(profile: Optional[str]) -> str:
    """Coerce a raw profile string to a canonical profile name.

    * ``None`` → :data:`DEFAULT_PROFILE` (``"full"``).
    * ``""`` (empty / whitespace) → :data:`DEFAULT_PROFILE`.
    * A known profile name → returned unchanged.
    * Anything else → raises :class:`UnknownProfileError`.

    This function does **not** mutate any state. It is safe to call
    at any point in the dispatch path.
    """
    if profile is None:
        return DEFAULT_PROFILE
    if not isinstance(profile, str):
        raise UnknownProfileError(profile)
    cleaned = profile.strip()
    if not cleaned:
        return DEFAULT_PROFILE
    if cleaned in _KNOWN_SET:
        return cleaned
    raise UnknownProfileError(cleaned)


def get_descriptor(profile: Optional[str] = None) -> ProfileDescriptor:
    """Return the :class:`ProfileDescriptor` for ``profile``.

    * ``None`` / empty → descriptor for :data:`DEFAULT_PROFILE`.
    * Known profile name → its descriptor.
    * Unknown profile → raises :class:`UnknownProfileError`.

    The returned descriptor is a frozen dataclass; mutation
    attempts raise ``FrozenInstanceError``.
    """
    name = parse_profile(profile)
    desc = _DESCRIPTORS.get(name)
    if desc is None:
        # Defensive: the table is a constant built at import time,
        # so this should never fire. If it does, the table is
        # inconsistent with KNOWN_PROFILES — a programmer error.
        raise InvalidDescriptorError(
            f"descriptor table missing entry for known profile {name!r}; "
            f"table keys={sorted(_DESCRIPTORS.keys())}"
        )
    return desc


def safety_tier_for(profile: Optional[str] = None) -> str:
    """Return the safety tier string for ``profile``.

    Convenience wrapper around :func:`get_descriptor`. Returns
    the ``safety_tier`` field of the descriptor. Does not consult
    live config — the value is read from the descriptor table.
    """
    return get_descriptor(profile).safety_tier


def all_descriptors() -> Tuple[ProfileDescriptor, ...]:
    """Return all known descriptors in declaration order.

    The returned tuple is a snapshot; mutating it does not affect
    the module-level table. Each element is a frozen dataclass.
    """
    return tuple(_DESCRIPTORS[name] for name in KNOWN_PROFILES)


__all__ = [
    "KNOWN_PROFILES",
    "DEFAULT_PROFILE",
    "ProfileDescriptor",
    "UnknownProfileError",
    "InvalidDescriptorError",
    "is_known_profile",
    "parse_profile",
    "get_descriptor",
    "safety_tier_for",
    "all_descriptors",
]