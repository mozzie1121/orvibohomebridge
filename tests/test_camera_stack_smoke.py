"""摄像头栈的结构性冒烟测试（不需要 Home Assistant）。"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import unittest

COMPONENT = pathlib.Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"

CAMERA_MODULES = [
    "camera.py", "camera_metadata.py", "media_source.py", "frontend_resources.py",
    "registry_sync.py", "runtime_options.py", "meari_live.py", "meari_stream.py",
    "meari_session.py", "meari_inventory.py", "meari_hdd.py", "meari_channels.py",
    "meari_probe.py", "meari_probe_context.py",
    "meari_transport/__init__.py", "meari_transport/hdd.py", "meari_transport/worker.py",
    "meari_transport/vendor/msgsvr_codec.py", "meari_transport/vendor/kcp_tunnel.py",
    "meari_transport/vendor/turn_client.py", "meari_transport/vendor/url_util.py",
    "meari_transport/vendor/p2p_streamer/codec.py",
    "meari_transport/vendor/p2p_streamer/protocol.py",
    "meari_transport/vendor/p2p_streamer/sdp.py",
    "meari_transport/vendor/p2p_streamer/codecs/h264.py",
    "meari_transport/vendor/p2p_streamer/codecs/hevc.py",
    "meari_transport/vendor/p2p_streamer/codecs/av1.py",
]


def _resolve_relative(module: pathlib.Path, node: ast.ImportFrom) -> pathlib.Path:
    base = module.parent
    for _ in range(max(0, node.level - 1)):
        base = base.parent
    target = node.module or ""
    if target:
        candidate = base / pathlib.PurePosixPath(target.replace(".", "/"))
        return candidate
    return base


class CameraStackSmokeTests(unittest.TestCase):
    def test_camera_modules_exist(self) -> None:
        missing = [name for name in CAMERA_MODULES if not (COMPONENT / name).exists()]
        self.assertEqual(missing, [])

    def test_relative_imports_resolve(self) -> None:
        """每个包内相对导入都必须指向真实存在的模块，避免搬漏文件。"""
        broken = []
        for name in CAMERA_MODULES:
            path = COMPONENT / name
            tree = ast.parse(path.read_text(errors="replace"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                resolved = _resolve_relative(path, node)
                if (resolved.with_suffix(".py")).exists() or (resolved / "__init__.py").exists():
                    continue
                broken.append(f"{name}: {'.' * node.level}{node.module or ''}")
        self.assertEqual(broken, [], f"未解析的相对导入: {broken}")

    def test_des3_shim_roundtrip(self) -> None:
        """pycryptodome 的 DES3 已被 cryptography shim 取代，必须能加解密往返。"""
        spec = importlib.util.spec_from_file_location(
            "orvibo_des3_shim_test",
            COMPONENT / "meari_transport/vendor/_des3_shim.py",
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        key = b"0123456789abcdef01234567"  # 24 字节 3DES key
        plain = b"orvibo-meari-tes"  # 16 字节，DES3 块长 8
        encrypted = module.DES3.new(key, module.DES3.MODE_ECB).encrypt(plain)
        self.assertNotEqual(encrypted, plain)
        self.assertEqual(
            module.DES3.new(key, module.DES3.MODE_ECB).decrypt(encrypted), plain
        )


if __name__ == "__main__":
    unittest.main()
