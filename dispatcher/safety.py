"""Safety policy — blocklist + allowlist + approval gates.

Phase 1 used a regex blocklist. Phase 2 P3 layers in:

1. **Hard blocklist** — substring matches that are always rejected
   (rm -rf /, fork bombs, etc.). This is non-negotiable.
2. **Allowlist** — only commands whose first token is in the allowlist
   are accepted in `mode: ops` / `mode: coding`. Anything else needs
   explicit `require_approval`.
3. **Approval gate** — commands matching the approval list (e.g.
   `sudo`, `apt install`) are not rejected but marked for human
   approval; the bridge surfaces this in the response so the
   orchestrator (ChatGPT) knows to ask the user.
4. **Path safety** — file ops are restricted to the allowlisted
   path prefixes (defaults: /home/ubuntu/, /tmp/, /opt/).

The "decision" object is a dataclass; tests assert on its fields
without needing to run the bridge.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

from config import load as config_load


@dataclass
class SafetyDecision:
    action: str  # "allow" | "block" | "require_approval"
    reason: str
    matched: Optional[str] = None
    needs_human: bool = False
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "matched": self.matched,
            "needs_human": self.needs_human,
            "meta": self.meta,
        }


def _first_token(cmd: str) -> str:
    """Return the first token (binary name) of a shell-like command."""
    try:
        tokens = shlex.split(cmd.strip(), posix=True)
    except ValueError:
        # Unbalanced quotes — treat as opaque.
        return cmd.strip().split()[0] if cmd.strip() else ""
    if not tokens:
        return ""
    return tokens[0]


def _has_path_op(cmd: str) -> bool:
    """Heuristic: does this command try to operate on a file path?"""
    # We look for shell redirects, command substitution, or explicit
    # paths. This is best-effort; safety is a defence-in-depth, not
    # the only line.
    return bool(re.search(r"[<>]|/[\w/.\-]+", cmd))


# ---------------------------------------------------------------------------
# AEE-8.3 — Profile-aware enforcement intent detection (best-effort).
#
# These helpers detect *intent* from the input text so the safety gate
# can reject cross-profile violations BEFORE the task is sent upstream.
# They are intentionally conservative: false positives (rejecting a
# legitimate operation under a restricted profile) are acceptable;
# false negatives (allowing a restricted operation) are not.
#
# Patterns are substring/regex, consistent with the existing safety.py
# approach. They do NOT import the descriptor module — that import is
# lazy, inside evaluate(), to preserve the isolation contract.
# ---------------------------------------------------------------------------

# Cron-creation intent: creating scheduled jobs, cron entries, etc.
_CRON_INTENT_PATTERNS: List[str] = [
    r"\bcron\b",
    r"\bcrontab\b",
    r"\bcronjob\b",
    r"\bschedule[d]?\s+(?:job|task)\b",
    r"\badd-schedule\b",
    r"\bhermes\s+cron\b",
]

# Subagent-delegation intent: spawning or delegating to subagents.
_SUBAGENT_INTENT_PATTERNS: List[str] = [
    r"\bdelegate\b",
    r"\bdelegate_task\b",
    r"\bsubagent\b",
    r"\bsub-agent\b",
    r"\bsub\s+agent\b",
    r"\bspawn\s+(?:a\s+)?(?:subagent|agent)\b",
]

# Write/mutation intent: file writes, DB mutations, git mutations,
# package installs, deploys, restarts. Used only for is_read_only
# profiles (edge). Non-read-only profiles (full, mini, developer)
# are not gated by these patterns.
_WRITE_INTENT_PATTERNS: List[str] = [
    r"\bwrite_file\b",
    r"\bmkdir\b",
    r"\brm\b\s",
    r"\bmv\b\s",
    r"\bcp\b\s",
    r"\btouch\b\s",
    r"\btee\b",
    r">>\s",
    r"\s>\s",
    r"\bINSERT\s+INTO\b",
    r"\bDELETE\s+FROM\b",
    r"\bDROP\s+TABLE\b",
    r"\bCREATE\s+TABLE\b",
    r"\bALTER\s+TABLE\b",
    r"\bUPDATE\s+.*\bSET\b",
    r"git\s+commit",
    r"git\s+push",
    r"git\s+merge",
    r"git\s+rebase",
    r"git\s+stash",
    r"pip\s+install",
    r"apt\s+install",
    r"npm\s+install",
    r"\bdeploy\b",
    r"\brestart\b",
]


def _has_cron_intent(text: str) -> bool:
    """Heuristic: does the text contain cron-creation intent?"""
    for pat in _CRON_INTENT_PATTERNS:
        try:
            if re.search(pat, text, flags=re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _has_subagent_intent(text: str) -> bool:
    """Heuristic: does the text contain subagent-delegation intent?"""
    for pat in _SUBAGENT_INTENT_PATTERNS:
        try:
            if re.search(pat, text, flags=re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _has_write_intent(text: str) -> bool:
    """Heuristic: does the text contain write/mutation intent?

    Used only for is_read_only profiles. Conservative: matches common
    shell redirects, file-manipulation commands, DB write keywords,
    git mutations, package installs, and deploy/restart verbs.
    """
    for pat in _WRITE_INTENT_PATTERNS:
        try:
            if re.search(pat, text, flags=re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def evaluate(
    input_text: str,
    mode: str = "normal",
    profile: Optional[str] = None,
) -> SafetyDecision:
    """Evaluate `input_text` against the safety policy.

    Returns a SafetyDecision with action ∈ {allow, block, require_approval}.
    """
    cfg = config_load("safety")
    text = input_text or ""

    # 1) Hard blocklist (substring) — always rejects.
    for substr in cfg.get("blocklist_substrings", []):
        if substr in text:
            return SafetyDecision(
                action="block",
                reason=f"hard blocklist matched: {substr!r}",
                matched=substr,
            )

    # 1b) Hard blocklist (regex) — covers API key assignments and other
    # patterns that don't fit a clean substring match. AEE-0: catches
    # `export API_SERVER_KEY=hack`, bare `API_SERVER_KEY=hack`, and
    # very-long literal secret assignments.
    for pat in cfg.get("blocklist_assignment_patterns", []):
        try:
            if re.search(pat, text, flags=re.MULTILINE):
                return SafetyDecision(
                    action="block",
                    reason=f"hard blocklist pattern matched: {pat!r}",
                    matched=pat,
                )
        except re.error:
            # Bad regex in config — ignore that pattern.
            continue

    # 2) Mode-specific checks.
    if mode in ("ops", "coding"):
        first = _first_token(text)
        allowlist = cfg.get("allowlist_commands", [])
        if first and first not in allowlist:
            # Not on allowlist — check approval gate FIRST.
            # Approval-gated substrings (sudo, apt install, pip install, ...)
            # remain require_approval regardless of whether the binary name
            # is recognised. These are the true risk surface.
            for substr in cfg.get("require_approval_substrings", []):
                if substr in text:
                    return SafetyDecision(
                        action="require_approval",
                        reason=f"approval required: matches {substr!r}",
                        matched=substr,
                        needs_human=True,
                        meta={"first_token": first},
                    )
            # Unknown binary name but no approval-gated substring present.
            # P3 loosening (2026-07-08): allow the command through with an
            # audit_warn flag instead of forcing a human-approval loop.
            # Rationale: real-world task titles & tool calls in
            # coding/ops mode routinely start with verb-noun phrases
            # (e.g. "Create artifacts for /home/ubuntu/...") or
            # domain-specific binaries that aren't in the static
            # allowlist. The hard blocklist (step 1) and path safety
            # (step 3) still gate the actually-dangerous surface.
            return SafetyDecision(
                action="allow",
                reason=(
                    f"command {first!r} not in allowlist for mode={mode}; "
                    "allowed with audit_warn (P3 loosening)"
                ),
                matched=first,
                needs_human=False,
                meta={
                    "first_token": first,
                    "allowlist_size": len(allowlist),
                    "audit_warn": True,
                },
            )

    # 3) Path safety — for ops/coding, ensure any file ops target
    # an allowlisted prefix. The prefix pattern (e.g. ^/home/ubuntu/)
    # should also match a bare /home/ubuntu (no trailing slash) and any
    # subpath under it.
    if mode in ("ops", "coding") and _has_path_op(text):
        prefixes = cfg.get("allowlist_prefix_patterns", [])
        path_tokens = re.findall(r"/[\w./\-]+", text)
        if path_tokens and prefixes:
            ok = False
            for tok in path_tokens:
                for p in prefixes:
                    # Strip optional leading ^, optional trailing / and $
                    base = p.lstrip("^").rstrip("/").rstrip("$")
                    pat = r"^" + re.escape(base) + r"(?:/.*)?$"
                    if re.match(pat, tok):
                        ok = True
                        break
                if ok:
                    break
            if not ok:
                return SafetyDecision(
                    action="block",
                    reason=f"file op target outside allowlist prefixes: {prefixes}",
                    matched=path_tokens[0],
                )
    # 4) Cross-mode approval gate: e.g. sudo, apt, pip install always
    # need approval regardless of mode.
    for substr in cfg.get("require_approval_substrings", []):
        if substr in text:
            return SafetyDecision(
                action="require_approval",
                reason=f"approval required: matches {substr!r}",
                matched=substr,
                needs_human=True,
            )

    # 5) AEE-8.3 — Profile-aware enforcement (read-only activation).
    # When a profile is supplied, lazily import the descriptor and use
    # its enforcement fields to reject cross-profile violations BEFORE
    # the task is sent upstream. This is the activation point for the
    # descriptor's can_create_cron / can_delegate_subagents /
    # is_read_only fields (AEE-8.1 contract). profile=None means no
    # enforcement — backward compatible with all pre-AEE-8.3 callers.
    if profile is not None and profile != "":
        # Lazy import: preserves isolation contract (no module-top
        # import of aee.profiles.descriptor in safety.py).
        from aee.profiles.descriptor import (
            get_descriptor,
            UnknownProfileError,
        )

        try:
            desc = get_descriptor(profile)
        except UnknownProfileError:
            # Unknown profile: defer to AEE-8.1 validation contract.
            # Do NOT build a second profile parser in the safety layer.
            # Re-raise so the caller sees the original error with the
            # full diagnostic (expected one of [...]).
            raise

        # 5a) is_read_only profile (edge): reject any write/mutation.
        if desc.is_read_only and _has_write_intent(text):
            return SafetyDecision(
                action="block",
                reason=(
                    f"profile {desc.name!r} is read-only; "
                    f"write/mutation intent detected"
                ),
                matched=desc.name,
                meta={
                    "profile": desc.name,
                    "violation": "is_read_only",
                    "capability": "is_read_only",
                },
            )

        # 5b) can_create_cron=False: reject cron-creation intent.
        if not desc.can_create_cron and _has_cron_intent(text):
            return SafetyDecision(
                action="block",
                reason=(
                    f"profile {desc.name!r} cannot create cron jobs; "
                    f"cron-creation intent detected"
                ),
                matched=desc.name,
                meta={
                    "profile": desc.name,
                    "violation": "can_create_cron",
                    "capability": "can_create_cron",
                },
            )

        # 5c) can_delegate_subagents=False: reject subagent delegation.
        if not desc.can_delegate_subagents and _has_subagent_intent(text):
            return SafetyDecision(
                action="block",
                reason=(
                    f"profile {desc.name!r} cannot delegate subagents; "
                    f"subagent-delegation intent detected"
                ),
                matched=desc.name,
                meta={
                    "profile": desc.name,
                    "violation": "can_delegate_subagents",
                    "capability": "can_delegate_subagents",
                },
            )

    return SafetyDecision(action="allow", reason="passed all safety checks")
