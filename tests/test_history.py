"""Tests for dependency-free history archive helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "history.py"
)
SPEC = importlib.util.spec_from_file_location("orvibohomebridge_history", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
history = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = history
SPEC.loader.exec_module(history)


class HistoryDirTests(unittest.TestCase):
    def test_dir_created_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-77c139c4d27f4fa6a20e1f459849aa47")
            self.assertTrue(d.is_dir())
            self.assertEqual(
                d, root / "orvibohomebridge" / "w-77c139c4d27f4fa6a20e1f459849aa47"
            )


class SaveSnapshotTests(unittest.TestCase):
    def test_save_and_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-lock")
            p1 = history.save_snapshot(d, "ring", 1785672298, b"jpg-data")
            p2 = history.save_snapshot(d, "ring", 1785672298, b"other")
            self.assertEqual(p1, p2)
            self.assertEqual(p1.read_bytes(), b"jpg-data")


class ListHistoryTests(unittest.TestCase):
    def test_sorted_and_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-lock")
            (d / "ring_1785672298.jpg").write_bytes(b"a")
            (d / "picklock_1785652830.mp4").write_bytes(b"b")
            (d / "picklock_1785652830.h264").write_bytes(b"c")
            (d / "unlock_1785672400.jpg").write_bytes(b"d")
            entries = history.list_history(root, "w-lock", limit=10)
            # ring 图 / picklock mp4 / picklock h264 / unlock 图 = 4 条
            self.assertEqual(len(entries), 4)
            self.assertEqual(entries[0]["kind"], "unlock")
            self.assertEqual(entries[0]["time"], "1785672400")
            self.assertEqual(entries[0]["type"], "image")
            self.assertEqual(entries[1]["kind"], "ring")
            self.assertEqual(entries[2]["kind"], "picklock")
            self.assertIn("media-source://media_source/", entries[0]["media_id"])

    def test_device_filter_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d1 = history.history_dir(root, "w-lock1")
            d2 = history.history_dir(root, "w-lock2")
            (d1 / "ring_1785672298.jpg").write_bytes(b"a")
            (d2 / "ring_1785672299.jpg").write_bytes(b"b")
            all_entries = history.list_history(root, limit=10)
            self.assertEqual(len(all_entries), 2)
            only_1 = history.list_history(root, "w-lock1", limit=10)
            self.assertEqual(len(only_1), 1)
            self.assertEqual(only_1[0]["device_id"], "w-lock1")


class MediaSourceIdTests(unittest.TestCase):
    def test_media_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-lock")
            f = d / "ring_1785672298.jpg"
            self.assertEqual(
                history.media_source_id(root, f),
                "media-source://media_source/orvibohomebridge/w-lock/ring_1785672298.jpg",
            )


if __name__ == "__main__":
    unittest.main()
