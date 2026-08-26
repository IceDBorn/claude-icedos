"""Paths, endpoints, and tunables. Everything overridable via env."""
import os
from pathlib import Path

HOME = Path.home()


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


# --- credential + cache sources (Claude Code's own files) ---
CREDS_PATH = _env_path("CLIMIT_CREDS", HOME / ".claude" / ".credentials.json")
CLAUDE_JSON = _env_path("CLIMIT_CLAUDE_JSON", HOME / ".claude.json")

# --- our storage (XDG) ---
_DATA_HOME = _env_path("XDG_DATA_HOME", HOME / ".local" / "share")
DATA_DIR = _DATA_HOME / "climit"
DB_PATH = _env_path("CLIMIT_DB", DATA_DIR / "usage.db")

# --- Anthropic OAuth usage endpoint ---
API_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
USAGE_PATH = "/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"
ANTHROPIC_VERSION = "2023-06-01"
# UA prefix "claude-code/" is REQUIRED — without it the endpoint drops you into
# an aggressively rate-limited bucket (persistent 429). Version is cosmetic.
USER_AGENT = os.environ.get("CLIMIT_UA", "claude-code/2.1.212")

# Refresh only when within this window of expiry. Kept small so Claude Code's own
# ~10-min-ahead refresh wins the race while it's running (avoids double-rotation).
PROACTIVE_REFRESH_BUFFER_MS = int(os.environ.get("CLIMIT_REFRESH_BUFFER_MS", "60000"))

# --- polling ---
MIN_INTERVAL = 180          # hard floor between live fetches (429 safety)
DEFAULT_INTERVAL = 300
HTTP_TIMEOUT = 30

# --- windows: stable keys present in BOTH cache and live responses ---
WINDOW_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "weekly",
    "seven_day_opus": "week · Opus",
    "seven_day_sonnet": "week · Sonnet",
}
WINDOW_ORDER = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]

# Real burn-window keys; internal model codenames (nimbus_quill, ...) ignored, mirroring Claude Code's allowlist.
# Tuple copy so later mutation of WINDOW_ORDER can't silently diverge the set.
KNOWN_WINDOW_KEYS = tuple(WINDOW_ORDER)

# Per-model weekly windows are synthesized from limits[] (kind == "weekly_scoped")
# as "<WEEKLY_SCOPED_PREFIX><slug>", so they flow through the same samples table.
WEEKLY_SCOPED_PREFIX = "weekly_scoped:"

# Retire weekly_scoped windows with no sample for this many days (0 = disabled).
# Active windows are re-recorded every poll (resets_at jitters), so this never hides them.
WEEKLY_SCOPED_RETIRE_DAYS = int(os.environ.get("CLIMIT_SCOPED_RETIRE_DAYS", "30"))

# Shorthand labels for compact surfaces (statusline, panel widget).
SHORT_LABELS = {
    "five_hour": "5h",
    "seven_day": "wk",
    "seven_day_opus": "opus",
    "seven_day_sonnet": "son",
}


def weekly_scoped_slug(display_name: str) -> str:
    """Stable slug for a per-model window key: lowercase, spaces -> '_'. Other
    punctuation ('.', '-') is preserved so names like 'Claude Opus 4.5' round-trip."""
    return display_name.strip().lower().replace(" ", "_")


def window_display_name(window: str) -> str:
    """Inverse of weekly_scoped_slug: 'weekly_scoped:claude_opus_4.5' -> 'Claude Opus 4.5'."""
    slug = window[len(WEEKLY_SCOPED_PREFIX):] if window.startswith(WEEKLY_SCOPED_PREFIX) else window
    return " ".join(w.title() for w in slug.split("_"))


def is_known_window(window: str) -> bool:
    """True for windows climit should render: the stable dict windows plus any
    synthesized weekly_scoped key. Keeps phantom/promotional keys (incl. leftover
    historical DB rows) out of every surface."""
    return window in KNOWN_WINDOW_KEYS or window.startswith(WEEKLY_SCOPED_PREFIX)


def label(window: str) -> str:
    if window in WINDOW_LABELS:
        return WINDOW_LABELS[window]
    if window.startswith(WEEKLY_SCOPED_PREFIX):
        return "week · " + window_display_name(window)
    return window


def short_label(window: str) -> str:
    if window in SHORT_LABELS:
        return SHORT_LABELS[window]
    if window.startswith(WEEKLY_SCOPED_PREFIX):
        # Drop "Claude " prefix so families don't collapse to "Cla", append version for distinctness (e.g. Opus 4.1/4.5).
        words = window_display_name(window).split()
        if len(words) > 1 and words[0] == "Claude":
            words = words[1:]
        family = next((w for w in words if w and not w[0].isdigit()), None)
        version = next((w for w in words if w and w[0].isdigit()), None)
        if family is None:
            return (version or window[:3])[:3]
        if version:
            return family[:2] + "".join(p for p in version.split(".") if p)
        return family[:3]
    return window[:3]
