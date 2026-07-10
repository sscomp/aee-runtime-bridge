"""Progress reporting helper.

Valid pcts: 0, 5, 10, 25, 40, 60, 80, 95, 100. Progress is monotonic; a
regress attempt raises ValueError (caller may decide to ignore).
"""
from __future__ import annotations

from typing import Optional

from .models import is_legal_progress, LEGAL_PROGRESS_PCTS


def next_pct_hint(current_pct: int, elapsed_sec: float, timeout_sec: int, has_output: bool) -> int:
    """Heuristic for the auto-pct updater (called by the watcher loop).

    Returns the suggested new pct, or -1 to indicate 'no change recommended'.
    The caller should not call this when current_pct is already 100.
    """
    if current_pct >= 100:
        return 100
    # Time-based: if we've used N% of the budget, jump to the bucket >= N%.
    if timeout_sec > 0:
        ratio = elapsed_sec / float(timeout_sec)
    else:
        ratio = 0.5
    if ratio >= 0.95:
        return 100 if has_output else 95
    if ratio >= 0.80:
        return 95 if has_output else 80
    if ratio >= 0.60:
        return 80 if has_output else 60
    if ratio >= 0.40:
        return 60 if has_output else 40
    if ratio >= 0.20:
        return 40 if has_output else 25
    if ratio >= 0.05:
        return 25 if has_output else 10
    return 10 if has_output else 5


def validate_progress(pct: int) -> None:
    if not is_legal_progress(pct):
        raise ValueError(
            f"progress_pct must be one of {LEGAL_PROGRESS_PCTS}, got {pct}"
        )


def monotonic(old: int, new: int) -> Optional[str]:
    """Return an error message if `new` regresses below `old`, else None."""
    if new < old:
        return f"progress regression: {old} -> {new}"
    return None
