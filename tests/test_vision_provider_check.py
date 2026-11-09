"""Tests for vision provider configuration check on screen share commands."""
import unittest
from unittest.mock import Mock, patch, MagicMock

from core.screen_watch_commands import classify_screen_watch_command


class VisionProviderCheckTests(unittest.TestCase):
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

    @patch("core.screen_sharing.get_screen_sharing")
    @patch("memory.config_manager.get_cloud_vision_enabled", return_value=True)
    @patch("core.desktop_control.get_service")
    @patch("core.provider_config.get_provider_store")
    @patch("memory.config_manager.load_api_keys")
    def test_is_vision_provider_configured_with_legacy_key(self, mock_load_keys, mock_store, mock_get_service, mock_cloud_vision, mock_get_sharing):
        """Test vision provider check with legacy Gemini key."""
        from main import JarvisLive
        from ui import JarvisUI

        mock_load_keys.return_value = {"gemini_api_key": "valid_key_123456789012345"}
        mock_get_service.return_value = Mock(cloud_vision_enabled=True)

        mock_profile_store = Mock()
        mock_profile_store.list_profiles.return_value = []
        mock_profile_store.selected.return_value = None
        mock_store.return_value = mock_profile_store

        ui = Mock(spec=JarvisUI)
        ui.write_log = Mock()

        with patch("core.task_control.TaskStore"), \
             patch("core.provider_runtime.ProviderRuntime"), \
             patch("core.repair_permissions.redact"), \
             patch("core.screen_sharing.LiveScreenSharing"), \
             patch("core.click_speech.ClickSpeech"), \
             patch("actions.system_monitor.SystemMonitor"), \
             patch("actions.proactive.ProactiveEngine"), \
             patch("core.diagnostics.configure_discovery_logging"), \
             patch("core.action_loader.discover_actions"), \
             patch("core.plugin_loader.discover_plugins"), \
             patch("memory.config_manager.get_wake_word_enabled", return_value=False), \
             patch("memory.config_manager.get_voice", return_value="Charon"), \
             patch("core.provider_client.sharing_destinations", return_value={}), \
             patch("core.screen_sharing.get_screen_sharing"):

            jarvis = JarvisLive(ui)
            jarvis._allow_cloud_vision = True
            jarvis._vision_permission_generation = 0
            jarvis._vision_notices = set()

            result = jarvis._is_vision_provider_configured()
            self.assertTrue(result, "Should return True with valid legacy Gemini key")

    @patch("core.screen_sharing.get_screen_sharing")
    @patch("memory.config_manager.get_cloud_vision_enabled", return_value=True)
    @patch("core.desktop_control.get_service")
    @patch("core.provider_config.get_provider_store")
    @patch("memory.config_manager.load_api_keys")
    def test_is_vision_provider_configured_without_key(self, mock_load_keys, mock_store, mock_get_service, mock_cloud_vision, mock_get_sharing):
        """Test vision provider check without any configured key."""
        from main import JarvisLive
        from ui import JarvisUI

        mock_load_keys.return_value = {}
        mock_get_service.return_value = Mock(cloud_vision_enabled=True)

        mock_profile_store = Mock()
        mock_profile_store.list_profiles.return_value = []
        mock_profile_store.selected.return_value = None
        mock_store.return_value = mock_profile_store

        ui = Mock(spec=JarvisUI)
        ui.write_log = Mock()

        with patch("core.task_control.TaskStore"), \
             patch("core.provider_runtime.ProviderRuntime"), \
             patch("core.repair_permissions.redact"), \
             patch("core.screen_sharing.LiveScreenSharing"), \
             patch("core.click_speech.ClickSpeech"), \
             patch("actions.system_monitor.SystemMonitor"), \
             patch("actions.proactive.ProactiveEngine"), \
             patch("core.diagnostics.configure_discovery_logging"), \
             patch("core.action_loader.discover_actions"), \
             patch("core.plugin_loader.discover_plugins"), \
             patch("memory.config_manager.get_wake_word_enabled", return_value=False), \
             patch("memory.config_manager.get_voice", return_value="Charon"), \
             patch("core.provider_client.sharing_destinations", return_value={}), \
             patch("core.screen_sharing.get_screen_sharing"):

            jarvis = JarvisLive(ui)
            jarvis._allow_cloud_vision = True
            jarvis._vision_permission_generation = 0
            jarvis._vision_notices = set()

            result = jarvis._is_vision_provider_configured()
            self.assertFalse(result, "Should return False without any vision provider configured")

    @patch("core.screen_sharing.get_screen_sharing")
    @patch("memory.config_manager.get_cloud_vision_enabled", return_value=False)
    @patch("core.desktop_control.get_service")
    @patch("core.provider_config.get_provider_store")
    @patch("memory.config_manager.load_api_keys")
    def test_is_vision_provider_configured_cloud_vision_disabled(self, mock_load_keys, mock_store, mock_get_service, mock_cloud_vision, mock_get_sharing):
        """Test vision provider check when Cloud Vision is disabled."""
        from main import JarvisLive
        from ui import JarvisUI

        mock_load_keys.return_value = {"gemini_api_key": "valid_key"}
        mock_get_service.return_value = Mock(cloud_vision_enabled=False)

        mock_profile_store = Mock()
        mock_profile_store.list_profiles.return_value = []
        mock_profile_store.selected.return_value = None
        mock_store.return_value = mock_profile_store

        ui = Mock(spec=JarvisUI)
        ui.write_log = Mock()

        with patch("core.task_control.TaskStore"), \
             patch("core.provider_runtime.ProviderRuntime"), \
             patch("core.repair_permissions.redact"), \
             patch("core.screen_sharing.LiveScreenSharing"), \
             patch("core.click_speech.ClickSpeech"), \
             patch("actions.system_monitor.SystemMonitor"), \
             patch("actions.proactive.ProactiveEngine"), \
             patch("core.diagnostics.configure_discovery_logging"), \
             patch("core.action_loader.discover_actions"), \
             patch("core.plugin_loader.discover_plugins"), \
             patch("memory.config_manager.get_wake_word_enabled", return_value=False), \
             patch("memory.config_manager.get_voice", return_value="Charon"), \
             patch("core.provider_client.sharing_destinations", return_value={}), \
             patch("core.screen_sharing.get_screen_sharing"):

            jarvis = JarvisLive(ui)
            jarvis._allow_cloud_vision = False
            jarvis._vision_permission_generation = 0
            jarvis._vision_notices = set()

            result = jarvis._is_vision_provider_configured()
            self.assertFalse(result, "Should return False when Cloud Vision is disabled")


if __name__ == "__main__":
    unittest.main()