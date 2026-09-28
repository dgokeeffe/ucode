"""On-disk cache for the OAuth bearer `ug auth-token` prints.

Agents such as pi resolve their API key by running `ug auth-token` before
*every* request, under a hard deadline (pi kills the helper after 10s and
discards its stderr). A cache miss costs a full `ucode.cli` import (~0.7s,
dominated by `databricks.sdk`) plus a `databricks auth token` subprocess that
can stall on network I/O (host-metadata or OIDC refresh). A hit is served by
``ucode._entry`` before any of that is imported.

This module must stay stdlib-only: it runs on the fast path.

Only OAuth tokens minted through the Databricks CLI are cached — never PATs,
``DATABRICKS_BEARER``/``DATABRICKS_BEARER_COMMAND`` values, or custom-client
tokens. Files are ``0600`` inside a ``0700`` directory, the same posture as the
CLI's own ``~/.databricks/token-cache.json``. Entries are served only while the
JWT has at least ``MIN_REMAINING_SECONDS`` left and are at most
``MAX_AGE_SECONDS`` old, so a re-login outside ucode (e.g. switching the
profile's identity) is picked up within that window; ucode's own login path
clears the cache immediately.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".ucode" / "token-cache"
# Refresh through the CLI once the token is this close to expiry.
MIN_REMAINING_SECONDS = 300
# Re-validate through the CLI at least this often, matching ucode's launch-time
# token refresh cadence (TOKEN_REFRESH_INTERVAL_SECONDS in databricks.py).
MAX_AGE_SECONDS = 1800

_BEARER_ENV_VARS = ("DATABRICKS_BEARER", "DATABRICKS_BEARER_COMMAND")


def token_expiry(token: str) -> float | None:
    """Return the JWT ``exp`` claim (epoch seconds), or None if not a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, TypeError, ValueError, binascii.Error, json.JSONDecodeError):
        return None


def cache_applicable() -> bool:
    """False when an env override owns auth; those tokens must never be cached."""
    return not any(os.environ.get(name, "").strip() for name in _BEARER_ENV_VARS)


def _cache_path(host: str, profile: str | None) -> Path:
    key = "\0".join(
        (
            host.strip().rstrip("/").lower(),
            profile or "",
            # A different config file can map the same profile name elsewhere.
            os.environ.get("DATABRICKS_CONFIG_FILE", ""),
        )
    )
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def read(host: str, profile: str | None, *, now: float | None = None) -> str | None:
    """Return a cached token that is still comfortably valid, else None."""
    if not cache_applicable():
        return None
    now = time.time() if now is None else now
    try:
        entry = json.loads(_cache_path(host, profile).read_text(encoding="utf-8"))
        token = entry["token"]
        cached_at = float(entry["cached_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(token, str) or not token:
        return None
    if not 0 <= now - cached_at < MAX_AGE_SECONDS:
        return None
    expires_at = token_expiry(token)
    if expires_at is None or now >= expires_at - MIN_REMAINING_SECONDS:
        return None
    return token


def write(host: str, profile: str | None, token: str) -> None:
    """Best-effort atomic write; a cache failure must never fail the helper."""
    if not cache_applicable() or token_expiry(token) is None:
        return
    path = _cache_path(host, profile)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "cached_at": time.time()}, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def clear() -> None:
    """Drop every cached token (e.g. after a fresh `databricks auth login`)."""
    shutil.rmtree(CACHE_DIR, ignore_errors=True)


def fast_path_token(args: list[str]) -> str | None:
    """Serve ``auth-token --host H [--profile P]`` from the cache, else None.

    Only the exact argv shape ``build_auth_token_argv`` emits for OAuth is
    handled; anything else (``--use-pat``, ``--force-refresh``, custom client
    options, no ``--host``) returns None so the full CLI decides."""
    if not args or args[0] != "auth-token":
        return None
    opts = args[1:]
    if len(opts) not in (2, 4) or len(opts) % 2:
        return None
    parsed = dict(zip(opts[::2], opts[1::2], strict=True))
    if len(parsed) != len(opts) // 2 or not set(parsed) <= {"--host", "--profile"}:
        return None
    host = parsed.get("--host", "").strip()
    if not host:
        return None
    return read(host, parsed.get("--profile") or None)
