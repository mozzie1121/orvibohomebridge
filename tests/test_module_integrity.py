"""整个集成的导入完整性测试：相对导入与 DeviceCategory 成员都必须存在。

这类问题只会在真机加载时才炸（HA 逐个导入平台），静态检查能提前拦住。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

COMPONENT = pathlib.Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _module_names(path: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(errors="replace")).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _resolve(path: pathlib.Path, node: ast.ImportFrom) -> pathlib.Path | None:
    base = path.parent
    for _ in range(max(0, node.level - 1)):
        base = base.parent
    if not node.module:
        return None
    candidate = base / pathlib.PurePosixPath(node.module.replace(".", "/"))
    if candidate.with_suffix(".py").exists():
        return candidate.with_suffix(".py")
    if (candidate / "__init__.py").exists():
        return candidate / "__init__.py"
    return None


def _all_modules() -> list[pathlib.Path]:
    return sorted(p for p in COMPONENT.rglob("*.py") if "__pycache__" not in p.parts)


class ModuleIntegrityTests(unittest.TestCase):
    def test_relative_imports_resolve(self) -> None:
        broken: list[str] = []
        for path in _all_modules():
            tree = ast.parse(path.read_text(errors="replace"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                resolved = _resolve(path, node)
                if resolved is None:
                    broken.append(f"{path.name}: 模块缺失 {'.' * node.level}{node.module or ''}")
                    continue
                exported = _module_names(resolved)
                for alias in node.names:
                    if alias.name == "*" or alias.name in exported:
                        continue
                    broken.append(f"{path.name}: {resolved.name} 没有 {alias.name}")
        self.assertEqual(broken, [], f"相对导入问题: {broken[:10]}")

    def test_device_category_members_exist(self) -> None:
        tree = ast.parse((COMPONENT / "device_types.py").read_text(errors="replace"))
        members: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "DeviceCategory":
                for item in node.body:
                    if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name):
                        members.add(item.targets[0].id)
        self.assertTrue(members, "DeviceCategory 未解析到成员")

        missing: dict[str, list[str]] = {}
        for path in _all_modules():
            for node in ast.walk(ast.parse(path.read_text(errors="replace"))):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "DeviceCategory"
                    and node.attr not in members
                ):
                    missing.setdefault(node.attr, []).append(path.name)
        self.assertEqual(missing, {}, f"未定义的 DeviceCategory 成员: {missing}")

    def test_camera_stack_modules_present(self) -> None:
        required = [
            "camera.py", "camera_metadata.py", "media_source.py", "meari_live.py",
            "meari_stream.py", "meari_hdd.py", "meari_transport/vendor/msgsvr_codec.py",
            "meari_transport/vendor/_des3_shim.py",
        ]
        missing = [name for name in required if not (COMPONENT / name).exists()]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
