"""Scheduler reconnaissance: list cron jobs, supervisord programs,
systemd units on the host.

Used by research tasks to give the orchestrator a real picture of what
is scheduled, and to inform new scheduling decisions.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List


def list_hermes_cronjobs() -> List[Dict[str, Any]]:
    """Return all Hermes cron jobs via `hermes cron list` (JSON-ish).

    Hermes cron job IDs are 12-char hex strings (e.g. `381d62ce7f5e`).
    """
    try:
        out = subprocess.check_output(
            ["hermes", "cron", "list"], stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    jobs: List[Dict[str, Any]] = []
    seen: set = set()
    for line in out.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue
        # Hermes cron IDs are 12-char hex (lowercase).
        m = re.search(r"\b([0-9a-f]{12})\b", line_stripped)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            # Try to extract the human-friendly name (e.g. "Name: morning-brief-...")
            name_m = re.search(r"Name:\s+(\S+)", line_stripped)
            schedule_m = re.search(r"Schedule:\s+(\S+)", line_stripped)
            jobs.append({
                "id": m.group(1),
                "name": name_m.group(1) if name_m else None,
                "schedule": schedule_m.group(1) if schedule_m else None,
                "raw": line_stripped,
            })
    return jobs


def list_supervisord_programs(sock: str = "/tmp/supervisor.sock") -> List[Dict[str, Any]]:
    """List supervisord programs (via supervisorctl) — returns name + state."""
    try:
        out = subprocess.check_output(
            [
                "supervisorctl", f"--serverurl=unix://{sock}", "status",
            ],
            stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    programs: List[Dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "<name>      RUNNING   pid 1234, uptime ..."
        parts = line.split()
        if len(parts) >= 2:
            programs.append({"name": parts[0], "state": parts[1], "raw": line})
    return programs


def list_systemd_units() -> List[Dict[str, Any]]:
    """List user systemd units (Abacus host has no user systemd; returns [])."""
    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "list-units", "--type=service", "--no-pager", "--no-legend"],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    units: List[Dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split()
        if parts:
            units.append({"unit": parts[0], "raw": line})
    return units


def snapshot() -> Dict[str, Any]:
    """One-shot snapshot: cron jobs + supervisord programs + systemd units."""
    return {
        "hermes_cronjobs": list_hermes_cronjobs(),
        "supervisord_programs": list_supervisord_programs(),
        "systemd_units": list_systemd_units(),
    }


def as_json(snapshot_data: Dict[str, Any]) -> str:
    return json.dumps(snapshot_data, indent=2, ensure_ascii=False)
