import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.observation import ObservationController, _navigation


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.observer = Mock(return_value={"application": "chrome.exe", "title": "Example", "window_id": 42})
        self.status = Mock()
        self.controller = ObservationController(self.observer, clock=lambda: self.now, on_status=self.status)
        self.battery = patch("core.observation.psutil.sensors_battery", return_value=SimpleNamespace(power_plugged=True))
        self.cpu = patch("core.observation.psutil.cpu_percent", return_value=0)
        self.memory = patch("core.observation.psutil.virtual_memory", return_value=SimpleNamespace(percent=25))
        self.battery_mock = self.battery.start()
        self.cpu_mock = self.cpu.start()
        self.memory.start()
        self.addCleanup(self.battery.stop)
        self.addCleanup(self.cpu.stop)
        self.addCleanup(self.memory.stop)
        self.addCleanup(self.controller.set_mode, "stopped")

    def start(self, proactive=False):
        self.controller.configure(proactive=proactive)
        self.controller.set_mode("running")

    def tick(self, seconds=16, **kwargs):
        self.now += seconds
        return self.controller.poll(**kwargs)

    def test_stopped_and_paused_never_read_or_retain_context(self):
        self.controller.notify_navigation("https://example.test/?private=1", "Example")
        self.assertIsNone(self.controller.poll())
        self.observer.assert_not_called()
        self.battery_mock.assert_not_called()
        self.assertEqual(self.controller.snapshot()["context"], {})
        self.start()
        self.assertIsNotNone(self.controller.poll())
        self.controller.set_mode("paused")
        self.observer.reset_mock()
        self.assertIsNone(self.tick())
        self.observer.assert_not_called()
        self.assertEqual(self.controller.snapshot()["context"], {})

    def test_mode_validation_and_event_hook_cleanup(self):
        with self.assertRaises(ValueError):
            self.controller.set_mode("recording")
        with self.assertRaises(ValueError):
            self.controller.configure(ac_only="yes")
        with patch("core.observation._ForegroundEvents") as hook, patch("core.observation.os.name", "nt"):
            real_observer_controller = ObservationController()
            real_observer_controller.set_mode("running")
            hook.return_value.start.assert_called_once()
            real_observer_controller.set_mode("paused")
            hook.return_value.stop.assert_called_once()

    def test_ac_only_suspend_and_resume(self):
        self.battery_mock.return_value.power_plugged = False
        self.start()
        self.assertIsNone(self.controller.poll())
        self.observer.assert_not_called()
        self.assertIn("AC power", self.controller.snapshot()["status"])
        self.battery_mock.return_value.power_plugged = True
        self.assertIsNotNone(self.tick(5))

    def test_power_diagnostic_failure_fails_closed_but_setting_can_disable_gate(self):
        self.battery_mock.side_effect = OSError("do not expose this secret")
        self.start()
        self.assertIsNone(self.controller.poll())
        self.observer.assert_not_called()
        self.assertNotIn("secret", str(self.controller.snapshot()))
        self.controller.configure(ac_only=False)
        self.assertIsNotNone(self.tick(1))

    def test_no_battery_is_desktop_ac_and_high_cpu_defers_work(self):
        self.battery_mock.return_value = None
        self.cpu_mock.return_value = 95
        self.start()
        self.assertIsNone(self.controller.poll())
        self.observer.assert_not_called()
        self.assertIn("resources", self.controller.snapshot()["status"])
        self.cpu_mock.return_value = 0
        self.assertIsNotNone(self.tick(5))

    def test_locked_and_sensitive_windows_clear_previous_context(self):
        self.start()
        self.controller.poll()
        for raw in ({"locked": True, "title": "private"},
                    {"application": "LogonUI.exe", "title": "Welcome"},
                    {"application": "chrome.exe", "title": "Sign in - 123456"},
                    {"application": "chrome.exe", "title": "secret content", "sensitive": True}):
            self.observer.return_value = raw
            self.assertIsNone(self.tick())
            self.assertEqual(self.controller.snapshot()["context"], {})
            self.assertNotIn("123456", str(self.controller.snapshot()))

    def test_navigation_strips_credentials_query_fragment_and_long_secrets(self):
        self.assertEqual(_navigation("https://example.test/docs?q=PRIVATE#CODE", "Example")["url"], "https://example.test/docs")
        for url in ("https://user:password@example.test/", "https://example.test/oauth/callback",
                    "https://example.test/" + "x" * 40, "file:///C:/private.txt", "http://[broken"):
            self.assertEqual(_navigation(url, ""), {"sensitive": True})
        self.assertEqual(_navigation("https://example.test/", "Example https://elsewhere.test/?q=PRIVATE")["page_title"], "Example [URL]")

    def test_sensitive_navigation_remains_suspended_until_new_safe_event(self):
        self.start()
        self.controller.notify_navigation("https://example.test/auth", "Welcome")
        self.assertIsNone(self.controller.poll())
        self.assertIsNone(self.tick(5))
        self.assertEqual(self.controller.snapshot()["context"], {})
        self.controller.notify_navigation("https://example.test/docs", "Docs")
        self.assertIsNotNone(self.tick(5))

    def test_no_form_values_and_title_urls_sanitized(self):
        self.observer.return_value = {"application": "chrome.exe", "title": "Example https://example.test/?q=PRIVATE",
                                      "window_id": 1, "password": "PRIVATE", "fields": {"value": "PRIVATE"}}
        self.start()
        result = self.controller.poll()
        self.assertNotIn("PRIVATE", str(result))
        self.assertNotIn("fields", str(result))
        self.assertNotIn("password", str(result))

    def test_proactive_dedup_and_global_cooldown(self):
        self.start(proactive=True)
        self.controller.notify_navigation("https://one.test/a", "Page")
        self.assertIn("greeting", self.controller.poll())
        self.controller.notify_navigation("https://one.test/b", "Next")
        self.assertNotIn("greeting", self.tick(2))
        self.controller.notify_navigation("https://two.test/", "Next")
        self.assertNotIn("greeting", self.tick(2))
        self.assertIn("greeting", self.tick(120))
        self.controller.notify_navigation("https://one.test/again", "Again")
        self.assertNotIn("greeting", self.tick(120))

    def test_busy_or_speaking_never_interrupts_and_can_offer_once_idle(self):
        self.start(proactive=True)
        result = self.controller.poll(busy=True)
        self.assertNotIn("greeting", result)
        self.assertIn("task_status", result)
        self.assertIsNone(self.tick(speaking=True))
        self.assertIn("greeting", self.tick())
        self.assertIsNone(self.tick(130))

    def test_only_supported_apps_offer_help(self):
        self.observer.return_value = {"application": "unknown.exe", "title": "Example"}
        self.start(proactive=True)
        self.assertNotIn("greeting", self.controller.poll())

    def test_adaptive_polling_is_bounded_even_with_event_flood(self):
        self.start()
        self.controller.poll()
        for _ in range(50):
            self.controller._dirty.set()
            self.tick(0.02)
        self.assertLessEqual(self.observer.call_count, 3)
        self.observer.reset_mock()
        for _ in range(100):
            self.tick(0.1)
        self.assertLessEqual(self.observer.call_count, 5)

    def test_errors_back_off_and_recover_without_exposing_details(self):
        self.observer.side_effect = [RuntimeError("SECRET"), RuntimeError("SECRET"), {"application": "notepad.exe", "title": "Notes"}]
        self.start()
        self.assertIsNone(self.controller.poll())
        self.assertIsNone(self.tick(1))
        self.assertEqual(self.observer.call_count, 1)
        self.assertIsNone(self.tick(1))
        self.assertIsNone(self.tick(2))
        self.assertEqual(self.observer.call_count, 2)
        self.assertIsNotNone(self.tick(2))
        self.assertNotIn("SECRET", str(self.controller.snapshot()))

    def test_clear_is_immediate_and_snapshot_is_a_copy(self):
        self.start()
        self.controller.poll()
        snapshot = self.controller.snapshot()
        snapshot["context"]["title"] = "tampered"
        self.assertNotEqual(self.controller.snapshot()["context"]["title"], "tampered")
        self.controller.clear()
        self.assertEqual(self.controller.snapshot()["context"], {})
        self.assertFalse(self.controller.snapshot()["stores_images"])
        self.assertFalse(self.controller.snapshot()["external_analysis"])

    def test_inflight_observation_is_discarded_after_stop(self):
        started, release = threading.Event(), threading.Event()
        def observe():
            started.set()
            release.wait(2)
            return {"application": "chrome.exe", "title": "Late result"}
        self.observer.side_effect = observe
        self.start()
        results = []
        worker = threading.Thread(target=lambda: results.append(self.controller.poll()))
        worker.start()
        self.assertTrue(started.wait(1))
        self.controller.set_mode("stopped")
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual(self.controller.snapshot()["context"], {})


if __name__ == "__main__":
    unittest.main()
