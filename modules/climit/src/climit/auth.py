"""OAuth token handling — reuse Claude Code's credentials, refresh when needed.

Reads ~/.claude/.credentials.json (claudeAiOauth.*). Refreshes via the OAuth
token endpoint only when genuinely near expiry, with a re-read race guard so we
defer to Claude Code if it already rotated the token. Writes back atomically at
mode 0600. Token values are never logged.
"""
import json
import os
import time
import urllib.error
import urllib.request

from . import config


class AuthError(Exception):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_creds() -> dict:
    try:
        with open(config.CREDS_PATH) as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise AuthError(f"credentials not found: {config.CREDS_PATH}") from e
    except (OSError, json.JSONDecodeError) as e:
        raise AuthError(f"cannot read credentials: {e}") from e


def _oauth(creds: dict) -> dict:
    return creds.get("claudeAiOauth") or {}


def _write_creds_atomic(creds: dict) -> None:
    path = config.CREDS_PATH
    tmp = path.with_name(path.name + ".climit.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(creds, f)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _refresh(creds: dict) -> dict:
    """POST the refresh grant, mutate creds['claudeAiOauth'] in place, return creds."""
    oauth = _oauth(creds)
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise AuthError("no refreshToken in credentials")
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(
        config.TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": config.USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise AuthError(f"token refresh failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise AuthError(f"token refresh failed: {e.reason}") from e

    # Standard OAuth returns snake_case; accept camelCase defensively.
    access = data.get("access_token") or data.get("accessToken")
    if not access:
        raise AuthError("refresh response missing access_token")
    oauth["accessToken"] = access
    oauth["refreshToken"] = data.get("refresh_token") or data.get("refreshToken") or refresh_token
    expires_in = data.get("expires_in") or data.get("expiresIn")
    if expires_in:
        oauth["expiresAt"] = _now_ms() + int(expires_in) * 1000
    elif data.get("expires_at") or data.get("expiresAt"):
        oauth["expiresAt"] = int(data.get("expires_at") or data.get("expiresAt"))
    creds["claudeAiOauth"] = oauth
    return creds


def get_access_token(force_refresh: bool = False) -> str:
    """Return a usable access token, refreshing (and persisting) if near expiry."""
    creds = _read_creds()
    oauth = _oauth(creds)
    token = oauth.get("accessToken")
    expires_at = int(oauth.get("expiresAt") or 0)
    near_expiry = _now_ms() >= expires_at - config.PROACTIVE_REFRESH_BUFFER_MS

    if force_refresh or near_expiry:
        # Race guard: re-read; Claude Code may have already rotated the token.
        fresh = _read_creds()
        fresh_oauth = _oauth(fresh)
        if not force_refresh and int(fresh_oauth.get("expiresAt") or 0) > expires_at:
            token = fresh_oauth.get("accessToken")  # someone else refreshed — adopt it
        else:
            creds = _refresh(fresh)
            _write_creds_atomic(creds)
            token = _oauth(creds).get("accessToken")

    if not token:
        raise AuthError("no accessToken available")
    return token
