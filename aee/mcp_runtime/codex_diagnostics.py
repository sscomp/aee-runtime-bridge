"""Narrow classification of an observed nonfatal native startup diagnostic."""
import re


_IGNORED_NOTIFY = re.compile(
    r"Ignored unsupported project-local config keys in /[^\r\n]+/\.codex/config\.toml: "
    r"notify\. If you want these settings to apply, manually set them in your "
    r"user-level config\.toml\.\Z"
)


def ignored_startup_config_notice(item, *, turn_seen):
    """Do not generalize this to arbitrary error messages or in-turn errors.

    Codex 0.167.2 emits this notice as item.type=error before turn.started,
    even though it explicitly ignores the project setting and runs normally.
    Other error items and missing authentic completion remain fatal.
    """
    return (not turn_seen and item.get('type') == 'error'
            and item.get('status') is None
            and isinstance(item.get('message'), str)
            and _IGNORED_NOTIFY.fullmatch(item['message']) is not None)
