"""Cloudflare Tunnel configuration.

Reads the tunnel token from the CLOUDFLARE_TUNNEL_TOKEN environment
variable.  Never hardcode credentials in source files — the previous
revision of this file contained a redaction placeholder (``TOKEN=***``)
which was invalid Python and risked being replaced with a real token.

The real tunnel credentials live outside this repo in
``~/.cloudflared/<tunnel-id>.json`` (managed by cloudflared itself).
This module provides an env-var-based accessor for any code that needs
to reference the token programmatically.
"""
import os

TOKEN_ENV_VAR = "CLOUDFLARE_TUNNEL_TOKEN"


def get_token():
    """Return the Cloudflare tunnel token from the environment, or empty string."""
    return os.environ.get(TOKEN_ENV_VAR, "")


TOKEN = get_token()
