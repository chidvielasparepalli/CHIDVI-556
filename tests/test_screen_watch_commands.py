import unittest

from core.screen_watch_commands import classify_screen_watch_command


class ScreenWatchCommandTests(unittest.TestCase):
    def test_explicit_start_phrases(self):
        cases = (
            "see my screen",
            "CHIDVI, see my screen and tell me what is wrong",
            "Can you watch my screen?",
            "could you look at my screen",
            "start screen sharing",
            "please start sharing my screen",
            "start watching my screen",
            "I want you to see my screen",
            "Hey Jarvis, turn on screen sharing",
        )
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertEqual(classify_screen_watch_command(phrase), "start")

    def test_explicit_stop_phrases(self):
        cases = (
            "stop watching my screen",
            "stop screen sharing",
            "please stop sharing my screen",
            "CHIDVI, stop watching my screen now",
            "end screen sharing",
            "pause watching my screen",
        )
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertEqual(classify_screen_watch_command(phrase), "stop")

    def test_mentions_and_negated_requests_do_not_control_sharing(self):
        cases = (
            "I can see my screen",
            "I need to share my screen later",
            "don't see my screen",
            "do not share my screen",
            "don't stop watching my screen",
            "can you see my screen, I do not want to share it",
            "show me how screen sharing works",
            "stop writing on my screen",
            "",
        )
        for phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertIsNone(classify_screen_watch_command(phrase))


if __name__ == "__main__":
    unittest.main()
