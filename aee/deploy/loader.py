"""AEE Epic 9.6 §21.6.B — Host Capability Document loader.

Loads a YAML Host Capability Document into a
:class:`HostCapabilities` instance. The loader is intentionally
stdlib-only: it parses a minimal YAML subset (the §21.6.B shape —
nested mappings + scalar values + flow-style inline mappings) so the
deployment contract does not depend on PyYAML being installed.

If PyYAML is available, the loader uses it; otherwise it falls back
to the stdlib mini-parser. This keeps the deployment contract
self-contained on hosts without PyYAML (e.g. a fresh Docker image
before ``pip install -r requirements.txt``).

Design invariants:

1. The loader does **not** branch on ``provider_hint`` (per
   §21.6.B last paragraph).
2. The loader does **not** validate the document — that is
   :func:`aee.deploy.validate_capabilities`'s job. The loader returns
   a :class:`HostCapabilities` instance; the caller validates.
3. The loader is read-only: it does not write to disk or mutate the
   document.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from aee.deploy.contract import HostCapabilities


# ---------------------------------------------------------------------------
# Optional PyYAML fast path
# ---------------------------------------------------------------------------


def _try_pyyaml(text: str) -> Optional[Dict[str, Any]]:
    """Try to parse YAML using PyYAML if it is available.

    Returns the parsed dict or None if PyYAML is not installed.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return None
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError(
            "host capability document must be a YAML mapping at the top level"
        )
    return parsed


# ---------------------------------------------------------------------------
# Stdlib mini-parser (handles the §21.6.B document shape)
# ---------------------------------------------------------------------------


def _parse_scalar(value: str) -> Any:
    """Parse a YAML scalar string to a Python value.

    Handles the §21.6.B scalar shapes: bool, int, null, quoted
    strings, and bare strings. Does NOT handle YAML anchors, aliases,
    multi-document streams, or block scalars — the §21.6.B document
    shape does not use them.
    """
    v = value.strip()
    # Remove inline comments (only when not inside quotes).
    if v.startswith('"') or v.startswith("'"):
        # Quoted string — find the matching close quote.
        quote = v[0]
        end = v.find(quote, 1)
        if end == -1:
            return v[1:]  # unterminated; return the inner text
        return v[1:end]
    # Strip trailing inline comments (e.g. "value  # comment").
    # Only do this if there's a space before the # to avoid breaking
    # URLs / paths that contain #.
    if " #" in v:
        v = v.split(" #", 1)[0].strip()
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    if v.lower() in ("null", "~", ""):
        return None
    # int
    try:
        return int(v)
    except ValueError:
        pass
    # float
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _parse_flow_mapping(text: str) -> Dict[str, Any]:
    """Parse a flow-style inline mapping like `{ cpu: 2, mem_mb: 4096 }`."""
    text = text.strip()
    if not text.startswith("{") or not text.endswith("}"):
        # Not a flow mapping; treat as scalar and return a single-key
        # dict so the caller can still consume it.
        return {"_value": _parse_scalar(text)}
    inner = text[1:-1].strip()
    out: Dict[str, Any] = {}
    if not inner:
        return out
    # Split on commas not inside braces.
    parts = []
    depth = 0
    buf = ""
    for ch in inner:
        if ch == "{":
            depth += 1
            buf += ch
        elif ch == "}":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    for part in parts:
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[k.strip()] = _parse_scalar(v)
    return out


def _parse_block(text: str) -> Dict[str, Any]:
    """Parse a minimal YAML block mapping into a nested dict.

    Supports the §21.6.B document shape: nested mappings via
    indentation, scalar values, flow-style inline mappings
    (``{ cpu: 2 }``), and list values (``- item``). Does NOT support
    YAML anchors, aliases, multi-document streams, or block scalars
    — the §21.6.B document shape does not use them.

    The parser is line-oriented and uses indentation to determine
    nesting. This is sufficient for the §21.6.B shape, which uses
    2-space indentation consistently.
    """
    lines = text.splitlines()
    # Strip comments and blank lines, but keep indentation.
    cleaned: list[tuple[int, str]] = []
    for ln in lines:
        # Strip full-line comments.
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        if not stripped:
            continue
        # Strip inline comments (only when there's a space before #).
        if " #" in ln:
            ln = ln.split(" #", 1)[0].rstrip()
        indent = len(ln) - len(ln.lstrip(" "))
        cleaned.append((indent, ln.strip()))
    return _parse_block_lines(cleaned, 0, 0)[0]


