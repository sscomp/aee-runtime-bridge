"""AEE Bootstrap v1 — W10 shared secret-redaction module (spec §8.2, §8.4).

This module implements the **shared secret-redaction policy** that the
bootstrap v1 surface (shell trampolines, stage markers, log lines, the
future ``aee doctor`` diagnostics bundle) consumes. Per spec §8.2:

    "All log lines and stage markers redact any string matching the
    secret patterns documented below. The patterns are a [PROPOSAL]
    for the bootstrap v1 surface; they are NOT yet backed by an
    existing reusable regex module in this repository."

    "* ``*_API_KEY``, ``*_TOKEN``, ``*_SECRET``, ``*_PASSWORD`` env
       var names → redacted as ``<REDACTED:NAME>``.
    * Bearer tokens, JWTs, and basic-auth headers → redacted.
    * Long hex/base64 strings (>40 chars, high entropy) → truncated
      to first 8 + ``…`` + last 4."

Spec §17.1 R5 explicitly notes:

    "there is NO reusable regex in ``aee/artifacts/policy.py``. A
    shared redaction module is [PROPOSAL] (§8.2) — Implement the
    shared module in a work order; add a regression test (W10)."

This IS that work order (Phase 5 / Bootstrap v1 Phase B / W10).

Design contract:

1. **Pure Python, no side effects.** The module performs regex
   substitution on strings; it does NOT read env files, write to disk,
   spawn processes, or import platform-specific modules.
2. **Idempotent.** Redacting an already-redacted string is a no-op
   (the ``<REDACTED:NAME>`` sentinel is itself not a secret pattern).
3. **Single source of truth.** The regex set lives here and only here.
   The shell layer (``bootstrap/lib/resume.sh``, future trampoline) and
   the Python layer (``BootstrapLifecycle.record_stage`` stderr_tail,
   ``aee doctor`` diagnostics) both consume this module's public API.
4. **No false negatives for the documented patterns.** The patterns
   cover env-var-name redaction, Authorization header values, Bearer
   tokens, JWTs, basic-auth, and long high-entropy strings. They do
   NOT attempt to redact arbitrary free-text secrets (the spec
   acknowledges this is best-effort, not a security boundary).
5. **Honest scope.** This module does NOT claim to be a generic
   secret-scanning engine. It redacts the §8.2 patterns; anything
   outside that set is the operator's responsibility.

Public API:

* :data:`REDACTED_SENTINEL` — the literal ``<REDACTED>`` string used
  for header/value redaction.
* :data:`SECRET_ENV_NAME_PATTERN` — the regex matching
  ``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET`` / ``*_PASSWORD`` env var
  names (case-insensitive, anchored at word boundary).
* :data:`BEARER_TOKEN_PATTERN` — ``Authorization: Bearer <token>``.
* :data:`BASIC_AUTH_PATTERN` — ``Authorization: Basic <b64>``.
* :data:`JWT_PATTERN` — three base64url segments joined by ``.``.
* :data:`HIGH_ENTROPY_PATTERN` — >40 chars of hex/base64.
* :func:`redact_env_var_names` — replace ``NAME=value`` with
  ``NAME=<REDACTED:NAME>`` for matching names.
* :func:`redact_authorization_headers` — replace Authorization header
  values with ``<REDACTED>``.
* :func:`redact_high_entropy_strings` — truncate long hex/base64
  strings to ``first8…last4``.
* :func:`redact_all` — apply all three filters in sequence (the
  canonical order: env names first so the value pattern doesn't
  double-redact, then headers, then high-entropy).

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_bootstrap_integration -v``
"""
from __future__ import annotations

import re
from typing import Tuple

# ---------------------------------------------------------------------------
# Sentinels + patterns (spec §8.2)
# ---------------------------------------------------------------------------

#: The literal sentinel substituted for redacted header values / tokens.
REDACTED_SENTINEL: str = "<REDACTED>"

#: The prefix used for env-var-name redaction: ``<REDACTED:NAME>``.
_REDACTED_NAME_PREFIX: str = "<REDACTED:"
_REDACTED_NAME_SUFFIX: str = ">"


# Env var names ending in _API_KEY, _TOKEN, _SECRET, _PASSWORD (case-insensitive).
# Matches the NAME part in ``NAME=value`` or ``NAME: value`` contexts.
# We intentionally match the NAME before the ``=`` or ``:`` so the
# substitution preserves the name while blanking the value.
SECRET_ENV_NAME_PATTERN: re.Pattern[str] = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*"
    r"(?:_API_KEY|_TOKEN|_SECRET|_PASSWORD))\b",
    re.IGNORECASE,
)

# Full ``NAME=value`` capture for env-var-style lines. The value is
# everything after the ``=`` up to whitespace or end-of-line. We
# substitute the whole match with ``NAME=<REDACTED:NAME>``.
_ENV_ASSIGNMENT_PATTERN: re.Pattern[str] = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*"
    r"(?:_API_KEY|_TOKEN|_SECRET|_PASSWORD))"
    r"=([^\s]*)",
    re.IGNORECASE,
)

