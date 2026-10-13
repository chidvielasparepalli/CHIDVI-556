import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QApplication

from Personality import loader
from personality_icons import personality_icon


ROOT = Path(__file__).resolve().parents[1]


def pixels(pixmap):
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    return image.width(), image.height(), image.constBits().asstring(image.sizeInBytes())


class PersonalityIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.profiles = loader.discover_personalities()

    def test_every_discovered_personality_has_a_distinct_icon(self):
        rendered = set()
        for personality_id, profile in self.profiles.items():
            with self.subTest(personality_id=personality_id):
                icon = personality_icon(personality_id, profile)
                self.assertFalse(icon.isNull())
                self.assertNotIn(pixels(icon), rendered)
                rendered.add(pixels(icon))

    def test_valid_existing_portraits_are_preferred(self):
        for personality_id in ("tony", "hinata", "levi", "wednesday", "frieren"):
            with self.subTest(personality_id=personality_id):
                profile = self.profiles[personality_id]
                original = QPixmap(str(ROOT / profile["avatar"]["asset"]))
                self.assertFalse(original.isNull())
                icon = personality_icon(personality_id, profile)
                scaled = original.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                         Qt.TransformationMode.SmoothTransformation)
                self.assertEqual(icon.toImage().pixelColor(256, 256),
                                 scaled.toImage().pixelColor(scaled.width() // 2, scaled.height() // 2))
                self.assertEqual(icon.toImage().pixelColor(0, 0).alpha(), 0)
                self.assertEqual(icon.width(), icon.height())
                self.assertNotEqual(pixels(icon), pixels(personality_icon(personality_id, {**profile, "avatar": {}})))

    def test_incomplete_project_jpegs_use_themed_emblems(self):
        for personality_id in ("gojo", "makise", "deadpool", "naruto"):
            with self.subTest(personality_id=personality_id):
                profile = self.profiles[personality_id]
                fallback_profile = {**profile, "avatar": {}}
                self.assertEqual(pixels(personality_icon(personality_id, profile)),
                                 pixels(personality_icon(personality_id, fallback_profile)))

    def test_missing_corrupt_and_unreadable_assets_use_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "corrupt.png").write_bytes(b"not an image")
            (root / "folder.png").mkdir()
            profile = {"name": "Future Pilot", "theme": {"primary": "#ba64dc"}}
            expected = pixels(personality_icon("future_pilot", profile))
            for asset in ("missing.png", "corrupt.png", "folder.png"):
                with self.subTest(asset=asset):
                    actual = personality_icon("future_pilot", {**profile, "avatar": {"asset": asset}}, root)
                    self.assertEqual(pixels(actual), expected)

    def test_truncated_jpeg_is_rejected_even_if_qt_can_decode_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portrait = (ROOT / self.profiles["tony"]["avatar"]["asset"]).read_bytes()
            (root / "truncated.jpg").write_bytes(portrait[:-2])
            profile = {"name": "Future Pilot", "avatar": {"asset": "truncated.jpg"}}
            self.assertEqual(pixels(personality_icon("future_pilot", profile, root)),
                             pixels(personality_icon("future_pilot", {"name": "Future Pilot"})))

    def test_new_profile_is_discovered_and_gets_a_stable_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "future_pilot.py").write_text(
                "PROFILE = {'name': 'Future Pilot', 'description': 'New persona', "
                "'interaction': {}, 'voice': {}, 'theme': {}}", encoding="utf-8")
            with patch.object(loader, "_PERSONA_DIR", root):
                profiles = loader.discover_personalities()
            self.assertIn("future_pilot", profiles)
            icon = personality_icon("future_pilot", profiles["future_pilot"])
            self.assertFalse(icon.isNull())
            self.assertGreaterEqual(icon.width(), 256)
            self.assertEqual(pixels(icon), pixels(personality_icon("future_pilot", profiles["future_pilot"])))

    def test_icon_identity_does_not_depend_on_discovery_order(self):
        forward = {key: pixels(personality_icon(key, profile)) for key, profile in self.profiles.items()}
        reverse = {key: pixels(personality_icon(key, profile)) for key, profile in reversed(self.profiles.items())}
        self.assertEqual(forward, reverse)

    def test_unknown_profiles_with_the_same_theme_still_have_distinct_emblems(self):
        profile = {"name": "New Personality", "theme": {"primary": "#123456"}}
        self.assertNotEqual(pixels(personality_icon("future_pilot", profile)),
                            pixels(personality_icon("future_engineer", profile)))

    def test_sherlock_and_goku_have_distinct_emblems(self):
        sherlock = personality_icon("sherlock", {"name": "Sherlock Holmes"})
        goku = personality_icon("goku", {"name": "Goku"})
        self.assertFalse(sherlock.isNull())
        self.assertFalse(goku.isNull())
        self.assertNotEqual(pixels(sherlock), pixels(goku))


if __name__ == "__main__":
    unittest.main()
