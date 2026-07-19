import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from climit import contrib

NOW = 1_800_000_000_000  # fixed fake "now" (ms)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class TestContrib(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        d = self.root / "-home-ice-proj"
        d.mkdir()
        ts = iso(NOW - 3_600_000)  # 1h ago, inside a 24h window
        lines = [
            # main turn, attributed to MCP 'icedos' + skill 'memory-first', low context
            {"type": "assistant", "sessionId": "s1", "timestamp": ts,
             "attributionMcpServer": "icedos", "attributionSkill": "memory-first",
             "message": {"usage": {"input_tokens": 1000, "output_tokens": 1000,
                                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
            # main turn at >150k context (200k cache_read)
            {"type": "assistant", "sessionId": "s1", "timestamp": ts,
             "message": {"usage": {"input_tokens": 0, "output_tokens": 0,
                                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 200000}}},
            # subagent completion (Explore) — cost via toolUseResult aggregate
            {"type": "user", "sessionId": "s1", "timestamp": ts,
             "toolUseResult": {"agentType": "Explore",
                               "usage": {"input_tokens": 0, "output_tokens": 2000,
                                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
        ]
        (d / "sess.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_analyze_weights_and_pcts(self):
        acc = contrib.analyze(hours=24, now_ms=NOW, root=self.root, skip_old_files=False)
        # weights: A=6000 (1000 in + 1000 out*5), B=20000 (200k read*0.1), C=10000 (2000 out*5)
        self.assertAlmostEqual(acc["total"], 36000.0)
        self.assertAlmostEqual(acc["main"], 26000.0)
        self.assertAlmostEqual(acc["subagent"], 10000.0)
        j = json.loads(contrib.to_json(acc, 24))
        self.assertEqual(j["subagent_pct"], 28)          # 10000/36000
        self.assertEqual(j["context_over_150k_pct"], 56)  # 20000/36000
        self.assertEqual(j["mcp"]["icedos"], 17)          # 6000/36000
        self.assertEqual(j["skills"]["memory-first"], 17)
        self.assertEqual(j["subagents"]["Explore"], 28)
        self.assertEqual(j["subagent_heavy_pct"], 0)      # sub share 0.28 < 0.5

    def test_time_window_excludes_old(self):
        # cutoff = (NOW+2h) - 1h = NOW+1h; all fixture lines are at NOW-1h → excluded
        acc = contrib.analyze(hours=1, now_ms=NOW + 2 * 3_600_000, root=self.root, skip_old_files=False)
        self.assertEqual(acc["total"], 0.0)
        self.assertEqual(contrib.render(acc, 1), "no local usage found in the last 1h.")


if __name__ == "__main__":
    unittest.main()