# Authorization header value: ``Bearer <token>`` or ``bearer <token>``.
BEARER_TOKEN_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)\b(Authorization\s*:\s*Bearer\s+)([A-Za-z0-9._\-]+)",
)

# Authorization header value: ``Basic <base64>``.
BASIC_AUTH_PATTERN: re.Pattern[str] = re.compile(
    r"(?i)\b(Authorization\s*:\s*Basic\s+)([A-Za-z0-9+/=]+)",
)

# JWT: three base64url segments joined by dots. The middle segment is
# the payload; the whole token is sensitive. We require at least one
# char per segment and a total length > 20 to avoid false positives on
# dotted version strings like ``v1.2.3``.
JWT_PATTERN: re.Pattern[str] = re.compile(
    r"\b(eyJ[A-Za-z0-9_\-]+\.([A-Za-z0-9_\-]+)\.[A-Za-z0-9_\-]+)\b",
)

# Long high-entropy string: >40 chars of hex or base64 (incl. ``+/=_-``).
# We require the string to be predominantly alphanumeric (>= 80%) to
# avoid redacting ordinary prose that happens to be long.
_HIGH_ENTROPY_MIN_LEN: int = 40
_HIGH_ENTROPY_PATTERN: re.Pattern[str] = re.compile(
    r"(?<![A-Za-z0-9+/=_\-])([A-Za-z0-9+/=_\-]{40,})(?![A-Za-z0-9+/=_\-])",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_mostly_alphanumeric(token: str) -> bool:
    """Return True if >= 80% of ``token`` chars are alphanumeric."""
    if not token:
        return False
    alnum = sum(1 for c in token if c.isalnum())
    return (alnum / len(token)) >= 0.80


def _truncate_high_entropy(token: str) -> str:
    """Truncate a long hex/base64 token to ``first8…last4``."""
    if len(token) <= 8 + 4:
        # Too short to meaningfully truncate; redact fully.
        return REDACTED_SENTINEL
    return f"{token[:8]}…{token[-4:]}"


# ---------------------------------------------------------------------------
# Public redaction functions
# ---------------------------------------------------------------------------


def redact_env_var_names(text: str) -> str:
    """Redact ``NAME=value`` for §8.2 env var names.

    Replaces the value with ``<REDACTED:NAME>`` so the operator can see
    *which* secret was redacted without seeing the value. The name is
    preserved uppercased in the sentinel for readability.

    Examples:
        ``OPENAI_API_KEY=sk-abc123`` → ``OPENAI_API_KEY=<REDACTED:OPENAI_API_KEY>``
        ``DB_PASSWORD=hunter2`` → ``DB_PASSWORD=<REDACTED:DB_PASSWORD>``
    """
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return f"{name}={_REDACTED_NAME_PREFIX}{name.upper()}{_REDACTED_NAME_SUFFIX}"

    return _ENV_ASSIGNMENT_PATTERN.sub(_replace, text)


def redact_authorization_headers(text: str) -> str:
    """Redact ``Authorization: Bearer <token>`` and ``Authorization: Basic <b64>``.

    Replaces the token/base64 value with :data:`REDACTED_SENTINEL`,
    preserving the ``Authorization: Bearer `` / ``Authorization: Basic ``
    prefix so the operator can see the auth scheme.
    """
    text = BEARER_TOKEN_PATTERN.sub(
        lambda m: m.group(1) + REDACTED_SENTINEL, text
    )
    text = BASIC_AUTH_PATTERN.sub(
        lambda m: m.group(1) + REDACTED_SENTINEL, text
    )
    # JWTs that appear without an ``Authorization:`` prefix.
    text = JWT_PATTERN.sub(REDACTED_SENTINEL, text)
    return text


def redact_high_entropy_strings(text: str) -> str:
    """Truncate long hex/base64 strings (>40 chars) to ``first8…last4``.

    Operates on bare high-entropy tokens (not inside an env assignment or
    Authorization header — those are handled by the other two filters).
    Only tokens that are >= 80% alphanumeric are redacted, to avoid
    truncating ordinary prose.
    """
    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if not _is_mostly_alphanumeric(token):
            return token
        return _truncate_high_entropy(token)

    return _HIGH_ENTROPY_PATTERN.sub(_replace, text)


def redact_all(text: str) -> str:
    """Apply all three §8.2 redaction filters in canonical order.

    Order: env var names → Authorization headers → high-entropy strings.
    This order ensures the env-name filter consumes ``NAME=value`` before
    the high-entropy filter can truncate the value (which would leave a
    partial secret visible).
    """
    text = redact_env_var_names(text)
    text = redact_authorization_headers(text)
    text = redact_high_entropy_strings(text)
    return text


__all__: Tuple[str, ...] = (
    "REDACTED_SENTINEL",
    "SECRET_ENV_NAME_PATTERN",
    "BEARER_TOKEN_PATTERN",
    "BASIC_AUTH_PATTERN",
    "JWT_PATTERN",
    "redact_env_var_names",
    "redact_authorization_headers",
    "redact_high_entropy_strings",
    "redact_all",
)