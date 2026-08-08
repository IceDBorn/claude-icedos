import unittest

from climit import config


class TestWindowClassification(unittest.TestCase):
    def test_is_known_window(self):
        for w in config.WINDOW_ORDER:
            self.assertTrue(config.is_known_window(w))
        self.assertTrue(config.is_known_window(config.WEEKLY_SCOPED_PREFIX + "fable"))
        self.assertFalse(config.is_known_window("nimbus_quill"))
        self.assertFalse(config.is_known_window("iguana_necktie"))

    def test_slug_roundtrip_single_word(self):
        slug = config.weekly_scoped_slug("Fable")
        self.assertEqual(slug, "fable")
        self.assertEqual(config.window_display_name(config.WEEKLY_SCOPED_PREFIX + slug), "Fable")

    def test_slug_roundtrip_punctuated_name(self):
        slug = config.weekly_scoped_slug("Claude Opus 4.5")
        self.assertEqual(slug, "claude_opus_4.5")
        self.assertEqual(
            config.window_display_name(config.WEEKLY_SCOPED_PREFIX + slug),
            "Claude Opus 4.5",
        )

    def test_label(self):
        self.assertEqual(config.label("five_hour"), "5-hour")
        self.assertEqual(config.label("seven_day"), "weekly")
        self.assertEqual(config.label(config.WEEKLY_SCOPED_PREFIX + "fable"), "week · Fable")
        self.assertEqual(config.label("nimbus_quill"), "nimbus_quill")

    def test_short_label(self):
        self.assertEqual(config.short_label("five_hour"), "5h")
        self.assertEqual(config.short_label("seven_day_sonnet"), "son")
        self.assertEqual(config.short_label(config.WEEKLY_SCOPED_PREFIX + "fable"), "Fab")
        # "Claude " prefix is dropped so distinct model families don't all
        # collapse to the same "Cla" token
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_opus"),
            "Opu",
        )
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_opus_4.1"),
            "Op41",
        )
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_opus_4.5"),
            "Op45",
        )
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_sonnet_4.5"),
            "So45",
        )
        # version-first naming must not collapse either ("Claude 3.5 Sonnet"
        # and "Claude 3.5 Haiku" would both short to "3.5")
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_3.5_sonnet"),
            "So35",
        )
        self.assertEqual(
            config.short_label(config.WEEKLY_SCOPED_PREFIX + "claude_3.5_haiku"),
            "Ha35",
        )
        # a blank slug must not crash (defensive; sources rejects blank names)
        self.assertEqual(config.short_label(config.WEEKLY_SCOPED_PREFIX), "wee")


if __name__ == "__main__":
    unittest.main()
