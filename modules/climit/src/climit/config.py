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


def label(window: str) -> str:
    return WINDOW_LABELS.get(window, window)
