"""AEE Epic 9.8 §21.8 — Release Strategy (unified version + changelog).

This package implements the §21.8 Release Strategy contract from the
authoritative Master Plan (`/home/ubuntu/Abacus/AEE/AEE_MASTER_PLAN.md`
§21.8):

    Purpose: Unified product versions and releases. No confusion between
    AEE-MINI ``1.0.x`` and AEE ``2.0.x``.

    Proposal: Unified product version ``2.0.0-rc1`` on first Epic 9
    release; ``2.0.0`` GA when §21.10 completes. SemVer: MAJOR per Epic /
    MINOR per sub-section / PATCH per bugfix. Release artifacts: one
    Docker image (``aee:X.Y.Z``), one tarball (``aee-X.Y.Z.tar.gz``), one
    changelog entry. AEE-MINI ``1.0.1`` is the **last release of the
    AEE-MINI line**; archived (not deleted), referenced in changelog.
    Upgrade path from AEE-MINI ``1.0.1`` → AEE ``2.0.0 --profile mini``:
    fresh install, not in-place.

Acceptance criteria (§21.A item 8, line 7855):
    ``aee --version`` returns ``2.0.0`` (not ``1.0.1``); changelog
    references ADR-009.

Design contract (this slice):

1. **Canonical version source.** :data:`AEE_PRODUCT_VERSION` is the
   unified product version string. It is read from
   :data:`aee.__version__` — there is **no** parallel hard-coded literal
   in this package. Bump the version in one place
   (``aee/__init__.py``); every consumer follows.
2. **AEE-MINI line is frozen.** :data:`AEE_MINI_LAST_VERSION` is the
   last version of the AEE-MINI line (``"1.0.1"``). It is archived
   (not deleted). The changelog entry references it explicitly so an
   operator migrating from AEE-MINI sees the upgrade path.
3. **SemVer policy is encoded.** :data:`SEMVER_POLICY` is a human-
   readable string describing MAJOR/MINOR/PATCH cadence, sourced from
   §21.8 line 7796. Tests assert the literal so a future drift in the
   policy is caught.
4. **Upgrade path is fresh install.** :data:`UPGRADE_PATH` documents
   that AEE-MINI ``1.0.1`` → AEE ``2.0.0 --profile mini`` is a fresh
   install, not an in-place upgrade (§21.8 line 7796, §21.R R4
   mitigation).
5. **Release artifacts are declarative.** :data:`RELEASE_ARTIFACTS`
   is the tuple of release artifact descriptors (Docker image,
   tarball, changelog). No artifact is actually built, published, or
   pushed in this slice — §21.8 acceptance criterion is the *strategy*
   and *version*, not a release event. Safety default: dry-run /
   plan-first.
6. **ADR-009 is the canonical reference.** :data:`ADR_REFERENCE`
   points to ADR-009 in the Master Plan; the changelog entry text
   includes this reference (§21.A item 8: "changelog references
   ADR-009").
7. **No side effects.** This package performs **no** ``subprocess``,
   no filesystem writes (other than via the explicit
   :func:`render_changelog` returning a string), no registry push, no
   channel mutation, no Docker build. It is a pure data module. The
   Master Plan §21.8 release event is a separately authorizable
   follow-up; this slice ships the *strategy surface*, not the event.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee98_release_strategy -v``
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from aee import __version__ as _AEE_VERSION


# ---------------------------------------------------------------------------
# Canonical release-strategy data (§21.8)
# ---------------------------------------------------------------------------

#: The unified AEE product version. Source of truth is ``aee.__version__``;
#: this attribute is re-exported here so release-strategy consumers
#: (CLI ``--version``, changelog renderer, tests) all read one place.
#:
#: Per §21.8: ``2.0.0-rc1`` on first Epic 9 release; ``2.0.0`` GA when
#: §21.10 completes. SemVer: MAJOR per Epic / MINOR per sub-section /
#: PATCH per bugfix.
AEE_PRODUCT_VERSION: str = _AEE_VERSION

#: The last version of the AEE-MINI line (§21.8 line 7796). Archived, not
#: deleted. Referenced in the changelog entry so the upgrade path is
#: discoverable from the release notes.
AEE_MINI_LAST_VERSION: str = "1.0.1"

#: The SemVer cadence policy (§21.8 line 7796). Tests assert this literal
#: so a future drift in the versioning policy is caught.
SEMVER_POLICY: str = (
    "MAJOR per Epic / MINOR per sub-section / PATCH per bugfix"
)

#: The upgrade path from AEE-MINI to AEE (§21.8 line 7796, §21.R R4
#: mitigation). Fresh install, not in-place.
UPGRADE_PATH: str = (
    "AEE-MINI 1.0.1 -> AEE 2.0.0 --profile mini: fresh install, "
    "not in-place"
)

#: The ADR reference for the unification decision (§21.A item 8:
#: "changelog references ADR-009"). This is the canonical architecture
#: reference; the changelog entry text includes this string.
ADR_REFERENCE: str = "ADR-009 (§9 of Master Plan)"

#: Release artifact descriptors (§21.8 line 7796). Declarative — no
#: artifact is built, published, or pushed in this slice. Safety
#: default: dry-run / plan-first.
@dataclass(frozen=True)
class ReleaseArtifact:
    """A single release artifact descriptor (§21.8).

    The descriptor is declarative: it names the artifact type, the
    canonical name template, and whether it is a publish-side action
    (``is_publish``). No artifact is actually produced by this package.
    """

    artifact_type: str
    name_template: str
    description: str
    is_publish: bool = False


#: The three release artifacts mandated by §21.8 line 7796: one Docker
#: image, one tarball, one changelog entry.
RELEASE_ARTIFACTS: Tuple[ReleaseArtifact, ...] = (
    ReleaseArtifact(
        artifact_type="docker_image",
        name_template="aee:{version}",
        description=(
            "Unified Docker image. One image, one codebase (§21.5). "
            "Profile is selected at run time via --profile, not at "
            "build time."
        ),
        is_publish=True,
    ),
    ReleaseArtifact(
        artifact_type="tarball",
        name_template="aee-{version}.tar.gz",
        description=(
            "Unified source/binary tarball. One tarball per release."
        ),
        is_publish=True,
    ),
    ReleaseArtifact(
        artifact_type="changelog",
        name_template="CHANGELOG.md",
        description=(
            "Unified changelog. One entry per release. References "
            "ADR-009 (§21.A item 8)."
        ),
        is_publish=False,
    ),
)


# ---------------------------------------------------------------------------
# Changelog rendering (pure function; no side effects)
# ---------------------------------------------------------------------------

def render_changelog_entry(
    *,
    version: str = AEE_PRODUCT_VERSION,
    mini_last: str = AEE_MINI_LAST_VERSION,
    adr_ref: str = ADR_REFERENCE,
    semver_policy: str = SEMVER_POLICY,
    upgrade_path: str = UPGRADE_PATH,
) -> str:
    """Render the §21.8 changelog entry as a markdown string.

    This is a **pure function**. It performs no filesystem writes, no
    subprocess calls, no network operations. The caller decides what
    to do with the returned string (write to ``CHANGELOG.md``, emit to
    stdout, feed to a test assertion).

    The entry references ADR-009 (§21.A item 8 acceptance criterion),
    documents the AEE-MINI ``1.0.1`` archive and the fresh-install
    upgrade path (§21.8 line 7796, §21.R R4 mitigation).

    Args:
        version: The unified AEE product version for this entry.
        mini_last: The last AEE-MINI line version (archived).
        adr_ref: The ADR reference string.
        semver_policy: The SemVer cadence policy string.
        upgrade_path: The upgrade-path description string.

    Returns:
        The changelog entry as a markdown string.
    """
    lines: List[str] = [
        "## [{v}] — Epic 9 Architecture Unification".format(v=version),
        "",
        "**Architecture decision:** {adr} — AEE 2.0 Architecture "
        "Unification (Single Product + Profile Strategy).".format(
            adr=adr_ref
        ),
        "",
        "**SemVer policy:** {policy}".format(policy=semver_policy),
        "",
        "**Release artifacts:**",
        "- Docker image: `aee:{v}` (one image, profile at run time)".format(
            v=version
        ),
        "- Tarball: `aee-{v}.tar.gz`".format(v=version),
        "- Changelog: this file (references {adr})".format(adr=adr_ref),
        "",
        "**AEE-MINI line:** `{mini}` is the last release of the "
        "AEE-MINI line. Archived (not deleted). Referenced here for "
        "migration discoverability.".format(mini=mini_last),
        "",
        "**Upgrade path:** {upgrade}".format(upgrade=upgrade_path),
        "",
        "**Status:** Release strategy shipped (§21.8). Documentation "
        "migration (§21.9) and deprecation plan (§21.10) are "
        "separately authorizable follow-ups. No production release, "
        "registry push, or channel mutation is performed by this "
        "slice — dry-run / plan-first per §21.8 safety default.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dry-run / plan-first release-manifest builder (no side effects)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReleasePlan:
    """A declarative release plan (§21.8 dry-run / plan-first).

    The plan is a **data object**. It describes what a §21.8 release
    event *would* do, without performing any of it. The
    :func:`build_release_plan` factory is the only constructor; it
    reads the canonical version + artifact descriptors and returns a
    frozen plan. No field of the plan is a callable — there is no
    ``execute()`` method, because the §21.8 release event is a
    separately authorizable follow-up.
    """

    version: str
    semver_policy: str
    upgrade_path: str
    adr_reference: str
    mini_last_version: str
    artifacts: Tuple[ReleaseArtifact, ...]
    changelog_entry: str
    is_dry_run: bool
    will_publish: bool
    will_push_registry: bool
    will_mutate_channels: bool


def build_release_plan(
    *,
    version: str = AEE_PRODUCT_VERSION,
    is_dry_run: bool = True,
) -> ReleasePlan:
    """Build a declarative §21.8 release plan (dry-run / plan-first).

    Safety default: ``is_dry_run=True``. The plan's ``will_publish``,
    ``will_push_registry``, and ``will_mutate_channels`` fields are
    always ``False`` in this slice — the §21.8 release event is a
    separately authorizable follow-up. This function performs **no**
    side effects.

    Args:
        version: The unified AEE product version.
        is_dry_run: Whether the plan is a dry-run (default: True).

    Returns:
        A frozen :class:`ReleasePlan`.
    """
    return ReleasePlan(
        version=version,
        semver_policy=SEMVER_POLICY,
        upgrade_path=UPGRADE_PATH,
        adr_reference=ADR_REFERENCE,
        mini_last_version=AEE_MINI_LAST_VERSION,
        artifacts=RELEASE_ARTIFACTS,
        changelog_entry=render_changelog_entry(version=version),
        is_dry_run=is_dry_run,
        will_publish=False,
        will_push_registry=False,
        will_mutate_channels=False,
    )


__all__ = [
    "AEE_PRODUCT_VERSION",
    "AEE_MINI_LAST_VERSION",
    "SEMVER_POLICY",
    "UPGRADE_PATH",
    "ADR_REFERENCE",
    "RELEASE_ARTIFACTS",
    "ReleaseArtifact",
    "ReleasePlan",
    "render_changelog_entry",
    "build_release_plan",
]