import json
import tempfile
import unittest

from climit import speed


def _a(ts, mid, out, stop="end_turn", **kw):
    return {"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "stop_reason": stop, "usage": {"output_tokens": out}}, **kw}


def _u(ts):
    return {"type": "user", "timestamp": ts}


class TestSamples(unittest.TestCase):
    def test_spans_from_prompt_to_last_block(self):
        rows = [_u("2026-10-01T10:00:00Z"), _a("2026-10-01T10:00:01Z", "m1", 100),
                _a("2026-10-01T10:00:02Z", "m1", 100, "tool_use")]
        self.assertEqual(speed.samples(rows), [(100, 2.0)])

    def test_tool_result_starts_next_response(self):
        rows = [_u("2026-10-01T10:00:00Z"), _a("2026-10-01T10:00:02Z", "m1", 50, "tool_use"),
                _u("2026-10-01T10:00:05Z"), _a("2026-10-01T10:00:09Z", "m2", 200)]
        self.assertEqual(speed.samples(rows), [(50, 2.0), (200, 4.0)])

    def test_interleaved_tool_result_stays_one_response(self):
        rows = [_u("2026-10-01T10:00:00Z"), _a("2026-10-01T10:00:02Z", "m1", 80, "tool_use"),
                _u("2026-10-01T10:00:03Z"), _a("2026-10-01T10:00:04Z", "m1", 80, "tool_use")]
        self.assertEqual(speed.samples(rows), [(80, 4.0)])

    def test_skips_sidechain_and_zero_tokens(self):
        rows = [_u("2026-10-01T10:00:00Z"), _a("2026-10-01T10:00:01Z", "s", 99, isSidechain=True),
                _a("2026-10-01T10:00:02Z", "m1", 0)]
        self.assertEqual(speed.samples(rows), [])

    def test_attachment_rows_do_not_restart_span(self):
        # Claude Code writes attachment/system rows in the same millisecond as the response.
        rows = [_u("2026-10-01T10:00:00Z"), {"type": "attachment", "timestamp": "2026-10-01T10:00:04.900Z"},
                _a("2026-10-01T10:00:05Z", "m1", 100),
                {"type": "system", "timestamp": "2026-10-01T10:00:05.010Z"}]
        self.assertEqual(speed.samples(rows), [(100, 5.0)])

class TestRender(unittest.TestCase):
    def _file(self, rows):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        f.close()
        return f.name

    def test_last_and_avg(self):
        path = self._file([_u("2026-10-01T10:00:00Z"), _a("2026-10-01T10:00:02Z", "m1", 100, "tool_use"),
                           _u("2026-10-01T10:00:03Z"), _a("2026-10-01T10:00:05Z", "m2", 300)])
        self.assertEqual(speed.render(path), "󰓅 150 · 󰾟 100")

    def test_avg_hidden_when_close(self):
        path = self._file([_u("2026-10-01T10:00:00Z"), _a("2026-10-01T10:00:02Z", "m1", 100, "tool_use"),
                           _u("2026-10-01T10:00:03Z"), _a("2026-10-01T10:00:05Z", "m2", 110)])
        self.assertEqual(speed.render(path), "󰓅 55.0")

    def test_missing_path(self):
        self.assertIsNone(speed.render(None))
        self.assertIsNone(speed.render("/nonexistent.jsonl"))
