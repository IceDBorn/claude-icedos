"""SQLite time-series of usage samples + a small key/value meta table."""
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts        INTEGER NOT NULL,   -- unix ms when the value was observed
  window    TEXT    NOT NULL,   -- five_hour, seven_day, ...
  util      REAL    NOT NULL,   -- 0..100
  resets_at TEXT,               -- ISO-8601 or NULL
  source    TEXT    NOT NULL,   -- 'cache' | 'poll'
  PRIMARY KEY (ts, window)
);
CREATE INDEX IF NOT EXISTS idx_samples_window_ts ON samples(window, ts);
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


def _latest(con: sqlite3.Connection, window: str):
    return con.execute(
        "SELECT util, resets_at FROM samples WHERE window=? ORDER BY ts DESC LIMIT 1",
        (window,),
    ).fetchone()


def record(con: sqlite3.Connection, ts: int, windows: dict, source: str) -> int:
    """Insert one row per window. Skip a window whose (util, resets_at) is
    unchanged from its last stored row (keeps idle periods from spamming rows).
    Returns number of rows inserted."""
    n = 0
    for key, w in windows.items():
        prev = _latest(con, key)
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


def get_meta(con: sqlite3.Connection, k: str, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def get_meta_int(con: sqlite3.Connection, k: str, default: int = 0) -> int:
    v = get_meta(con, k)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def set_meta(con: sqlite3.Connection, k: str, v) -> None:
    con.execute(
        "INSERT INTO meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, str(v)),
    )
    con.commit()
