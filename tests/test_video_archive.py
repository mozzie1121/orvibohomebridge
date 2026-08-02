"""Tests for dependency-free video archive helpers."""

from __future__ import annotations

import http.server
import importlib.util
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "video_archive.py"
)
SPEC = importlib.util.spec_from_file_location(
    "orvibohomebridge_video_archive", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
video_archive = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = video_archive
SPEC.loader.exec_module(video_archive)


class InferEventNameTests(unittest.TestCase):
    def test_picklock(self) -> None:
        key = (
            "/77c139c4d27f4fa6a20e1f459849aa47/"
            "videoPicklockEvent/picklockEvent_1785652830.h264"
        )
        self.assertEqual(
            video_archive.infer_event_name(key), ("picklock", "1785652830")
        )

    def test_leave_home(self) -> None:
        key = "/uid/videoleaveHomeEvent/leaveHomeEvent_1785652883.h264"
        self.assertEqual(
            video_archive.infer_event_name(key), ("leavehome", "1785652883")
        )

    def test_picture_ring(self) -> None:
        # 门铃图片（pictureUploadRing）不是录像对象，不应推断为视频事件
        key = "/uid/pictureUploadRing/ring_1785652287.jpg"
        self.assertIsNone(video_archive.infer_event_name(key))

    def test_unmatched(self) -> None:
        self.assertIsNone(video_archive.infer_event_name("/uid/random/file.bin"))


class BuildMediaPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_paths(self) -> None:
        key = (
            "/77c139c4d27f4fa6a20e1f459849aa47/"
            "videoPicklockEvent/picklockEvent_1785652830.h264"
        )
        paths = video_archive.build_media_paths(
            self.root, "w-77c139c4d27f4fa6a20e1f459849aa47", key
        )
        assert paths is not None
        h264, mp4 = paths
        self.assertEqual(
            h264,
            self.root
            / "orvibohomebridge"
            / "w-77c139c4d27f4fa6a20e1f459849aa47"
            / "picklock_1785652830.h264",
        )
        self.assertEqual(mp4.suffix, ".mp4")

    def test_unmatched_returns_none(self) -> None:
        self.assertIsNone(
            video_archive.build_media_paths(self.root, "w-x", "/a/b.bin")
        )


class DownloadTests(unittest.TestCase):
    def test_download_from_http(self) -> None:
        payload = b"\x00\x01\x02JFIF-test-data" * 100

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # noqa: A003
                pass

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory() as td:
                    dest = Path(td) / "sub" / "out.h264"
                    ok = video_archive.download(
                        f"http://127.0.0.1:{port}/test.h264", dest
                    )
                    self.assertTrue(ok)
                    self.assertEqual(dest.read_bytes(), payload)
            finally:
                httpd.shutdown()


class TranscodeTests(unittest.TestCase):
    def test_no_ffmpeg_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.h264"
            dst = Path(td) / "out.mp4"
            src.write_bytes(b"fake")
            self.assertFalse(video_archive.transcode_to_mp4(src, dst, None))
            self.assertFalse(dst.exists())

    def test_ffmpeg_missing_binary_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.h264"
            dst = Path(td) / "out.mp4"
            src.write_bytes(b"fake")
            self.assertFalse(
                video_archive.transcode_to_mp4(
                    src, dst, "/nonexistent/ffmpeg-xyz"
                )
            )


if __name__ == "__main__":
    unittest.main()
