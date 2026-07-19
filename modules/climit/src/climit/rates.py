"""Reset-aware burn-rate math over the stored sample series.

Rate is a windowed average: (util_now - util_at(t_start)) / elapsed, where
t_start = max(now - lookback, last-reset boundary). This naturally decays to
zero when usage stops and never goes negative across a window reset.
"""
from dataclasses import dataclass
from datetime import datetime


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None


def _reset_boundary_ts(rows):
    """ts marking the start of the current window: the sample right after the
    most recent reset. A reset shows up as a utilization DROP (a fresh window
    starts near 0). The endpoint's resets_at jitters sub-second between polls
    (e.g. 20:59:59.9 vs 21:00:00.1 around the same boundary), so it is NOT a
    reliable reset signal and is deliberately ignored here."""
    boundary = rows[0][0]
    prev_util = rows[0][1]
    for ts, util, _reset in rows[1:]:
        if util < prev_util - 0.5:
            boundary = ts
        prev_util = util
    return boundary


def _util_at(rows, t):
    """Carry-forward utilization at time t (value of last sample with ts<=t)."""
    val = rows[0][1]
    for ts, util, _ in rows:
        if ts <= t:
            val = util
        else:
            break
    return val


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
    latest_ts, util_now, resets_at = rows[-1]
    boundary = _reset_boundary_ts(rows)
    t_start = max(now_ms - lookback_min * 60_000, boundary)
    util_start = _util_at(rows, t_start)
    span_min = max((now_ms - t_start) / 60_000, 1e-9)
    per_min = max((util_now - util_start) / span_min, 0.0)
    per_hour, per_8h, per_day = per_min * 60, per_min * 480, per_min * 1440

    reset_dt = parse_iso(resets_at)
    reset_ts = int(reset_dt.timestamp() * 1000) if reset_dt else None

    remaining = max(100.0 - util_now, 0.0)
    if per_min > 1e-6:
        runway_min = remaining / per_min
        exhaust_ts = int(now_ms + runway_min * 60_000)
    else:
        runway_min = None
        exhaust_ts = None

    will = bool(exhaust_ts is not None and reset_ts is not None and exhaust_ts < reset_ts)
    stale = (now_ms - latest_ts) > stale_after_min * 60_000
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
