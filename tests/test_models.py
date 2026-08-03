"""Tests for immutable integration domain models."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_models_module():
    package_name = "orvibohomebridge_models_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.models")


class AccountCredentialsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.models = _load_models_module()

    def test_normalizes_values_and_is_immutable(self) -> None:
        credentials = self.models.AccountCredentials(
            " account@example.com ",
            "5f4dcc3b5aa765d61d8327deb882cf99",
            " family-id ",
        )

        self.assertEqual(credentials.username, "account@example.com")
        self.assertEqual(
            credentials.password_hash,
            "5F4DCC3B5AA765D61D8327DEB882CF99",
        )
        self.assertEqual(credentials.family_id, "family-id")
        with self.assertRaises(FrozenInstanceError):
            credentials.family_id = "other"

    def test_rejects_invalid_credentials(self) -> None:
        with self.assertRaises(ValueError):
            self.models.AccountCredentials("", "not-a-digest")


if __name__ == "__main__":
    unittest.main()
