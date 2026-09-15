"""Two data sources for usage windows, identical output shape.

parse_statusline() — the rate_limits object Claude Code pipes to status line
                     commands (free, fresh after every model response).
fetch_live()       — GET the OAuth usage endpoint (authoritative, sees every
                     client; rate-limited).

Both return windows = { window_key: {"util": float, "resets_at": str|None} }.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config


class FetchError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _parse_windows(util: dict) -> dict:
    """Pick only real burn windows from a usage endpoint payload.

    The payload carries the stable window objects ({utilization, resets_at}), a
    "limits" array, and internal model-codename keys (nimbus_quill, ...) that are
    NOT windows. We keep only config.KNOWN_WINDOW_KEYS (recording a null
    resets_at as-is, so a post-reset zero sample is never dropped), and
    synthesize per-model weekly windows from limits[] (kind == "weekly_scoped").
    """
    out: dict = {}
    if not isinstance(util, dict):
        return out
    for key, val in util.items():
        if key == "limits" and isinstance(val, list):
            out.update(_scoped_windows(val))
            continue
        if key not in config.KNOWN_WINDOW_KEYS:
            continue
        if not (isinstance(val, dict) and "utilization" in val and "resets_at" in val):
            continue
        u = val.get("utilization")
        if u is None:
            continue
        try:
            out[key] = {"util": float(u), "resets_at": val.get("resets_at")}
        except (TypeError, ValueError):
            continue
    return out


def _scoped_windows(limits) -> dict:
    """Synthesize per-model weekly windows from the limits[] array.

    Each weekly_scoped entry names a model via scope.model.display_name and
    carries percent + resets_at. Becomes a regular window key
    "<WEEKLY_SCOPED_PREFIX><slug>" so it flows through store/rates unchanged.
    """
    out: dict = {}
    for entry in limits:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        scope = entry.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        display = model.get("display_name") if isinstance(model, dict) else None
        if not isinstance(display, str) or not display.strip():
            continue
        percent = entry.get("percent")
        if percent is None:
            continue
        try:
            util = float(percent)
        except (TypeError, ValueError):
            continue
        key = config.WEEKLY_SCOPED_PREFIX + config.weekly_scoped_slug(display)
        out[key] = {"util": util, "resets_at": entry.get("resets_at")}
    return out


def parse_statusline(payload) -> dict:
    """Windows from a status line payload's rate_limits (resets_at is epoch seconds there)."""
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    out: dict = {}
    if not isinstance(limits, dict):
        return out
    for key in config.KNOWN_WINDOW_KEYS:
        val = limits.get(key)
        if not isinstance(val, dict):
            continue
        try:
            util = float(val["used_percentage"])
        except (KeyError, TypeError, ValueError):
            continue
        reset = val.get("resets_at")
        if isinstance(reset, (int, float)) and not isinstance(reset, bool):
            reset = datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()
        else:
            reset = None
        out[key] = {"util": util, "resets_at": reset}
    return out


def fetch_live(token: str) -> dict:
    """GET the usage endpoint. Returns windows. Raises FetchError."""
    req = urllib.request.Request(
        config.API_BASE + config.USAGE_PATH,
        method="GET",
        headers={
            # Mirrors Claude Code's fetchUtilization exactly: Bearer + oauth beta +
            # UA + json. Notably NO anthropic-version header on this endpoint.
            "Authorization": f"Bearer {token}",
            "anthropic-beta": config.OAUTH_BETA,
            "User-Agent": config.USER_AGENT,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise FetchError(f"usage fetch HTTP {e.code}", status=e.code) from e
    except urllib.error.URLError as e:
        raise FetchError(f"usage fetch failed: {e.reason}") from e
    return _parse_windows(data)
