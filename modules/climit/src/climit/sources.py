"""Two data sources for usage windows, identical output shape.

read_cache()  — parse ~/.claude.json cachedUsageUtilization (free, offline, no 429;
                fresh only while a Claude session is running).
fetch_live()  — GET the OAuth usage endpoint (authoritative; rate-limited).

Both return (windows, fetched_at_ms) where
    windows = { window_key: {"util": float, "resets_at": str|None} }
"""
import json
import time
import urllib.error
import urllib.request

from . import auth, config


class FetchError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_windows(util: dict) -> dict:
    """Pick only real burn windows from a utilization payload.

    Works for both cache and live payloads. The utilization dict carries the
    stable window objects ({utilization, resets_at}), a "limits" array, and a
    dumping ground of internal model-codename keys (nimbus_quill, ...) that are
    NOT windows. We keep only the stable config.KNOWN_WINDOW_KEYS (recording a
    null resets_at as-is, so a post-reset zero sample is never dropped), and
    additionally synthesize per-model weekly windows from the limits[] array
    (kind == "weekly_scoped", keyed by scope.model.display_name), mirroring
    Claude Code's own k1t handling.
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


def read_cache():
    """Return (windows, fetched_at_ms) from the local cache, or None if unusable."""
    try:
        with open(config.CLAUDE_JSON) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    cache = data.get("cachedUsageUtilization")
    if not isinstance(cache, dict):
        return None
    windows = _parse_windows(cache.get("utilization") or {})
    fetched = cache.get("fetchedAtMs")
    if not windows or fetched is None:
        return None
    return windows, int(fetched)


def fetch_live():
    """GET the usage endpoint. Returns (windows, now_ms). Raises FetchError."""
    token = auth.get_access_token()
    url = config.API_BASE + config.USAGE_PATH
    windows = _do_fetch(url, token, retried=False)
    return windows, _now_ms()


def _do_fetch(url: str, token: str, retried: bool) -> dict:
    req = urllib.request.Request(
        url,
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
        if e.code == 401 and not retried:
            # token likely expired mid-flight — force a refresh and retry once
            token = auth.get_access_token(force_refresh=True)
            return _do_fetch(url, token, retried=True)
        raise FetchError(f"usage fetch HTTP {e.code}", status=e.code) from e
    except urllib.error.URLError as e:
        raise FetchError(f"usage fetch failed: {e.reason}") from e
    return _parse_windows(data)
