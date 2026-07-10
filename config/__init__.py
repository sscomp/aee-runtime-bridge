"""Configuration loader for the Task Dispatcher.

Configs live in `<bridge_root>/config/*.json`. Pure stdlib (no PyYAML
dependency) to match the existing bridge style. Environment variables
of the form `DISPATCHER_<UPPER>__<KEY>` override file values at lookup
time (double underscore = nested key).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

_BRIDGE_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = _BRIDGE_ROOT / "config"

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "scheduler.json": {
        "max_retries": 3,
        "queue_max_size": 100,
        "poll_interval_sec": 2.0,
        "watcher_tick_sec": 5.0,
        "auto_cancel_after_sec": 7200,
    },
    "report.json": {
        "enabled": True,
        "output_dir": "reports",
        "sections_order": [
            "Executive Summary",
            "Current Architecture",
            "Current Workflow",
            "Findings",
            "Technical Debt",
            "Optimization",
            "Priority",
            "Roadmap",
            "Appendix",
        ],
        "include_task_json": True,
    },
    "research.json": {
        "default_root": "/home/ubuntu",
        "scan_paths": [
            "/home/ubuntu/hermes-runtime-bridge",
            "/home/ubuntu/macro-report",
        ],
        "skip_dirs": [
            "__pycache__", ".git", ".venv", "venv", "node_modules",
        ],
        "max_files": 5000,
    },
    "model.json": {
        "default_model": "claude-sonnet-4-6",
        "fallback_order": [
            "claude-sonnet-4-6",
            "kimi-k2.6:cloud",
            "gpt-4o",
        ],
    },
    "reaper.json": {
        "enabled": True,
        "stale_running_sec": 1800,
        "stale_queued_sec": 300,
        "max_total_age_sec": 7200,
        "grace_period_sec": 30,
    },
    "notify.json": {
        "telegram": {
            "enabled": False,
            "bot_token_env": "TELEGRAM_BOT_TOKEN",
            "chat_id_env": "TELEGRAM_CHAT_ID",
            "notify_on": ["failed", "timeout"],
            "rate_limit_per_hour": 20,
        },
    },
    "safety.json": {
        # AEE-0: tightened blocklist to cover secret reads, pipe-to-shell,
        # shadow/ssh/credential exposure, fork bomb. require_approval_substrings
        # no longer uses fuzzy '...' placeholders — exact substrings only.
        "mode": "blocklist_plus_allowlist",
        "allowlist_commands": [
            "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "file",
            "echo", "printf", "date", "whoami", "pwd", "env", "printenv",
            "python3", "python", "pip", "uv", "node", "npm", "npx",
            "git", "curl", "wget", "jq", "yq",
            "docker", "kubectl", "helm",
            "supervisorctl", "systemctl", "journalctl",
            "ssh", "scp", "rsync",
            "tar", "gzip", "gunzip", "zip", "unzip",
        ],
        "allowlist_prefix_patterns": [
            r"^/home/ubuntu/",
            r"^/tmp/",
            r"^/opt/",
        ],
        "blocklist_substrings": [
            "rm -rf /", "rm -rf ~", "rm -rf /*",
            "mkfs", "dd if=", "shutdown", "reboot",
            ":(){:|:&};:",
            ":() { :|:& };:",
            ":(){ :|:& };:",
            "passwd ",
            "cat ~/.hermes/.env",
            "cat .hermes/.env",
            ".hermes/.env",
            "cat ~/.ssh",
            "cat /etc/shadow",
            "printenv | grep",
            "env | grep",
        ],
        "blocklist_assignment_patterns": [
            r"^\s*export\s+API_SERVER_KEY\s*=",
            r"^\s*API_SERVER_KEY\s*=",
            r"\bAPI_SERVER_KEY\s*=",
            r"=\s*['\"]?[A-Za-z0-9_\-]{16,}",
        ],
        "require_approval_substrings": [
            "sudo ", "apt install", "apt remove", "pip install",
            " | sh", " | bash", " | sh ", " | bash ",
        ],
        "log_rejected": True,
    },
    "pricing.json": {
        "models": {
            "claude-sonnet-4-6": {"input_per_1m": 3.0, "output_per_1m": 15.0},
            "kimi-k2.6:cloud":     {"input_per_1m": 0.6, "output_per_1m": 0.6},
            "gpt-4o":              {"input_per_1m": 5.0, "output_per_1m": 15.0},
            "gpt-4o-mini":         {"input_per_1m": 0.15, "output_per_1m": 0.60},
            "default":             {"input_per_1m": 3.0, "output_per_1m": 15.0},
        },
    },
}


def config_dir() -> Path:
    return CONFIG_DIR


def ensure_defaults() -> None:
    """Write default config files if they don't exist (idempotent)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in DEFAULTS.items():
        path = CONFIG_DIR / name
        if not path.exists():
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )


def load(name: str) -> Dict[str, Any]:
    """Load a config file by basename (e.g. 'scheduler'). Adds .json if needed."""
    if not name.endswith(".json"):
        name = name + ".json"
    path = CONFIG_DIR / name
    if not path.exists():
        return dict(DEFAULTS.get(name, {}))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS.get(name, {}))
    if not isinstance(data, dict):
        return dict(DEFAULTS.get(name, {}))
    return data


def get(name: str, key: str, default: Any = None) -> Any:
    """Get a single value with env override: DISPATCHER_<UPPER>__<KEY>."""
    env_key = f"DISPATCHER_{name.upper().removesuffix('.JSON')}__" + key.upper()
    if env_key in os.environ:
        return os.environ[env_key]
    data = load(name)
    if key in data:
        return data[key]
    return default
