"""Acquisition loop.

Each cycle prefers the free local cache (when a Claude session kept it fresh) and
only falls back to the live endpoint when the cache is stale — and even then never
more than once per MIN_INTERVAL, so no caller can trip the endpoint's 429 bucket.
"""
import time

from . import config, sources, store


def _now_ms() -> int:
    return int(time.time() * 1000)


def poll_once(con, interval_ms: int):
    """One acquisition cycle. Returns (source, rows_inserted).

    source ∈ {'cache', 'cache-stale', 'poll', 'throttled', 'error:<x>'}.
    """
    now = _now_ms()
    cached = sources.read_cache()
    if cached:
        windows, fetched = cached
        if now - fetched < interval_ms:
            return "cache", store.record(con, fetched, windows, "cache")

    # Cache missing or stale → live fetch, but honour the hard rate-limit floor
    # (shared across every caller via the meta table).
    last_live = store.get_meta_int(con, "last_live_ms", 0)
    if now - last_live < config.MIN_INTERVAL * 1000:
        if cached:
            windows, fetched = cached
            return "cache-stale", store.record(con, fetched, windows, "cache")
        return "throttled", 0

    try:
        windows, ts = sources.fetch_live()
    except sources.FetchError as e:
        if e.status == 429:
            store.set_meta(con, "last_live_ms", now)  # avoid hammering after a 429
        return f"error:{e.status or e}", 0
    store.set_meta(con, "last_live_ms", ts)
    return "poll", store.record(con, ts, windows, "poll")


def run(interval: int = config.DEFAULT_INTERVAL, once: bool = False, log=print, alert=None):
    """Poll forever (or once). `alert`, if given, is called as alert(con) after a
    successful acquisition."""
    interval = int(interval)
    assert interval >= config.MIN_INTERVAL, (
        f"interval {interval}s is below the {config.MIN_INTERVAL}s floor "
        "(the /api/oauth/usage rate-limit bucket)"
    )
    con = store.connect()
    backoff = 0
    try:
        while True:
            src, n = poll_once(con, interval * 1000)
            if src.startswith("error"):
                if "429" in src:
                    backoff = min(backoff * 2 if backoff else interval, 3600)
                    log(f"[poll] 429 — backing off {backoff}s")
                else:
                    backoff = interval
                    log(f"[poll] {src}")
            else:
                backoff = 0
                log(f"[poll] {src} (+{n})")
                if alert:
                    try:
                        alert(con)
                    except Exception as e:  # alerts must never kill the poller
                        log(f"[alert] {e}")
            if once:
                return con
            time.sleep(backoff or interval)
    except KeyboardInterrupt:
        return con
