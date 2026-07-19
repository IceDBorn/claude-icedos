"""climit — Claude usage-limit tracker.

Pulls the same rate-limit data Claude Code's /usage shows (5h session + weekly
windows), stores a time series, and reports burn rate (%/min·hour·day) plus
projected exhaustion vs. window reset.
"""

__version__ = "0.1.0"
