"""AEE-8.1 — Profile descriptor package (read-only).

This package holds the **read-only** profile descriptor plumbing
introduced by AEE-8.1. It can read, parse, validate, and expose
profile descriptors to callers, but it performs no installation,
switching, write-back, migration, or runtime mutation.

Public surface (re-exported from :mod:`aee.profiles.descriptor`):

* :data:`KNOWN_PROFILES`, :data:`DEFAULT_PROFILE`
* :class:`ProfileDescriptor`
* :class:`UnknownProfileError`, :class:`InvalidDescriptorError`
* :func:`is_known_profile`, :func:`parse_profile`
* :func:`get_descriptor`, :func:`safety_tier_for`
* :func:`all_descriptors`

See :mod:`aee.profiles.descriptor` for the full contract.
"""
from __future__ import annotations

from aee.profiles.descriptor import (
    KNOWN_PROFILES,
    DEFAULT_PROFILE,
    ProfileDescriptor,
    UnknownProfileError,
    InvalidDescriptorError,
    is_known_profile,
    parse_profile,
    get_descriptor,
    safety_tier_for,
    all_descriptors,
)

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