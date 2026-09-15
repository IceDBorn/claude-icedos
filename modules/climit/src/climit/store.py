"""SQLite time-series of usage samples + a small key/value meta table."""
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts        INTEGER NOT NULL,   -- unix ms when the value was observed
  window    TEXT    NOT NULL,   -- five_hour, seven_day, ...
  util      REAL    NOT NULL,   -- 0..100
  resets_at TEXT,               -- ISO-8601 or NULL
  source    TEXT    NOT NULL,   -- 'poll' | 'statusline' ('cache' in old rows)
  PRIMARY KEY (ts, window)
);
CREATE INDEX IF NOT EXISTS idx_samples_window_ts ON samples(window, ts);
CREATE INDEX IF NOT EXISTS idx_samples_window_source_ts ON samples(window, source, ts);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path=None) -> sqlite3.Connection:
    p = path or config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    return con


def _latest(con: sqlite3.Connection, window: str, source: str):
    return con.execute(
        "SELECT util, resets_at FROM samples WHERE window=? AND source=? ORDER BY ts DESC LIMIT 1",
        (window, source),
    ).fetchone()


def record(con: sqlite3.Connection, ts: int, windows: dict, source: str) -> int:
    """Insert one row per window. Skip a window whose (util, resets_at) is
    unchanged from the same source's last row, so idle status line redraws and
    interleaved sources don't spam rows. Returns number of rows inserted."""
    n = 0
    for key, w in windows.items():
        prev = _latest(con, key, source)
        if prev is not None and prev[0] == w["util"] and prev[1] == w["resets_at"]:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO samples(ts,window,util,resets_at,source) VALUES (?,?,?,?,?)",
            (ts, key, w["util"], w["resets_at"], source),
        )
        n += cur.rowcount
    con.commit()
    return n


def samples_for(con: sqlite3.Connection, window: str, since_ts: int | None = None):
    """Rows (ts, util, resets_at) ascending. If since_ts given, also include the
    one bracketing row just before it so a rate window always has an anchor."""
    if since_ts is None:
        return con.execute(
            "SELECT ts, util, resets_at FROM samples WHERE window=? ORDER BY ts",
            (window,),
        ).fetchall()
    rows = con.execute(
        "SELECT ts, util, resets_at FROM samples WHERE window=? AND ts>=? ORDER BY ts",
        (window, since_ts),
    ).fetchall()
    bracket = con.execute(
        "SELECT ts, util, resets_at FROM samples WHERE window=? AND ts<? ORDER BY ts DESC LIMIT 1",
        (window, since_ts),
    ).fetchone()
    if bracket is not None:
        rows = [bracket] + rows
    return rows


def windows_present(con: sqlite3.Connection):
    return [r[0] for r in con.execute("SELECT DISTINCT window FROM samples").fetchall()]


def latest_ts(con: sqlite3.Connection, window: str) -> int | None:
    """Most recent sample timestamp (unix ms) for a window, or None."""
    row = con.execute(
        "SELECT MAX(ts) FROM samples WHERE window=?", (window,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def prune_retired(con: sqlite3.Connection, windows, now_ms: int):
    """Filter out weekly_scoped windows whose newest sample is older than
    config.WEEKLY_SCOPED_RETIRE_DAYS (retired/renamed models leave frozen rows
    that would otherwise render and alert forever). Main windows pass through.
    Used by both the CLI and the notifier so surfaces never diverge."""
    windows = list(windows)
    days = config.WEEKLY_SCOPED_RETIRE_DAYS
    if not days:
        return windows
    cutoff = now_ms - days * 86_400_000
    return [
        w for w in windows
        if not w.startswith(config.WEEKLY_SCOPED_PREFIX) or (latest_ts(con, w) or 0) >= cutoff
    ]


def get_meta(con: sqlite3.Connection, k: str, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def get_meta_int(con: sqlite3.Connection, k: str, default: int = 0) -> int:
    v = get_meta(con, k)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def set_meta(con: sqlite3.Connection, k: str, v) -> None:
    con.execute(
        "INSERT INTO meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, str(v)),
    )
    con.commit()
