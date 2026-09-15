"""Reset-aware burn-rate math over the stored sample series.

Rate is a windowed average: (peak_now - peak_at(t_start)) / elapsed, where
t_start = max(now - lookback, start of the current window). Usage inside a window
never goes down, so the running peak absorbs sources that round differently
(the endpoint can say 75 while the status line says 74).
"""
from dataclasses import dataclass
from datetime import datetime

# A window starts when resets_at moves later by more than this; sub-second jitter doesn't count.
RESET_SHIFT_MS = 10 * 60_000
# Without resets_at on both rows, a drop this far below the window's peak marks a reset.
RESET_DROP = 5.0


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            return None


def to_ms(resets_at):
    """ISO-8601 reset time to unix ms, or None."""
    dt = parse_iso(resets_at)
    return int(dt.timestamp() * 1000) if dt else None


def _window_rows(rows):
    """Rows of the current window, minus late reports from the previous one."""
    start, peak, reset = 0, rows[0][1], to_ms(rows[0][2])
    stale = set()
    for i in range(1, len(rows)):
        _ts, util, resets_at = rows[i]
        r = to_ms(resets_at)
        if r is not None and reset is not None:
            if r < reset - RESET_SHIFT_MS:
                stale.add(i)  # a late report from the previous window (an idle session's redraw)
                continue
            new_window = r > reset + RESET_SHIFT_MS
        else:
            new_window = util < peak - RESET_DROP
        if new_window:
            start, peak, reset = i, util, r
        else:
            peak = max(peak, util)
            reset = reset if reset is not None else r
    return [row for i, row in enumerate(rows) if i >= start and i not in stale]


@dataclass
class Rate:
    window: str
    util: float
    resets_at: str | None
    per_min: float
    per_hour: float
    per_8h: float
    per_day: float
    runway_min: float | None            # minutes to 100% at current rate; None = never
    exhaust_ts: int | None              # unix ms projected exhaustion
    reset_ts: int | None                # unix ms window reset
    will_exhaust_before_reset: bool
    stale: bool
    last_ts: int


def compute(window, rows, now_ms, lookback_min=60, stale_after_min=30):
    """rows: (ts, util, resets_at) ascending. Returns Rate or None if no data."""
    if not rows:
        return None
    latest_ts = rows[-1][0]
    stale = (now_ms - latest_ts) > stale_after_min * 60_000
    win = _window_rows(rows)
    resets_at = next((r for _, _, r in reversed(win) if r), None)
    reset_ts = to_ms(resets_at)

    # The window reset with no sample since: usage is back to zero.
    if reset_ts is not None and now_ms >= reset_ts:
        return Rate(window, 0.0, None, 0.0, 0.0, 0.0, 0.0, None, None, None, False, stale, latest_ts)

    util_now = max(u for _, u, _ in win)
    t_start = max(now_ms - lookback_min * 60_000, win[0][0])
    util_start = max((u for ts, u, _ in win if ts <= t_start), default=win[0][1])
    span_min = max((now_ms - t_start) / 60_000, 1e-9)
    per_min = max((util_now - util_start) / span_min, 0.0)
    per_hour, per_8h, per_day = per_min * 60, per_min * 480, per_min * 1440

    remaining = max(100.0 - util_now, 0.0)
    if per_min > 1e-6:
        runway_min = remaining / per_min
        exhaust_ts = int(now_ms + runway_min * 60_000)
    else:
        runway_min = None
        exhaust_ts = None

    will = bool(exhaust_ts is not None and reset_ts is not None and exhaust_ts < reset_ts)
    return Rate(
        window=window,
        util=util_now,
        resets_at=resets_at,
        per_min=per_min,
        per_hour=per_hour,
        per_8h=per_8h,
        per_day=per_day,
        runway_min=runway_min,
        exhaust_ts=exhaust_ts,
        reset_ts=reset_ts,
        will_exhaust_before_reset=will,
        stale=stale,
        last_ts=latest_ts,
    )
