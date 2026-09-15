"""Read the OAuth access token Claude Code keeps in ~/.claude/.credentials.json.

climit never refreshes or writes credentials. Claude Code owns the login, and a
second refresher can rotate the refresh token out from under it.
"""
import json
import time

from . import config


class AuthError(Exception):
    pass


def get_access_token() -> str:
    """Return the stored access token, or raise AuthError if it is missing or expired."""
    try:
        with open(config.CREDS_PATH) as f:
            oauth = json.load(f).get("claudeAiOauth") or {}
    except FileNotFoundError as e:
        raise AuthError(f"not logged in ({config.CREDS_PATH} missing); run claude and /login") from e
    except (OSError, json.JSONDecodeError, AttributeError) as e:
        raise AuthError(f"cannot read credentials: {e}") from e

    token = oauth.get("accessToken")
    if not token:
        raise AuthError("not logged in; run claude and /login")
    if time.time() * 1000 >= int(oauth.get("expiresAt") or 0):
        raise AuthError("access token expired; Claude Code renews it on its next run")
    return token
