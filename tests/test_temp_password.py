"""Tests for dependency-free temp password helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import time
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "temp_password.py"
)
SPEC = importlib.util.spec_from_file_location("orvibohomebridge_temp_password", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
temp_password = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = temp_password
SPEC.loader.exec_module(temp_password)


def make_grant_response(
    status=0,
    password="121583",
    authorized_id=101,
    number=1,
    unlock_num=0,
    end_time=0,
) -> dict:
    auth = {
        "authorizedUnlockId": "z19f376bfe3b48f79f0e9dcb0a0410f9",
        "deviceId": "w-lock",
        "authorizedId": authorized_id,
        "phone": "",
        "time": 1440,
        "number": number,
        "startTime": int(time.time()),
        "unlockNum": unlock_num,
        "authorizeStatus": 0,
        "password": password,
    }
    if end_time:
        auth["endTime"] = end_time
    return {
        "code": password,
        "fromMq": True,
        "type": 2,
        "userName": "临时用户 08031634",
        "phone": "",
        "authorizedUnlock": auth,
        "cmd": 246,
        "status": status,
    }


class ParseGrantResponseTests(unittest.TestCase):
    def test_parse_success(self) -> None:
        rec = temp_password.parse_grant_response(make_grant_response())
        assert rec is not None
        self.assertEqual(rec["password"], "121583")
        self.assertEqual(rec["authorized_id"], 101)
        self.assertEqual(rec["number"], 1)
        self.assertEqual(rec["unlock_num"], 0)

    def test_parse_failure_status(self) -> None:
        self.assertIsNone(
            temp_password.parse_grant_response(make_grant_response(status=1))
        )

    def test_parse_missing_password(self) -> None:
        resp = make_grant_response()
        resp.pop("code")
        resp["authorizedUnlock"].pop("password")
        self.assertIsNone(temp_password.parse_grant_response(resp))

    def test_parse_garbage(self) -> None:
        self.assertIsNone(temp_password.parse_grant_response({}))


class ExpiryTests(unittest.TestCase):
    def test_end_time_expired(self) -> None:
        rec = temp_password.parse_grant_response(make_grant_response())
        assert rec is not None
        rec["end_time"] = int(time.time()) - 10
        self.assertTrue(temp_password.is_expired(rec))

    def test_uses_remaining(self) -> None:
        rec = temp_password.parse_grant_response(make_grant_response())
        assert rec is not None
        self.assertFalse(temp_password.is_expired(rec))

    def test_usage_exhausted(self) -> None:
        rec = temp_password.parse_grant_response(
            make_grant_response(number=1, unlock_num=1)
        )
        assert rec is not None
        self.assertTrue(temp_password.is_expired(rec))

    def test_unlimited_not_expired_by_usage(self) -> None:
        rec = temp_password.parse_grant_response(
            make_grant_response(number=0, unlock_num=5)
        )
        assert rec is not None
        self.assertFalse(temp_password.is_expired(rec))


class DescribeTests(unittest.TestCase):
    def test_describe(self) -> None:
        rec = temp_password.parse_grant_response(make_grant_response())
        assert rec is not None
        info = temp_password.describe_record(rec)
        self.assertEqual(info["password"], "121583")
        self.assertIn("expired", info)
        self.assertIn("authorized_id", info)


class ParseAuthorizationItemTests(unittest.TestCase):
    def test_parse_readtable_item(self) -> None:
        item = {
            "authorizedUnlockId": "z19f376bfe3b48f79f0e9dcb0a0410f9",
            "deviceId": "w-lock",
            "authorizedId": 101,
            "uid": "lock-uid",
            "number": 1,
            "password": "121583",
            "phone": "13800138000",
            "unlockNum": 0,
            "startTime": 1783341252,
            "endTime": 1783341252 + 86400,
            "authorizeStatus": 0,
            "delFlag": 0,
        }
        rec = temp_password.parse_authorization_item(item)
        assert rec is not None
        self.assertEqual(rec["password"], "121583")
        self.assertEqual(rec["authorized_id"], 101)
        self.assertEqual(rec["number"], 1)
        self.assertEqual(rec["phone"], "13800138000")
        self.assertEqual(rec["end_time"], 1783341252 + 86400)

    def test_skips_deleted(self) -> None:
        item = {
            "authorizedUnlockId": "x",
            "authorizedId": 102,
            "password": "270602",
            "delFlag": 1,
        }
        self.assertIsNone(temp_password.parse_authorization_item(item))

    def test_skips_invalid_status(self) -> None:
        # App 实测：authorizeStatus=3 为已删除/失效（delFlag 恒为 0）
        item = {
            "authorizedUnlockId": "x",
            "authorizedId": 101,
            "password": "911851",
            "delFlag": 0,
            "authorizeStatus": 3,
        }
        self.assertIsNone(temp_password.parse_authorization_item(item))

    def test_keeps_valid_status(self) -> None:
        item = {
            "authorizedUnlockId": "x",
            "authorizedId": 101,
            "password": "496146",
            "delFlag": 0,
            "authorizeStatus": 0,
            "number": 1,
            "unlockNum": 0,
        }
        rec = temp_password.parse_authorization_item(item)
        assert rec is not None
        self.assertEqual(rec["password"], "496146")

    def test_skips_missing_fields(self) -> None:
        self.assertIsNone(temp_password.parse_authorization_item({}))
        self.assertIsNone(
            temp_password.parse_authorization_item({"authorizedId": 1})
        )


if __name__ == "__main__":
    unittest.main()