def _parse_block_lines(
    lines: list[tuple[int, str]], start: int, parent_indent: int
) -> tuple[Dict[str, Any], int]:
    """Parse a block of lines starting at ``start`` with
    ``parent_indent`` as the indentation of the parent mapping.

    Returns the parsed dict and the index of the next line that is
    not part of this block.
    """
    out: Dict[str, Any] = {}
    i = start
    while i < len(lines):
        indent, content = lines[i]
        if indent < parent_indent:
            # End of this block.
            return out, i
        if indent > parent_indent:
            # Should not happen at the top of a block; skip.
            i += 1
            continue
        # indent == parent_indent: a key at this level.
        if ":" not in content:
            i += 1
            continue
        key, value = content.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value:
            # Scalar or flow mapping value.
            if value.startswith("{"):
                out[key] = _parse_flow_mapping(value)
            elif value.startswith("["):
                # Flow sequence — split on commas.
                inner = value[1:-1].strip()
                if not inner:
                    out[key] = []
                else:
                    out[key] = [_parse_scalar(p.strip()) for p in inner.split(",")]
            else:
                out[key] = _parse_scalar(value)
            i += 1
        else:
            # Nested block or list.
            # Look ahead: find the next line with greater indent.
            if i + 1 < len(lines) and lines[i + 1][0] > indent:
                next_indent, next_content = lines[i + 1]
                if next_content.startswith("- "):
                    # List block.
                    items = []
                    j = i + 1
                    while j < len(lines) and lines[j][0] == next_indent:
                        item_content = lines[j][1].strip()
                        if not item_content.startswith("- "):
                            break
                        item_value = item_content[2:].strip()
                        items.append(_parse_scalar(item_value))
                        j += 1
                    out[key] = items
                    i = j
                else:
                    # Nested mapping.
                    nested, j = _parse_block_lines(lines, i + 1, next_indent)
                    out[key] = nested
                    i = j
            else:
                # Empty value with no nested block — null.
                out[key] = None
                i += 1
    return out, i


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_host_capabilities(path: str) -> HostCapabilities:
    """Load a Host Capability Document from a YAML file.

    The loader is stdlib-only (with an optional PyYAML fast path).
    It does not validate the document; the caller should call
    :func:`aee.deploy.validate_capabilities` afterwards.

    Per §21.6.B the ``provider_hint`` field is informational only and
    is preserved on the returned document without branching on it.
    """
    text = Path(path).read_text(encoding="utf-8")
    parsed = _try_pyyaml(text)
    if parsed is None:
        parsed = _parse_block(text)
    return _dict_to_host_capabilities(parsed, source=str(path))


def _dict_to_host_capabilities(
    data: Mapping[str, Any], source: str = "declared"
) -> HostCapabilities:
    """Convert a parsed dict to a :class:`HostCapabilities`.

    The dict shape matches the §21.6.B YAML document:

        host: {...}
        runtime_profile: {...}
        upstream_llm: {...}

    Unknown keys are ignored (forward compatibility).
    """
    host = data.get("host") or {}
    runtime_profile = data.get("runtime_profile") or {}
    upstream_llm = data.get("upstream_llm") or {}
    # Persistent paths may be a list of strings.
    persistent = host.get("persistent_paths") or []
    if isinstance(persistent, str):
        persistent = [persistent]
    # runtime_profile.supported may be a list or a comma-separated
    # string.
    supported = runtime_profile.get("supported") or ()
    if isinstance(supported, str):
        supported = tuple(s.strip() for s in supported.split(",") if s.strip())
    else:
        supported = tuple(supported)
    resource_floor = runtime_profile.get("resource_floor") or {}
    # Normalize resource_floor values to ints.
    normalized_floor: Dict[str, Dict[str, int]] = {}
    for profile_name, vals in resource_floor.items():
        if isinstance(vals, dict):
            normalized_floor[profile_name] = {
                k: int(v) for k, v in vals.items() if isinstance(v, (int, float, str))
            }
    return HostCapabilities(
        name=str(host.get("name", "")),
        class_=str(host.get("class", "")),
        os=str(host.get("os", "")),
        arch=str(host.get("arch", "")),
        python=str(host.get("python", "")),
        filesystem=str(host.get("filesystem", "posix")),
        supervisor=str(host.get("supervisor", "none")),
        network_egress=str(host.get("network_egress", "none")),
        tunnel_kind=str(host.get("tunnel_kind", "none")),
        inbound_allowed=bool(host.get("inbound_allowed", False)),
        db_path_writable=bool(host.get("db_path_writable", True)),
        tempdir_writable=bool(host.get("tempdir_writable", True)),
        persistent_paths=tuple(str(p) for p in persistent),
        provider_hint=str(host.get("provider_hint", "")),
        runtime_profile_supported=supported,
        runtime_profile_default=str(runtime_profile.get("default", "full")),
        runtime_profile_resource_floor=normalized_floor,
        upstream_llm_reachable=bool(upstream_llm.get("reachable", False)),
        upstream_llm_endpoint_kind=str(upstream_llm.get("endpoint_kind", "openai-compatible")),
        detected=False,
        source=source,
    )


__all__ = ["load_host_capabilities"]