"""AEE Epic 9 §21.10 — Deprecation Plan (warning emitter, side-effect free).

This module implements the §21.10 Deprecation Plan contract from the
authoritative Master Plan (``/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md``
§21.10, lines 7804–7815).

Contract:

    Purpose: Define the timeline and process for retiring the AEE-MINI
    brand. No operator is left without a migration path.

    Proposal:
      - Epic 9 ship (2.0.0-rc1): AEE-MINI brand deprecated. AEE-MINI
        1.0.1 is the last release of the line. Repo frozen (security
        patches only). DEPRECATED.md at AEE-MINI repo root.
      - Epic 9 + 1 (2.0.0-rc2): Unified installer's --profile mini
        validated end-to-end on a fresh host. B2 deployments may
        migrate. AEE-MINI install path still works.
      - Epic 9 + 2 (2.0.0 GA): AEE-MINI install path no longer supported
        (not removed -- script still exists, not tested in CI). New
        deployments must use the unified installer.
      - Epic 9 + 4 (2.0.2): AEE-MINI repo archived (marked read-only;
        DEPRECATED.md at root). mini profile string literal is the
        only surviving reference to the "MINI" name.

    No forced migration. Any B2 deployment running AEE-MINI 1.0.1
    continues to run.

Acceptance criteria (§21.A item 10):
    AEE-MINI repo frozen at 1.0.1; DEPRECATED.md at root; no new
    releases.

Design constraints (this module):

1. **Import-safe.** No module-level I/O, no ``sys.exit``, no exception
   raised at import time. Importing this module is a pure namespace
   operation.
2. **Side-effect free.** :func:`emit_deprecation_warning` returns a
   string; it does not log, write, print, or call out. The caller
   decides what to do with the banner.
3. **Idempotent.** Calling :func:`emit_deprecation_warning` twice
   returns the same string.
4. **String constants, not Enum.** The existing release module
   (``aee/release/__init__.py``) uses string constants for the release
   strategy; this module follows the same convention so the two
   modules compose without a type mismatch.
5. **Canonical references.** Every string the module emits references
   Master Plan §21.10 and ADR-009 as the canonical sources.
6. **No deletion, no rename.** This module does not delete, rename,
   archive, or mark read-only any file or directory. The
   ``DEPRECATED.md`` marker at the AEE-MINI repo root is **additive**.

References:
    - Master Plan §21.10 (lines 7804–7815)
    - ADR-009 (Master Plan §9 — Architecture Unification)
    - ``aee/release/__init__.py`` (§21.8 Release Strategy; this module
      is its §21.10 counterpart and intentionally reuses the string
      constant convention)
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical deprecation data (§21.10)
# ---------------------------------------------------------------------------

#: The last version of the AEE-MINI line (§21.8 line 7796, §21.10 line
#: 7809). Archived, not deleted. No new releases of the AEE-MINI line
#: will be made after this version.
AEE_MINI_LAST_VERSION: str = "1.0.1"

#: The deprecation phase label per the §21.M migration timeline
#: (line 7825): "Phase F — Deprecation Start" ships concurrently with
#: Phase E (Release) at Epic 9 ship (``2.0.0-rc1``). This module ships
#: as part of Phase F.
DEPRECATION_PHASE: str = "Phase F — Deprecation Start"

#: The four canonical deprecation phases used by
#: :func:`validate_deprecation_config`. The "archive" sentinel
#: corresponds to Phase H (Epic 9 + 4, ``2.0.2``) per §21.10 line 7812
#: and §21.M line 7827. Order is the Master Plan timeline order; it is
#: NOT used for ordering by ``validate_deprecation_config`` (which is
#: a set membership check, not a sequence check).
_CANONICAL_PHASES = frozenset(
    {
        "Phase F — Deprecation Start",
        "Phase G — GA",
        "Phase H — Archive",
        "archive",
    }
)

#: The ADR reference for the unification decision (same canonical
#: reference as ``aee/release/__init__.py:ADR_REFERENCE``; duplicated
#: here so this module is self-contained and does not import the
#: release package at module load time — keeps import-time I/O at
#: zero).
_ADR_REFERENCE: str = "ADR-009 (§9 of Master Plan)"

#: The Master Plan section that is the canonical source for the
#: deprecation timeline and the "no forced migration" clause.
_MASTER_PLAN_SECTION: str = "§21.10"


# ---------------------------------------------------------------------------
# Pure functions (no side effects)
# ---------------------------------------------------------------------------


def emit_deprecation_warning() -> str:
    """Return the deprecation banner string (idempotent, side-effect free).

    The banner is suitable for logging, CLI stdout, or inclusion in a
    release artifact. The caller decides what to do with the returned
    string; this function performs no I/O of any kind.

    Returns:
        A non-empty deprecation banner string containing the literal
        ``"DEPRECATED"``, the AEE-MINI last version (``"1.0.1"``), the
        ``--profile mini`` upgrade path, and references to ADR-009 and
        Master Plan §21.10.

    Notes:
        - Idempotent: repeated calls return the same string.
        - No side effects: no logging, no ``print``, no filesystem
          writes, no subprocess calls, no network operations.
        - No exceptions raised on the happy path.
    """
    return (
        "DEPRECATED: AEE-MINI is deprecated as of Epic 9 "
        "(ADR-009; Master Plan §21.10). "
        "AEE-MINI {last} is the last release of the AEE-MINI line. "
        "Upgrade path: fresh install of the unified AEE product with "
        "`--profile mini` (not in-place). "
        "No forced migration: existing deployments continue to run. "
        "References: ADR-009, Master Plan §21.10."
    ).format(last=AEE_MINI_LAST_VERSION)


def is_aee_mini_deprecated() -> bool:
    """Return ``True`` once §21.10 ships.

    §21.10 ships with Epic 9 (``2.0.0-rc1``); this module is the §21.10
    artifact, so once it is importable the deprecation is in effect.
    The function is a constant ``True`` so callers can wire
    conditional deprecation behavior (e.g. emit the warning on CLI
    startup) without tracking a separate flag.

    Returns:
        ``True`` (AEE-MINI is deprecated as of §21.10).
    """
    return True


def validate_deprecation_config(phase: str) -> bool:
    """Validate a deprecation phase name against the canonical set.

    The canonical phases are the four labels from the §21.10 / §21.M
    timeline:

    - ``"Phase F — Deprecation Start"`` (Epic 9 ship, ``2.0.0-rc1``)
    - ``"Phase G — GA"`` (Epic 9 + 2, ``2.0.0`` GA)
    - ``"Phase H — Archive"`` (Epic 9 + 4, ``2.0.2`` archive)
    - ``"archive"`` — the sentinel form used in some machine-readable
      contexts to mean "the repo is archived" (Phase H)

    Any other value is rejected. This is a **set membership** check,
    not a sequence or ordering check.

    Args:
        phase: The phase name to validate.

    Returns:
        ``True`` if ``phase`` is one of the four canonical phases;
        ``False`` otherwise (including for ``None``, empty string, or
        unknown strings).
    """
    if not isinstance(phase, str):
        return False
    return phase in _CANONICAL_PHASES


# ---------------------------------------------------------------------------
# Module-level I/O guard (defensive; never triggers in normal use)
# ---------------------------------------------------------------------------

# This module performs no I/O at import time. The constants and
# functions above are pure. There is no ``sys.exit``, no ``logging``
# configuration, no filesystem access, and no network access at module
# load. Importing this module from any context (test, CLI, library) is
# safe and side-effect free.


__all__ = [
    "AEE_MINI_LAST_VERSION",
    "DEPRECATION_PHASE",
    "emit_deprecation_warning",
    "is_aee_mini_deprecated",
    "validate_deprecation_config",
]