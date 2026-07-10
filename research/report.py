"""Unified 9-section report scaffolding.

SOP §5 specifies:
  1. Executive Summary
  2. Current Architecture
  3. Current Workflow
  4. Findings
  5. Technical Debt
  6. Optimization
  7. Priority
  8. Roadmap
  9. Appendix
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_BRIDGE_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = _BRIDGE_ROOT / "reports"

SECTIONS: List[str] = [
    "Executive Summary",
    "Current Architecture",
    "Current Workflow",
    "Findings",
    "Technical Debt",
    "Optimization",
    "Priority",
    "Roadmap",
    "Appendix",
]


def render(
    task_id: str,
    title: str,
    type: str,
    sections: Dict[str, str],
    *,
    model: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Path:
    """Render a report.md using `sections` keyed by section name.

    Missing sections are filled with a "(not provided)" placeholder.
    Returns the path to the written file.
    """
    out_dir = REPORTS_DIR / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: List[str] = [
        f"# {title}",
        "",
        f"- **Task ID**: `{task_id}`",
        f"- **Type**: `{type}`",
        f"- **Generated**: {now}",
    ]
    if model:
        lines.append(f"- **Model**: {model}")
    if extra_metadata:
        for k, v in extra_metadata.items():
            lines.append(f"- **{k}**: {v}")
    lines.append("")
    for i, name in enumerate(SECTIONS, 1):
        body = sections.get(name, "").strip() or "_(not provided)_"
        lines.append(f"## {i}. {name}")
        lines.append("")
        lines.append(body)
        lines.append("")
    out_path = out_dir / "report.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def template_sections() -> Dict[str, str]:
    """Return an empty sections dict (all placeholders)."""
    return {name: "" for name in SECTIONS}
