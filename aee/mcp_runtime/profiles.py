"""One reviewed Codex profile and one canonical native executable resolver."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType

from .process import Limits, run_bounded
from .sandbox import sandbox_command
from .store import JobError


@dataclass(frozen=True)
class ExecutionProfile:
    name: str = "codex-readonly-high"
    agent: str = "codex"
    model: str = "gpt-6.1-sol"
    reasoning_effort: str = "high"
    sandbox: str = "read-only"
    approval_policy: str = "never"


PROFILES = MappingProxyType({"codex-readonly-high": ExecutionProfile()})


def select_profile(name="codex-readonly-high", model=None, reasoning_effort=None):
    if not isinstance(name, str) or name not in PROFILES:
        raise JobError("INVALID_EXECUTION_PROFILE", "Unsupported execution profile")
    profile = PROFILES[name]
    if model is not None and model != profile.model:
        raise JobError("INVALID_EXECUTION_PROFILE", "Model is not permitted by the selected profile")
    if reasoning_effort is not None and reasoning_effort != profile.reasoning_effort:
        raise JobError("INVALID_EXECUTION_PROFILE", "Reasoning effort is not permitted by the selected profile")
    return profile


@dataclass(frozen=True)
class ExecutorIdentity:
    executable: str
    sha256: str
    version: str


def executable_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if os.fstat(stream.fileno()).st_size > 512 * 1024**2:
            raise JobError("AGENT_UNAVAILABLE", "Executor binary exceeds supported size")
        if stream.read(4) != b"\x7fELF":
            raise JobError("AGENT_UNAVAILABLE", "Pin a native ELF Codex binary; wrappers are not permitted")
        stream.seek(0)
        total = 0
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            total += len(chunk)
            if total > 512 * 1024**2:
                raise JobError("AGENT_UNAVAILABLE", "Executor binary exceeds supported size")
            digest.update(chunk)
    return digest.hexdigest()


def resolve_codex(configured):
    candidate = shutil.which(configured) if not os.path.isabs(configured) else configured
    if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise JobError("AGENT_UNAVAILABLE", "Pinned Codex executable is unavailable")
    path = Path(candidate).resolve()
    try:
        digest = executable_digest(path)
        with tempfile.TemporaryDirectory(prefix="aee-codex-identity-") as temporary:
            workspace = Path(temporary)
            arguments = ["/runner/codex", "--version"]
            limits = Limits(timeout=5, stdout_bytes=4096, stderr_bytes=4096)
            result = run_bounded(sandbox_command(path, workspace, arguments, limits), env={}, limits=limits)
        if result.returncode != 0:
            raise JobError("AGENT_UNAVAILABLE", "Codex identity probe failed inside the required sandbox")
        version = result.stdout.decode(errors="replace").strip()
        if not version.startswith("codex-cli ") or len(version) > 200 or "\n" in version:
            raise JobError("AGENT_UNAVAILABLE", "Unrecognized Codex identity probe")
        if executable_digest(path) != digest:
            raise JobError("AGENT_UNAVAILABLE", "Executor changed during identity verification")
        return ExecutorIdentity(str(path), digest, version)
    except OSError:
        raise JobError("AGENT_UNAVAILABLE", "Codex identity could not be safely resolved") from None


def codex_arguments(profile):
    # User task data is never placed here. '-' is the documented stdin channel.
    if not isinstance(profile, ExecutionProfile) or PROFILES.get(profile.name) != profile:
        raise JobError("INVALID_EXECUTION_PROFILE", "Executor requires an unchanged registered profile")
    return ["/runner/codex", "exec", "--ignore-user-config", "--ignore-rules",
            "--sandbox", profile.sandbox, "--skip-git-repo-check", "--ephemeral",
            "--json", "--color", "never", "--model", profile.model,
            "-c", f'model_reasoning_effort="{profile.reasoning_effort}"',
            "-c", f'approval_policy="{profile.approval_policy}"',
            "-C", "/workspace", "-"]


def execution_metadata(profile, identity, *, requested_profile=None, requested_model=None, requested_reasoning=None):
    return {"requested": {"execution_profile": requested_profile, "model": requested_model,
                          "reasoning_effort": requested_reasoning},
            "configured": {**asdict(profile), "execution_profile": profile.name,
                           "executable": identity.executable, "executable_sha256": identity.sha256},
            "observed": {"agent_version_probe": identity.version, "model": None,
                         "reasoning_effort": None, "sandbox": None, "approval_policy": None},
            "isolation": {"backend": "bubblewrap", "network": "disabled",
                          "workspace": "/workspace", "host_credentials_mounted": False}}
