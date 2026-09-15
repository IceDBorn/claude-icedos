"""One live acquisition, run by the systemd timer (or `climit poll`).

The rate-limit floor and 429 backoff live in the meta table, so every caller
shares them and no caller can trip the endpoint's 429 bucket.
"""
import time

from . import auth, config, sources, store

# Timer activations land a few ms either side of the interval; don't skip those.
FLOOR_SLACK_MS = 5_000
MAX_BACKOFF_S = 3600


def poll_once(con, now_ms: int | None = None):
    """Returns (status, rows_inserted); status is 'poll', 'throttled', 'skipped: …' or 'error: …'."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if now < store.get_meta_int(con, "live_next_ms", 0) - FLOOR_SLACK_MS:
        return "throttled", 0

    try:
        token = auth.get_access_token()
    except auth.AuthError as e:
        return f"skipped: {e}", 0

    # Claim the slot before fetching so a concurrent caller sees the floor.
    store.set_meta(con, "live_next_ms", now + config.MIN_INTERVAL * 1000)
    try:
        windows = sources.fetch_live(token)
    except sources.FetchError as e:
        if e.status == 429:
            backoff = min(max(store.get_meta_int(con, "live_backoff_s") * 2, config.MIN_INTERVAL), MAX_BACKOFF_S)
            store.set_meta(con, "live_backoff_s", backoff)
            store.set_meta(con, "live_next_ms", now + backoff * 1000)
        return f"error: {e}", 0

    store.set_meta(con, "live_backoff_s", 0)
    return "poll", store.record(con, now, windows, "poll")
