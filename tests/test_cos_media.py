"""Tests for dependency-free COS media credential/URL helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import time
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "cos_media.py"
)
SPEC = importlib.util.spec_from_file_location("orvibohomebridge_cos_media", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
cos_media = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cos_media
SPEC.loader.exec_module(cos_media)

# 实机 cmd=313 响应样本（2026-08-02，字段已脱敏处理但保留结构）
SAMPLE_SECURITY = {
    "status": 0,
    "pageNum": 1,
    "pageSize": 20,
    "accessKeyId": "AKIDTESTTESTTESTTESTTESTTESTTESTTESTTEST",
    "accessKeySecret": "CSYTRm7jLslXv1Ut9daZyiGl+HoFMgt5Raq//AypF2E=",
    "securityToken": "QeIhfJn0wdBI45nD3VbQcchmkhFxYHqadf45e2f789d5a3469de9046a99add69d",
    "bucketName": "p20-doorlock-1251222210",
    "region": "ap-guangzhou",
    "expiration": 129600,
    "systemCurrentTime": 1785700000000,
    "endpoint": "https://cos.ap-guangzhou.myqcloud.com",
}


def make_response(security: dict | None = None, status: int = 0) -> dict:
    inner = {
        "status": status,
        "namespace": "Skill.GetCOSAuthorization",
        "security": security if security is not None else SAMPLE_SECURITY,
    }
    return {"serial": 12345, "response": json.dumps(inner)}


class ParseResponseTests(unittest.TestCase):
    def test_parse_real_sample(self) -> None:
        creds = cos_media.parse_cos_response(make_response())
        assert creds is not None
        self.assertEqual(creds.bucket, "p20-doorlock-1251222210")
        self.assertEqual(creds.region, "ap-guangzhou")
        self.assertEqual(creds.secret_id, SAMPLE_SECURITY["accessKeyId"])
        self.assertEqual(creds.session_token, SAMPLE_SECURITY["securityToken"])
        self.assertAlmostEqual(creds.expires_at, 1785700000 + 129600, delta=2)
        self.assertTrue(creds.valid)

    def test_parse_wrong_namespace(self) -> None:
        inner = {"status": 0, "namespace": "Skill.Other", "security": SAMPLE_SECURITY}
        resp = {"response": json.dumps(inner)}
        self.assertIsNone(cos_media.parse_cos_response(resp))

    def test_parse_failure_status(self) -> None:
        self.assertIsNone(cos_media.parse_cos_response(make_response(status=12)))

    def test_parse_missing_fields(self) -> None:
        security = dict(SAMPLE_SECURITY)
        security.pop("bucketName")
        self.assertIsNone(cos_media.parse_cos_response(make_response(security)))

    def test_parse_garbage(self) -> None:
        self.assertIsNone(cos_media.parse_cos_response({"response": "not-json"}))
        self.assertIsNone(cos_media.parse_cos_response({}))


class SignedUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.creds = cos_media.parse_cos_response(make_response())
        assert self.creds is not None

    def test_virtual_host_style(self) -> None:
        url = cos_media.signed_media_url(
            self.creds, "/77c139c4d27f4fa6a20e1f459849aa47/picturePicklockEvent/x.jpg"
        )
        self.assertTrue(
            url.startswith(
                "https://p20-doorlock-1251222210.cos.ap-guangzhou.myqcloud.com/"
            ),
            url,
        )
        self.assertIn("q-sign-algorithm=sha1", url)
        self.assertIn("q-sign-time=", url)
        self.assertIn("q-signature=", url)
        self.assertIn("x.jpg", url)

    def test_key_without_leading_slash(self) -> None:
        url = cos_media.signed_media_url(
            self.creds, "77c139c4d27f4fa6a20e1f459849aa47/videoPicklockEvent/x.h264"
        )
        self.assertIn("/77c139c4d27f4fa6a20e1f459849aa47/videoPicklockEvent/x.h264?", url)

    def test_signature_window_within_expiry(self) -> None:
        url = cos_media.signed_media_url(self.creds, "/a.jpg", ttl=600)
        q_sign_time = url.split("q-sign-time=")[1].split("&")[0]
        start, end = (int(v) for v in q_sign_time.split(";"))
        self.assertGreaterEqual(start, int(time.time()) - 5)
        self.assertLessEqual(end - start, 600)


class CredentialCacheTests(unittest.TestCase):
    def test_cache_reuses_credentials(self) -> None:
        calls: list[tuple] = []

        class FakeSsl:
            async def send_cos_auth(self, user_id, device_id, device_uid):
                calls.append((user_id, device_id, device_uid))
                return make_response()

        manager = cos_media.CosMediaManager(FakeSsl(), "u_1", "f_1")

        async def run() -> None:
            c1 = await manager.get_credentials("w-lock", "lock-uid")
            c2 = await manager.get_credentials("w-lock", "lock-uid")
            assert c1 is not None and c2 is not None
            self.assertIs(c1, c2)

        import asyncio

        asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("u_1", "w-lock", "lock-uid"))

    def test_expired_credentials_refresh(self) -> None:
        calls: list[int] = []

        class FakeSsl:
            async def send_cos_auth(self, user_id, device_id, device_uid):
                calls.append(1)
                resp = make_response()
                # 返回即将过期的凭证
                inner = json.loads(resp["response"])
                inner["security"]["expiration"] = 1
                inner["security"]["systemCurrentTime"] = int(time.time() * 1000)
                return {"response": json.dumps(inner)}

        manager = cos_media.CosMediaManager(FakeSsl(), "u_1", "f_1")

        async def run() -> None:
            for _ in range(3):
                await manager.get_credentials("w-lock", "lock-uid")
                await asyncio.sleep(1.1)

        import asyncio

        asyncio.run(run())
        self.assertGreaterEqual(len(calls), 2)

    def test_try_signed_url_uses_cache_without_network(self) -> None:
        class FakeSsl:
            async def send_cos_auth(self, user_id, device_id, device_uid):
                return make_response()

        manager = cos_media.CosMediaManager(FakeSsl(), "u_1", "f_1")
        # 无缓存：返回 None
        self.assertIsNone(
            manager.try_signed_url("w-lock", "lock-uid", "/a/b.jpg")
        )

        async def run() -> None:
            await manager.get_credentials("w-lock", "lock-uid")

        import asyncio

        asyncio.run(run())
        url = manager.try_signed_url("w-lock", "lock-uid", "/a/b.jpg")
        assert url is not None
        self.assertIn("q-signature=", url)


if __name__ == "__main__":
    unittest.main()
