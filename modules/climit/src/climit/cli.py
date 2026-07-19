"""Command-line interface: status (default), watch, statusline/json, poll, daemon."""
import argparse
import dataclasses
import json as _json
import os
import shutil
import sys
import time

from . import __version__, config, poller, store
from .rates import compute


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------- formatting helpers ----------
def _color_on() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _color_on() else s


def _util_code(u: float) -> str:
    return "32" if u < 50 else ("33" if u < 80 else "31")


def fmt_dur(ms) -> str:
    if ms is None:
        return "—"
    s = max(0, int(ms // 1000))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def bar(util: float, width: int = 16) -> str:
    filled = max(0, min(width, int(round(util / 100 * width))))
    return "█" * filled + "░" * (width - filled)


# Column labels of the metric strip under each window's bar. Same six numbers the
# Plasma widget shows, in the same order.
METRIC_LABELS = ("%/min", "%/hr", "%/8h", "%/day", "ends in", "resets in")


def _col_width() -> int:
    """Width of one metric column; the bar spans all six of them."""
    try:
        cols = shutil.get_terminal_size((96, 24)).columns
    except Exception:
        cols = 96
    return max(10, min(cols, 108) // len(METRIC_LABELS))


def collect(con, now_ms: int, lookback: int = 60):
    present = store.windows_present(con)
    order = [w for w in config.WINDOW_ORDER if w in present]
    order += [w for w in present if w not in config.WINDOW_ORDER]
    out = []
    for w in order:
        r = compute(w, store.samples_for(con, w), now_ms, lookback_min=lookback)
        if r:
            out.append(r)
    return out


def freshen(con, args) -> None:
    """Best-effort top-up before display; never crashes the reader."""
    if getattr(args, "no_poll", False):
        return
    interval = getattr(args, "interval", None) or config.DEFAULT_INTERVAL
    try:
        poller.poll_once(con, interval * 1000)
    except Exception:
        pass


# ---------- renderers ----------
def _metric_values(r, now_ms: int):
    runway = "∞" if r.runway_min is None else fmt_dur(r.runway_min * 60_000)
    return (
        f"{r.per_min:.2f}",
        f"{r.per_hour:.1f}",
        f"{r.per_8h:.1f}",
        f"{r.per_day:.1f}",
        ("⚠ " if r.will_exhaust_before_reset else "") + runway,
        fmt_dur(r.reset_ts - now_ms) if r.reset_ts else "—",
    )


def render_section(r, now_ms: int, col: int) -> str:
    """One window as a block: heading + used%, full-width bar, metric strip.

    Mirrors the Plasma widget's popup (plasmoid/contents/ui/main.qml).
    """
    code = _util_code(r.util)
    width = col * len(METRIC_LABELS)

    name = config.label(r.window) + (" ·stale" if r.stale else "")
    used = f"{r.util:.0f}%"
    warn = "⚠ hits cap before reset" if r.will_exhaust_before_reset else ""
    # pad on the plain text, colour afterwards, so the escapes never shift columns
    right = f"{warn}  {used}" if warn else used
    gap = " " * max(1, width - len(name) - len(right))
    head = _c("1", name) + gap + (_c("31;1", warn) + "  " if warn else "") + _c(f"{code};1", used)

    labels = "".join(label.center(col) for label in METRIC_LABELS)
    cells = [v.center(col) for v in _metric_values(r, now_ms)]
    if r.will_exhaust_before_reset:
        cells[4] = _c("31", cells[4])

    # blank line under the bar, mirroring the widget's spacing
    return "\n".join([head, _c(code, bar(r.util, width)), "", _c("2", labels), "".join(cells)])


def render_table(rlist, now_ms: int) -> str:
    if not rlist:
        return "no data yet — run `climit daemon` (or `climit poll`) to collect samples."
    col = _col_width()
    return "\n\n".join(render_section(r, now_ms, col) for r in rlist)


def render_statusline(rlist, now_ms: int) -> str:
    if not rlist:
        return "climit: no data"
    short = {"five_hour": "5h", "seven_day": "wk", "seven_day_opus": "opus", "seven_day_sonnet": "son"}
    parts, warn = [], False
    for r in rlist:
        parts.append(f"{short.get(r.window, r.window[:3])} {r.util:.0f}%·{r.per_hour:.1f}/h")
        warn = warn or r.will_exhaust_before_reset
    return ("⚠ " if warn else "") + "  ".join(parts)


def cross_metric(rlist):
    """Exchange rate between the 5h session and weekly windows, or None when idle.

    Both windows rise from the same usage, so (5h burn / weekly burn) is a fairly
    stable "how much of the session does 1% of weekly cost" number.
    """
    by = {r.window: r for r in rlist}
    fh, wk = by.get("five_hour"), by.get("seven_day")
    if not fh or not wk or wk.per_hour <= 1e-6:
        return None
    ratio = fh.per_hour / wk.per_hour
    headroom = (100.0 - fh.util) / ratio if ratio > 1e-9 else None
    return {
        "session_pct_per_weekly_pct": ratio,
        "weekly_pct_until_session_caps": headroom,
    }


def render_cross(rlist) -> str | None:
    c = cross_metric(rlist)
    if not c:
        return None
    line = f"1% weekly usage ≈ {c['session_pct_per_weekly_pct']:.1f}% of 5-hour usage"
    if c["weekly_pct_until_session_caps"] is not None:
        line += f"  ·  ~{c['weekly_pct_until_session_caps']:.0f}% more weekly usage before 5-hour usage caps"
    return _c("2", line)


def render_json(rlist, now_ms: int) -> str:
    return _json.dumps(
        {
            "now_ms": now_ms,
            "windows": [dataclasses.asdict(r) for r in rlist],
            "cross": cross_metric(rlist),
        },
        indent=2,
    )


# ---------- commands ----------
def cmd_status(args) -> int:
    con = store.connect()
    freshen(con, args)
    now = _now_ms()
    rlist = collect(con, now, lookback=args.lookback)
    if args.json:
        print(render_json(rlist, now))
    elif args.statusline:
        print(render_statusline(rlist, now))
    else:
        print(render_table(rlist, now))
        cross = render_cross(rlist)
        if cross:
            print()
            print(cross)
    return 0


def cmd_watch(args) -> int:
    con = store.connect()
    refresh = max(2, args.refresh)
    try:
        while True:
            freshen(con, args)
            now = _now_ms()
            rlist = collect(con, now, lookback=args.lookback)
            sys.stdout.write("\033[2J\033[H")
            print(_c("2", f"{time.strftime('%H:%M:%S')}   "
                          f"(refresh {refresh}s · Ctrl-C to quit)\n"))
            print(render_table(rlist, now))
            cross = render_cross(rlist)
            if cross:
                print()
                print(cross)
            time.sleep(refresh)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_poll(args) -> int:
    con = store.connect()
    src, n = poller.poll_once(con, (args.interval or config.DEFAULT_INTERVAL) * 1000)
    print(f"{src} (+{n} rows)")
    return 1 if src.startswith("error") else 0


def cmd_daemon(args) -> int:
    alert = None
    if not args.no_alerts:
        from . import notify
        alert = notify.check
    print(f"climit daemon: interval {args.interval}s · db {config.DB_PATH}")
    poller.run(interval=args.interval, alert=alert)
    return 0


def cmd_contrib(args) -> int:
    from . import contrib

    acc = contrib.analyze(hours=args.hours)
    print(contrib.to_json(acc, args.hours) if args.json else contrib.render(acc, args.hours))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="climit",
        description="Track Claude usage limits + burn rate. Bare invocation runs the live dashboard.",
    )
    p.add_argument("--version", action="version", version=f"climit {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--interval", type=int, default=config.DEFAULT_INTERVAL,
                        help=f"min seconds between live fetches (floor {config.MIN_INTERVAL})")
        sp.add_argument("--no-poll", action="store_true", help="read stored data only; no fetch")
        sp.add_argument("--lookback", type=int, default=60, help="rate window in minutes")

    st = sub.add_parser("status", help="print current usage + rates once and exit")
    common(st)
    st.add_argument("--json", action="store_true", help="machine-readable output")
    st.add_argument("--statusline", action="store_true", help="one-line output for bars/tmux")

    pl = sub.add_parser("poll", help="run one acquisition cycle and exit")
    pl.add_argument("--interval", type=int, default=config.DEFAULT_INTERVAL)

    d = sub.add_parser("daemon", help="run the background poller")
    d.add_argument("--interval", type=int, default=config.DEFAULT_INTERVAL)
    d.add_argument("--no-alerts", action="store_true", help="disable notify-send alerts")

    cb = sub.add_parser("contrib", help="what's contributing to your usage (local, approximate)")
    cb.add_argument("--hours", type=int, default=24, help="lookback window in hours (default 24)")
    cb.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd is None:  # bare `climit` → live watch dashboard
        args.cmd = "watch"
        args.no_poll = False
        args.interval = config.DEFAULT_INTERVAL
        args.lookback = 60
        args.refresh = 10
    return {
        "status": cmd_status,
        "watch": cmd_watch,
        "poll": cmd_poll,
        "daemon": cmd_daemon,
        "contrib": cmd_contrib,
    }[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
