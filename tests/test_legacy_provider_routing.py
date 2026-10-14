"""Exercise small legacy-client boundaries without importing desktop side effects."""
import ast
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CALLERS = ("actions/code_helper.py", "actions/desktop.py", "actions/computer_settings.py",
           "actions/dev_agent.py", "actions/file_processor.py", "actions/flight_finder.py",
           "actions/youtube_video.py", "actions/web_search.py", "actions/image_generator.py",
           "plugins/form_agent.py", "plugins/form_agent_v2.py", "plugins/game_control.py",
           "core/long_horizon_agent.py", "core/app_builder/engine.py")


def load_function(path, name, **namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    namespace.setdefault("_get_api_key", mock.Mock(side_effect=AssertionError("Legacy key preflight must not run")))
    namespace.setdefault("Path", Path)
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), "exec"), namespace)
    return namespace[name]


class LegacyProviderRoutingTests(unittest.TestCase):
    def test_owned_tools_do_not_read_plaintext_gemini_fields(self):
        for path in CALLERS:
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding="utf-8")
                tree = ast.parse(source)
                keys = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                        and isinstance(node.value, str) and node.value in {"gemini_api_key", "GEMINI_API_KEY"}]
                self.assertEqual(keys, [], "Tool key reads must use the encrypted compatibility facade")

    def test_code_helpers_use_coding_override_without_gemini_preflight(self):
        for path, name, kwargs, args in (("actions/code_helper.py", "_get_gemini", {"GEMINI_MODEL": "old-model"}, ()),
                                         ("actions/dev_agent.py", "_get_model", {}, ("old-model",))):
            with self.subTest(path=path):
                factory = mock.Mock()
                factory.return_value.models.generate_content.return_value = types.SimpleNamespace(text="generated-code")
                with mock.patch("core.provider_client.configured_client", factory):
                    result = load_function(path, name, **kwargs)(*args).generate_content("request")
                self.assertEqual(result.text, "generated-code")
                factory.assert_called_once_with(role="chat", task_kind="coding")

    def test_file_image_analysis_selects_vision_while_text_selects_chat(self):
        function = load_function("actions/file_processor.py", "_gemini_client")
        with mock.patch("core.provider_client.configured_client") as factory:
            function(role="vision").generate_content("image-question")
            factory.assert_called_with(role="vision", task_kind="file_processing")
            function().generate_content("text-question")
            factory.assert_called_with(role="chat", task_kind="file_processing")

    def test_key_getters_delegate_without_reading_local_json(self):
        for path, name, args in (("actions/code_helper.py", "_get_api_key", ()),
                                 ("actions/computer_settings.py", "_get_api_key", ()),
                                 ("plugins/form_agent.py", "_get_api_key", ()),
                                 ("plugins/form_agent_v2.py", "_key", ()),
                                 ("core/app_builder/engine.py", "_api_key", (Path("unused"),))):
            with self.subTest(path=path), mock.patch("memory.config_manager.get_gemini_key", return_value="synthetic-secure-key"):
                function = load_function(path, name, API_CONFIG_PATH=Path("must-not-read.json"))
                self.assertEqual(function(*args), "synthetic-secure-key")

    def test_form_start_remains_disabled_after_provider_routing(self):
        for path in ("plugins/form_agent.py", "plugins/form_agent_v2.py"):
            with self.subTest(path=path), mock.patch("core.provider_client.configured_client") as factory:
                result = load_function(path, "run")({"action": "start"})
                self.assertIn("disabled", result)
                factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
