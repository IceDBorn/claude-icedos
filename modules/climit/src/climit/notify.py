"""Optional desktop alerts via notify-send.

Fires when a window is projected to exhaust before its reset, or crosses a
utilization threshold. Debounced per window through the meta table so you get one
notification per condition, not one per poll.
"""
import shutil
import subprocess
import time

from . import config, rates, store

THRESHOLDS = (80.0, 95.0)
DEBOUNCE_MS = 30 * 60_000


def _send(title: str, body: str, urgency: str = "normal") -> None:
    exe = shutil.which("notify-send")
    if not exe:
        return
    try:
        subprocess.run(
            [exe, "-u", urgency, "-a", "climit", title, body],
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _condition(r) -> str | None:
    if r.will_exhaust_before_reset:
        return "exhaust"
    for t in sorted(THRESHOLDS, reverse=True):
        if r.util >= t:
            return f"th{int(t)}"
    return None


def check(con, now_ms: int | None = None) -> None:
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    for window in store.windows_present(con):
        rows = store.samples_for(con, window)
        r = rates.compute(window, rows, now)
        if not r:
            continue
        cond = _condition(r)
        key = f"alert_{window}"
        if cond is None:
            store.set_meta(con, key, "")  # cleared → a later re-cross re-notifies
            continue
        prev = store.get_meta(con, key, "")
        last_ms = store.get_meta_int(con, key + "_ms", 0)
        if prev == cond and now - last_ms < DEBOUNCE_MS:
            continue
        store.set_meta(con, key, cond)
        store.set_meta(con, key + "_ms", now)
        label = config.label(window)
        if cond == "exhaust":
            _send(
                "climit — pace warning",
                f"{label}: {r.util:.0f}% used, ~{r.per_hour:.1f}%/h — projected to hit the cap "
                "before it resets.",
                urgency="critical",
            )
        else:
            _send("climit — usage high", f"{label}: {r.util:.0f}% used.", urgency="normal")
