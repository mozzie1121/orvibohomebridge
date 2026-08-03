"""Tests for instance-safe ORVIBO cloud endpoint selection."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibohomebridge"


def _load_cloud_module():
    package_name = "orvibohomebridge_cloud_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.cloud")


def _load_https_client_module():
    _load_cloud_module()
    if "aiohttp" not in sys.modules:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientSession = object
        aiohttp.ClientTimeout = object
        aiohttp.TCPConnector = object
        sys.modules["aiohttp"] = aiohttp
    return importlib.import_module("orvibohomebridge_cloud_test.https_client")


class CloudEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cloud = _load_cloud_module()

    def test_region_and_host_resolution(self) -> None:
        self.assertIs(
            self.cloud.cloud_for_region("global"),
            self.cloud.GLOBAL_CLOUD,
        )
        self.assertIs(
            self.cloud.cloud_for_api_host("china.orvibo.com"),
            self.cloud.CHINA_CLOUD,
        )
        with self.assertRaises(ValueError):
            self.cloud.cloud_for_api_host("example.invalid")

    def test_candidates_put_preference_first(self) -> None:
        candidates = self.cloud.cloud_candidates(self.cloud.GLOBAL_CLOUD)
        self.assertEqual(
            [item.region.value for item in candidates],
            ["global", "china"],
        )

    def test_endpoint_is_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.cloud.CHINA_CLOUD.api_host = "example.invalid"


class CloudClientIsolationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cloud = _load_cloud_module()
        cls.https_client = _load_https_client_module()

    def _client(self, cloud):
        return self.https_client.HttpsClient(
            username="account@example.com",
            password_hash="5F4DCC3B5AA765D61D8327DEB882CF99",
            cloud=cloud,
        )

    async def test_clients_keep_independent_regions(self) -> None:
        china_client = self._client(self.cloud.CHINA_CLOUD)
        global_client = self._client(self.cloud.GLOBAL_CLOUD)

        self.assertEqual(china_client.api_host, "china.orvibo.com")
        self.assertEqual(global_client.api_host, "homemate.orvibo.com")
        await china_client.switch_cloud(self.cloud.GLOBAL_CLOUD)
        self.assertEqual(global_client.api_host, "homemate.orvibo.com")

    async def test_detection_tries_fallback_and_preserves_family(self) -> None:
        client = self._client(self.cloud.CHINA_CLOUD)
        attempts = []

        async def fake_ensure_login() -> bool:
            attempts.append((client.cloud.region.value, client.family_id))
            return client.cloud is self.cloud.GLOBAL_CLOUD

        client.ensure_login = fake_ensure_login
        self.assertTrue(await client.async_detect_cloud("family-1"))
        self.assertEqual(
            attempts,
            [("china", "family-1"), ("global", "family-1")],
        )

    async def test_selected_family_must_exist_in_current_region(self) -> None:
        client = self._client(self.cloud.CHINA_CLOUD)
        client.user_id = "user-1"
        client.access_token = "token-1"
        client.family_id = "family-global"

        async def fake_send_request(url, data):
            return {
                "data": [
                    {"familyId": "family-china", "familyName": "China Home"}
                ]
            }

        client._send_request = fake_send_request
        self.assertEqual(await client._fetch_family(), {})

    async def test_login_failure_does_not_call_family_endpoint(self) -> None:
        client = self._client(self.cloud.CHINA_CLOUD)
        family_called = False

        async def fake_access_token():
            return {}

        async def fake_family():
            nonlocal family_called
            family_called = True
            return {}

        client._fetch_access_token = fake_access_token
        client._fetch_family = fake_family
        self.assertFalse(await client.ensure_login())
        self.assertFalse(family_called)

    async def test_oauth_uses_structured_get_query_parameters(self) -> None:
        client = self._client(self.cloud.CHINA_CLOUD)
        captured = {}

        async def fake_send_request(url, data, params=None):
            captured.update(url=url, data=data, params=params)
            return {
                "data": {
                    "access_token": "token-1",
                    "user_id": "user-1",
                }
            }

        client._send_request = fake_send_request
        result = await client._fetch_access_token()
        self.assertEqual(result["access_token"], "token-1")
        self.assertEqual(
            captured["url"], "https://china.orvibo.com/getOauthToken"
        )
        self.assertIsNone(captured["data"])
        self.assertEqual(captured["params"]["userName"], "account@example.com")


if __name__ == "__main__":
    unittest.main()
