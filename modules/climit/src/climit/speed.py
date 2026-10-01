"""Output tok/s from a Claude Code transcript, like prime-agent's /speed footer."""
import json
import os
from datetime import datetime

# Transcripts grow to 10MB+; the status line only reads the tail, so "avg" covers recent responses.
TAIL_BYTES = 1 << 20


def _ts(s) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _tail_rows(path: str):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]  # first line is cut mid-record
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def samples(rows):
    """(output_tokens, seconds) per finished assistant response, oldest first.

    A response spans from the last non-assistant entry (prompt or tool result) to its final
    streamed block, so time-to-first-token counts, as in prime-agent.
    """
    out, start, cur = [], None, None
    for r in rows:
        if r.get("isSidechain"):
            continue
        ts = _ts(r.get("timestamp"))
        if ts is None:
            continue
        if r.get("type") != "assistant":
            if cur:
                out.append(cur)
                cur = None
            start = ts
            continue
        msg = r.get("message") or {}
        mid = msg.get("id")
        if cur and cur["id"] != mid:
            out.append(cur)
            start, cur = cur["end"], None
        if cur is None and out and out[-1]["id"] == mid:
            cur = out.pop()  # tool results can land between blocks of one response
        if cur is None:
            if start is None:
                continue
            cur = {"id": mid, "start": start}
        cur["end"] = ts
        cur["tokens"] = (msg.get("usage") or {}).get("output_tokens") or 0
        cur["stop"] = msg.get("stop_reason")
    if cur and cur.get("stop"):
        out.append(cur)
    return [
        (c["tokens"], c["end"] - c["start"])
        for c in out
        if c["tokens"] > 0 and c["end"] > c["start"]
    ]


def _fmt(rate: float) -> str:
    return f"{rate:.0f}" if rate >= 100 else f"{rate:.1f}"


def render(transcript_path) -> str | None:
    """`42.1 tok/s · avg 38.0`, or None when the transcript has no usable response."""
    if not transcript_path:
        return None
    s = samples(_tail_rows(transcript_path))
    if not s:
        return None
    tok, sec = s[-1]
    last = _fmt(tok / sec) + " tok/s"
    if len(s) == 1:
        return last
    return f"{last} · avg {_fmt(sum(t for t, _ in s) / sum(d for _, d in s))}"
