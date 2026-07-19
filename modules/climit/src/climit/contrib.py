"""Approximate "what's contributing to your usage" analysis, from local Claude
Code session transcripts (``~/.claude/projects/<cwd>/<session>.jsonl``).

Mirrors the breakdown Claude Code's ``/usage`` screen shows. Local + approximate:
- subagent transcripts aren't inlined (this build), so a subagent's cost is read
  from the ``toolUseResult`` aggregate on the spawning turn;
- per-MCP-server / per-Skill use Claude Code's own precomputed attribution fields
  (``attributionMcpServer`` / ``attributionSkill``) rather than name-parsing.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Cost-ish weights in input-token-equivalents (rough Opus economics). Percentages
# are relative, so only the ratios between these matter.
W_OUT, W_IN, W_CACHE_CREATE, W_CACHE_READ = 5.0, 1.0, 1.25, 0.1
CONTEXT_HIGH = 150_000


def _weight(usage) -> float:
    if not isinstance(usage, dict):
        return 0.0
    return (
        usage.get("output_tokens", 0) * W_OUT
        + usage.get("input_tokens", 0) * W_IN
        + usage.get("cache_creation_input_tokens", 0) * W_CACHE_CREATE
        + usage.get("cache_read_input_tokens", 0) * W_CACHE_READ
    )


def _context(usage) -> int:
    if not isinstance(usage, dict):
        return 0
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )


def _ts_ms(iso) -> int | None:
    if not isinstance(iso, str):
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def analyze(
    hours: int = 24,
    now_ms: int | None = None,
    root: Path | None = None,
    skip_old_files: bool = True,
) -> dict:
    """Scan recent session logs and accumulate weighted usage attribution."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    cutoff = now - hours * 3_600_000
    root = root or (Path.home() / ".claude" / "projects")

    total = main = subagent = ctx_over = 0.0
    by_mcp: dict[str, float] = defaultdict(float)
    by_skill: dict[str, float] = defaultdict(float)
    by_agent: dict[str, float] = defaultdict(float)
    sessions: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])  # [main, sub]

    for path in sorted(root.glob("*/*.jsonl")):
        if skip_old_files:
            try:
                if path.stat().st_mtime * 1000 < cutoff:
                    continue  # untouched within the window — skip for speed
            except OSError:
                continue
        try:
            fh = open(path, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _ts_ms(obj.get("timestamp"))
                if ts is not None and ts < cutoff:
                    continue
                typ = obj.get("type")
                sid = obj.get("sessionId", "?")
                if typ == "assistant":
                    usage = (obj.get("message") or {}).get("usage") or {}
                    w = _weight(usage)
                    if w <= 0:
                        continue
                    total += w
                    main += w
                    sessions[sid][0] += w
                    if _context(usage) > CONTEXT_HIGH:
                        ctx_over += w
                    if obj.get("attributionMcpServer"):
                        by_mcp[obj["attributionMcpServer"]] += w
                    if obj.get("attributionSkill"):
                        by_skill[obj["attributionSkill"]] += w
                elif typ == "user":
                    tur = obj.get("toolUseResult")
                    if isinstance(tur, dict) and tur.get("agentType"):
                        u = tur.get("usage") or {}
                        w = _weight(u) if u else float(tur.get("totalTokens", 0) or 0)
                        if w <= 0:
                            continue
                        total += w
                        subagent += w
                        by_agent[tur["agentType"]] += w
                        sessions[sid][1] += w

    heavy = sum(m + s for m, s in sessions.values() if (m + s) > 0 and s / (m + s) > 0.5)
    return {
        "hours": hours,
        "total": total,
        "main": main,
        "subagent": subagent,
        "subagent_heavy": heavy,
        "ctx_over": ctx_over,
        "by_mcp": dict(by_mcp),
        "by_skill": dict(by_skill),
        "by_agent": dict(by_agent),
        "sessions": len(sessions),
    }


def _pct(x: float, total: float) -> int:
    return round(100 * x / total) if total > 0 else 0


def _table(title: str, items: dict, total: float) -> list[str]:
    rows = sorted(((k, v) for k, v in items.items() if _pct(v, total) >= 1), key=lambda kv: -kv[1])
    if not rows:
        return []
    out = [f"{title:<22}% of usage"]
    out += [f"{name:<22}{_pct(w, total):>3}%" for name, w in rows]
    return out


def render(acc: dict, hours: int | None = None) -> str:
    hours = hours or acc.get("hours", 24)
    total = acc["total"]
    if total <= 0:
        return f"no local usage found in the last {hours}h."
    lines = [
        "What's contributing to your limits usage?",
        f"Approximate, based on local sessions on this machine — last {hours}h · "
        "independent characteristics of your usage, not a breakdown.",
        "",
    ]
    tips = []
    if (p := _pct(acc["subagent_heavy"], total)):
        tips.append(f"{p}% of your usage came from subagent-heavy sessions")
    if (p := _pct(acc["ctx_over"], total)):
        tips.append(f"{p}% of your usage was at >150k context")
    if acc["by_mcp"]:
        name, w = max(acc["by_mcp"].items(), key=lambda kv: kv[1])
        if (p := _pct(w, total)):
            tips.append(f'{p}% of your usage came from MCP server "{name}"')
    if tips:
        lines += tips + [""]
    for block in (
        _table("MCP servers", acc["by_mcp"], total),
        _table("Skills", acc["by_skill"], total),
        _table("Subagents", acc["by_agent"], total),
    ):
        if block:
            lines += block + [""]
    return "\n".join(lines).rstrip()


def to_json(acc: dict, hours: int | None = None) -> str:
    total = acc["total"]
    return json.dumps(
        {
            "hours": hours or acc.get("hours"),
            "sessions": acc["sessions"],
            "subagent_heavy_pct": _pct(acc["subagent_heavy"], total),
            "context_over_150k_pct": _pct(acc["ctx_over"], total),
            "subagent_pct": _pct(acc["subagent"], total),
            "mcp": {k: _pct(v, total) for k, v in acc["by_mcp"].items()},
            "skills": {k: _pct(v, total) for k, v in acc["by_skill"].items()},
            "subagents": {k: _pct(v, total) for k, v in acc["by_agent"].items()},
        },
        indent=2,
    )
