"""Command-line interface: watch (default), status, statusline, poll, contrib."""
import argparse
import dataclasses
import getpass
import json as _json
import os
import shutil
import socket
import subprocess
import sys
import time

from . import __version__, config, poller, sources, store
from .rates import compute, to_ms


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------- formatting helpers ----------
def _color_on() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str, on: bool | None = None) -> str:
    if on is None:
        on = _color_on()
    return f"\033[{code}m{s}\033[0m" if on else s


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
    filled = max(0, min(width, round(util / 100 * width)))
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
    """The windows and rates every surface shows (terminal, widget, status line, alerts)."""
    present = [w for w in store.windows_present(con) if config.is_known_window(w)]
    order = [w for w in config.WINDOW_ORDER if w in present]
    scoped = sorted(
        (w for w in present if w.startswith(config.WEEKLY_SCOPED_PREFIX)),
        key=lambda w: config.window_display_name(w),
    )
    out = []
    for w in store.prune_retired(con, order + scoped, now_ms):
        rows = store.samples_for(con, w, since_ts=now_ms - config.HISTORY_MS)
        r = compute(w, rows, now_ms, lookback_min=lookback)
        if r:
            out.append(r)
    return out


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
        return "no data yet. Run Claude Code, or check `systemctl --user status climit.timer`."
    col = _col_width()
    return "\n\n".join(render_section(r, now_ms, col) for r in rlist)


def render_statusline(rlist, now_ms: int, color: bool | None = None) -> str:
    """Compact one-line summary, e.g. `5h 31% 2.1/h 1h25m · wk 74% 0.4/h 9h34m`."""
    if not rlist:
        return "climit: no data"
    parts = []
    for r in rlist:
        seg = [config.short_label(r.window), _c(f"{_util_code(r.util)};1", f"{r.util:.0f}%", color)]
        if r.will_exhaust_before_reset:
            seg.insert(0, _c("31;1", "⚠", color))
        # Idle windows show 0.0/h forever; drop it so the busy ones stand out.
        if r.per_hour >= 0.05:
            seg.append(_c("2", f"{r.per_hour:.1f}/h", color))
        if r.reset_ts:
            seg.append(_c("2", fmt_dur(r.reset_ts - now_ms), color))
        parts.append(" ".join(seg))
    return _c("2", " · ", color).join(parts)


# p10k theme colours (grey context/vcs, purple dir, cyan ahead/behind).
GREY, PURPLE, CYAN = "38;5;242", "38;2;145;65;172", "38;2;33;144;164"


def _git(cwd: str):
    """(branch, dirty, behind, ahead) for cwd, or None outside a work tree."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", cwd, "status", "--porcelain=v2", "--branch"],
            capture_output=True, text=True, timeout=1, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode:
        return None
    oid, branch, dirty, ahead, behind = "", "", False, 0, 0
    for line in proc.stdout.splitlines():
        if line.startswith("# branch.oid "):
            oid = line.split()[2]
        elif line.startswith("# branch.head "):
            branch = line.split(maxsplit=2)[2]
        elif line.startswith("# branch.ab "):
            a, b = line.split()[2:4]
            ahead, behind = int(a), -int(b)
        elif not line.startswith("#"):
            dirty = True
    if branch == "(detached)":
        branch = "@" + oid[:8]
    return branch, dirty, behind, ahead


def render_prompt(payload, rlist, now_ms: int) -> str:
    """Claude Code status line: user@host, dir, git state; usage windows on a second line."""
    payload = payload if isinstance(payload, dict) else {}
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd") or os.getcwd()
    home = str(config.HOME)
    shown = "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + "/") else cwd
    host = socket.gethostname().split(".")[0]
    parts = [_c(GREY, f"{getpass.getuser()}@{host}", True), _c(PURPLE, shown, True)]
    git = _git(cwd)
    if git:
        branch, dirty, behind, ahead = git
        arrows = ("⇣" if behind else "") + ("⇡" if ahead else "")
        parts.append(_c(GREY, branch + ("*" if dirty else ""), True) + (_c(CYAN, arrows, True) if arrows else ""))
    return " ".join(parts) + "\n" + render_statusline(rlist, now_ms, color=True)


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
            "windows": [
                {
                    **dataclasses.asdict(r),
                    "label": config.label(r.window),
                    "short_label": config.short_label(r.window),
                }
                for r in rlist
            ],
            "cross": cross_metric(rlist),
        },
        indent=2,
    )


# ---------- commands ----------
def cmd_status(args) -> int:
    con = store.connect()
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


def cmd_statusline(args) -> int:
    payload = {}
    if not sys.stdin.isatty():
        try:
            payload = _json.load(sys.stdin)
        except ValueError:
            pass
    now = _now_ms()
    con = store.connect()
    # An idle session keeps its last rate_limits; once that window has reset they are history.
    windows = {
        k: w for k, w in sources.parse_statusline(payload).items()
        if (to_ms(w["resets_at"]) or now + 1) > now
    }
    if windows:
        store.record(con, now, windows, "statusline")
    print(render_prompt(payload, collect(con, now), now))
    return 0


def cmd_poll(args) -> int:
    con = store.connect()
    status, n = poller.poll_once(con)
    print(f"{status} (+{n} rows)")
    if not args.no_alerts:
        from . import notify

        now = _now_ms()
        style = notify.AlertStyle(
            urgency=args.alert_urgency,
            timeout=args.alert_timeout,
            transient=args.alert_transient,
        )
        notify.check(con, collect(con, now), now, style)
    return 1 if status.startswith("error") else 0


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

    st = sub.add_parser("status", help="print current usage + rates once and exit")
    st.add_argument("--lookback", type=int, default=60, help="rate window in minutes")
    st.add_argument("--json", action="store_true", help="machine-readable output")
    st.add_argument("--statusline", action="store_true", help="one-line output for bars/tmux")

    sub.add_parser(
        "statusline",
        help="Claude Code status line: records the rate_limits piped on stdin, prints the line",
    )

    pl = sub.add_parser("poll", help="fetch the usage endpoint once (rate-limit safe), then alert")
    pl.add_argument("--no-alerts", action="store_true", help="disable notify-send alerts")
    pl.add_argument("--alert-urgency", choices=("low", "normal", "critical"), default="normal",
                    help="alert urgency; critical never auto-expires on Plasma")
    pl.add_argument("--alert-timeout", type=int, default=10,
                    help="seconds an alert stays on screen (0 = until dismissed)")
    pl.add_argument("--alert-transient", action="store_true",
                    help="don't keep alerts in the notification history")

    cb = sub.add_parser("contrib", help="what's contributing to your usage (local, approximate)")
    cb.add_argument("--hours", type=int, default=24, help="lookback window in hours (default 24)")
    cb.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd is None:  # bare `climit` → live watch dashboard
        args.cmd = "watch"
        args.lookback = 60
        args.refresh = 10
    return {
        "status": cmd_status,
        "watch": cmd_watch,
        "statusline": cmd_statusline,
        "poll": cmd_poll,
        "contrib": cmd_contrib,
    }[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
