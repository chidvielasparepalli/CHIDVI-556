"""Capture validation uses synthetic pixels; no real desktop or cloud is accessed."""
from contextlib import contextmanager
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from actions import screen_processor as screen


class ScreenCaptureTests(unittest.TestCase):
    def setUp(self):
        self.monitors = [
            {"left": -20, "top": 0, "width": 60, "height": 30},
            {"left": 0, "top": 0, "width": 40, "height": 30},
            {"left": -20, "top": 0, "width": 20, "height": 20},
        ]
        self.dpi_active = False

        @contextmanager
        def physical_coordinates():
            self.dpi_active = True
            try:
                yield
            finally:
                self.dpi_active = False

        def grab(rect):
            self.assertTrue(self.dpi_active, "Capture must run inside the physical DPI context")
            image = Image.new("RGB", (rect["width"], rect["height"]), "black")
            image.putpixel((0, 0), (255, 255, 255))
            return SimpleNamespace(rgb=image.tobytes(), size=image.size)

        self.capture = SimpleNamespace(monitors=self.monitors, grab=Mock(side_effect=grab))
        context = Mock()
        context.__enter__ = Mock(return_value=self.capture)
        context.__exit__ = Mock(return_value=False)
        self.mss_patch = patch.object(screen.mss, "mss", return_value=context)
        self.dpi_patch = patch.object(screen, "physical_coordinates", side_effect=physical_coordinates, create=True)
        self.mss_patch.start()
        self.dpi_patch.start()
        self.addCleanup(self.mss_patch.stop)
        self.addCleanup(self.dpi_patch.stop)

    def capture_screen(self, *args):
        self.assertTrue(callable(getattr(screen, "capture_screen", None)),
                        "A separate local capture operation must expose verified image metadata")
        return screen.capture_screen(*args)

    def test_local_capture_is_full_resolution_png_without_cloud_or_disk(self):
        with patch.object(screen, "_load_config", side_effect=AssertionError("Capture must not need cloud config")), \
                patch.object(screen.Path, "write_bytes", side_effect=AssertionError("Capture must stay in memory")):
            result = self.capture_screen()
        self.assertEqual(result["monitor"], 1)
        self.assertEqual(result["source_rect"], self.monitors[1])
        self.assertEqual((result["width"], result["height"]), (40, 30))
        self.assertEqual(result["mime_type"], "image/png")
        with Image.open(io.BytesIO(result["data"])) as image:
            self.assertEqual(image.size, (40, 30))
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertFalse(self.dpi_active)

    def test_explicit_monitor_and_virtual_desktop_preserve_negative_origins(self):
        for index in (0, 2):
            with self.subTest(monitor=index):
                result = self.capture_screen(index)
                self.assertEqual(result["monitor"], index)
                self.assertEqual(result["source_rect"], self.monitors[index])
                self.assertEqual((result["width"], result["height"]),
                                 (self.monitors[index]["width"], self.monitors[index]["height"]))
                self.capture.grab.assert_called_with(self.monitors[index])

    def test_invalid_monitor_selection_never_grabs_pixels(self):
        for index in (-1, 3, True, 1.5, "1", None):
            with self.subTest(monitor=index), self.assertRaises(ValueError):
                self.capture_screen(index)
        self.capture.grab.assert_not_called()

    def test_no_physical_monitor_fails_before_capture(self):
        for monitors in ([], [self.monitors[0]]):
            self.capture.monitors = monitors
            with self.subTest(monitors=monitors), self.assertRaisesRegex(RuntimeError, "monitor|display"):
                self.capture_screen()
        self.capture.grab.assert_not_called()

    def test_malformed_display_dimensions_never_grab_pixels(self):
        for width, height in ((0, 30), (40, -1), (True, 30), (40, 1.5)):
            self.capture.monitors = [self.monitors[0], {"left": 0, "top": 0, "width": width, "height": height}]
            with self.subTest(size=(width, height)), self.assertRaisesRegex(RuntimeError, "dimensions"):
                self.capture_screen()
        self.capture.grab.assert_not_called()

    def test_wrong_pixel_dimensions_or_empty_rgb_are_rejected(self):
        for shot in (SimpleNamespace(rgb=b"\x00" * 30, size=(5, 2)),
                     SimpleNamespace(rgb=b"", size=(40, 30))):
            self.capture.grab.side_effect = None
            self.capture.grab.return_value = shot
            with self.subTest(shot_size=shot.size), self.assertRaisesRegex(RuntimeError, "dimensions|empty|pixels"):
                self.capture_screen()

    def test_invalid_encoded_image_is_not_returned_as_success(self):
        for data in (b"", b"not an image"):
            with self.subTest(data=data), patch.object(screen.mss.tools, "to_png", return_value=data), \
                    self.assertRaisesRegex(RuntimeError, "empty|valid|decode"):
                self.capture_screen()

    def test_decoded_dimensions_must_match_selected_physical_display(self):
        out = io.BytesIO()
        image = Image.new("RGB", (20, 15), "black")
        image.putpixel((0, 0), (255, 255, 255))
        image.save(out, format="PNG")
        with patch.object(screen.mss.tools, "to_png", return_value=out.getvalue()), \
                self.assertRaisesRegex(RuntimeError, "dimensions"):
            self.capture_screen()

    def test_uniform_frames_are_reported_as_blank(self):
        for color in ("black", "white", "blue"):
            image = Image.new("RGB", (40, 30), color)
            self.capture.grab.side_effect = None
            self.capture.grab.return_value = SimpleNamespace(rgb=image.tobytes(), size=image.size)
            with self.subTest(color=color), self.assertRaisesRegex(RuntimeError, "blank|uniform"):
                self.capture_screen()

    def test_legacy_capture_tuple_uses_validated_capture_and_compression(self):
        image = Image.new("RGB", (1600, 900), "black")
        image.putpixel((0, 0), (255, 255, 255))
        out = io.BytesIO()
        image.save(out, format="PNG")
        with patch.object(screen, "capture_screen", return_value={"data": out.getvalue(), "mime_type": "image/png"},
                          create=True) as capture:
            data, mime = screen._capture_screen()
        capture.assert_called_once_with()
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(data)) as encoded:
            self.assertEqual(encoded.size, (1280, 720))


if __name__ == "__main__":
    unittest.main()
