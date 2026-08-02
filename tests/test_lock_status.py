"""Tests for dependency-free door-lock normalization helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "lock_status.py"
)
SPEC = importlib.util.spec_from_file_location("orvibohomebridge_lock_status", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
lock_status = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lock_status
SPEC.loader.exec_module(lock_status)


class DoorLockPropertyTests(unittest.TestCase):
    """形态 A：properties.doorLock（type=522 / classId=463）。

    V5 Eyes 实机语义：lockState="on"=已解锁，lockState="off"=已锁定。
    """

    def test_doorlock_morphology_unlocked_and_open(self) -> None:
        result = lock_status.normalize_door_lock_properties(
            {
                "doorLock": {
                    "lockState": "on",
                    "doorState": "on",
                    "insideLockState": "off",
                }
            }
        )
        self.assertIs(result["locked"], False)
        self.assertIs(result["door_open"], True)
        self.assertIs(result["inside_locked"], False)
        self.assertIsNone(result["child_locked"])

    def test_doorlock_morphology_locked(self) -> None:
        result = lock_status.normalize_door_lock_properties(
            {"doorLock": {"lockState": "off", "doorState": "off"}}
        )
        self.assertIs(result["locked"], True)
        self.assertIs(result["door_open"], False)

    def test_flat_morphology_reverse_lock(self) -> None:
        result = lock_status.normalize_door_lock_properties(
            {
                "door_status": "open",
                "reverse_lock": "unlocked",
                "handle": "unlocked",
                "clild_lock": "on",
            }
        )
        self.assertIs(result["locked"], False)
        self.assertIs(result["door_open"], True)
        self.assertIs(result["child_locked"], True)
        self.assertEqual(result["raw"]["flat"]["handle"], "unlocked")

    def test_flat_morphology_locked(self) -> None:
        result = lock_status.normalize_door_lock_properties(
            {"door_status": "closed", "reverse_lock": "locked"}
        )
        self.assertIs(result["locked"], True)
        self.assertIs(result["door_open"], False)

    def test_flat_keys_override_nested(self) -> None:
        result = lock_status.normalize_door_lock_properties(
            {
                "doorLock": {"lockState": "on", "doorState": "off"},
                "reverse_lock": "unlocked",
                "door_status": "open",
            }
        )
        self.assertIs(result["locked"], False)
        self.assertIs(result["door_open"], True)

    def test_unknown_values_are_ignored(self) -> None:
        result = lock_status.normalize_door_lock_properties(
            {"doorLock": {"lockState": "locked-ish"}, "door_status": "ajar"}
        )
        self.assertIsNone(result["locked"])
        self.assertIsNone(result["door_open"])

    def test_missing_properties(self) -> None:
        result = lock_status.normalize_door_lock_properties(None)
        self.assertIsNone(result["locked"])
        self.assertIsNone(result["door_open"])


class BatteryTests(unittest.TestCase):
    def test_dual_battery_managers(self) -> None:
        result = lock_status.normalize_battery_properties(
            {
                "batteryManager": {"level": 80, "isSetupBattery": "on"},
                "batteryManager1": {"level": "62", "isSetupBattery": "on"},
            }
        )
        self.assertEqual(result["dry_battery_level"], 80)
        self.assertIs(result["dry_battery_setup"], True)
        self.assertEqual(result["lithium_battery_level"], 62)
        self.assertIs(result["lithium_battery_setup"], True)

    def test_uninstalled_battery_reports_unknown_level(self) -> None:
        result = lock_status.normalize_battery_properties(
            {"batteryManager": {"level": 0, "isSetupBattery": "off"}}
        )
        self.assertIsNone(result["dry_battery_level"])
        self.assertIs(result["dry_battery_setup"], False)

    def test_missing_battery_managers(self) -> None:
        self.assertEqual(lock_status.normalize_battery_properties({}), {})


class LockEventTests(unittest.TestCase):
    def test_unlock_event(self) -> None:
        event = lock_status.normalize_lock_event(
            {
                "cmd": 352,
                "event": {
                    "server": "doorLock",
                    "name": "unlockEvent",
                    "value": {"type": "fingerprint", "userId": 1},
                },
            }
        )
        self.assertEqual(event["kind"], "unlock")
        self.assertEqual(event["unlock_type"], "fingerprint")
        self.assertEqual(event["unlock_user_id"], 1)

    def test_doorbell_ring_event(self) -> None:
        event = lock_status.normalize_lock_event(
            {
                "cmd": 352,
                "event": {
                    "server": "doorbell",
                    "name": "ring",
                    "value": {"url": "http://192.168.1.10/live", "doorbell_local_Ip": "192.168.1.10"},
                },
            }
        )
        self.assertEqual(event["kind"], "ring")
        self.assertEqual(event["doorbell_url"], "http://192.168.1.10/live")

    def test_answered_and_bye(self) -> None:
        self.assertEqual(
            lock_status.normalize_lock_event(
                {"event": {"server": "doorbell", "name": "answered", "value": {"uid": "x"}}}
            )["kind"],
            "answered",
        )
        self.assertEqual(
            lock_status.normalize_lock_event(
                {"event": {"server": "doorbell", "name": "bye", "value": {}}}
            )["kind"],
            "bye",
        )

    def test_non_event_payload(self) -> None:
        self.assertIsNone(lock_status.normalize_lock_event({"cmd": 42, "value1": 0}))
        self.assertIsNone(lock_status.normalize_lock_event(None))

    def test_error_unlock_event(self) -> None:
        event = lock_status.normalize_lock_event(
            {
                "cmd": 352,
                "time": 1785652326,
                "event": {
                    "server": "doorLock",
                    "name": "errorUnlockEvent",
                    "value": {"type": "password"},
                },
            }
        )
        self.assertEqual(event["kind"], "error_unlock")
        self.assertEqual(event["unlock_type"], "password")
        self.assertEqual(event["time"], 1785652326)

    def test_door_unclose_event(self) -> None:
        event = lock_status.normalize_lock_event(
            {
                "cmd": 352,
                "time": 1785652455,
                "event": {"server": "doorLock", "name": "doorUnclose"},
            }
        )
        self.assertEqual(event["kind"], "door_unclose")
        self.assertEqual(event["time"], 1785652455)

    def test_picklock_event_with_media(self) -> None:
        event = lock_status.normalize_lock_event(
            {
                "cmd": 352,
                "time": 1785652501,
                "event": {
                    "server": "doorLock",
                    "name": "picklockEvent",
                    "value": {
                        "videoUrl": "/LOCK/videoPicklockEvent/picklockEvent_1.h264",
                        "url": "/LOCK/picturePicklockEvent/picklockEvent_1.jpg",
                    },
                },
            }
        )
        self.assertEqual(event["kind"], "picklock")
        self.assertEqual(event["video_url"], "/LOCK/videoPicklockEvent/picklockEvent_1.h264")
        self.assertEqual(event["pic_url"], "/LOCK/picturePicklockEvent/picklockEvent_1.jpg")

    def test_picklock_event_with_empty_value(self) -> None:
        event = lock_status.normalize_lock_event(
            {
                "cmd": 352,
                "time": 1785652507,
                "event": {"server": "doorLock", "name": "picklockEvent", "value": {}},
            }
        )
        self.assertEqual(event["kind"], "picklock")
        self.assertIsNone(event["video_url"])
        self.assertIsNone(event["pic_url"])

    def test_leave_home_event_with_media(self) -> None:
        event = lock_status.normalize_lock_event(
            {
                "cmd": 352,
                "time": 1785652883,
                "event": {
                    "server": "doorLock",
                    "name": "leaveHomeEvent",
                    "value": {
                        "videoUrl": "/LOCK/videoleaveHomeEvent/leaveHomeEvent_1.h264",
                        "url": "/LOCK/pictureleaveHomeEvent/leaveHomeEvent_1.jpg",
                    },
                },
            }
        )
        self.assertEqual(event["kind"], "leave_home")
        self.assertEqual(event["video_url"], "/LOCK/videoleaveHomeEvent/leaveHomeEvent_1.h264")
        self.assertEqual(event["pic_url"], "/LOCK/pictureleaveHomeEvent/leaveHomeEvent_1.jpg")

    def test_leave_home_event_with_empty_value(self) -> None:
        event = lock_status.normalize_lock_event(
            {
                "cmd": 352,
                "time": 1785652883,
                "event": {"server": "doorLock", "name": "leaveHomeEvent", "value": {}},
            }
        )
        self.assertEqual(event["kind"], "leave_home")

    def test_leave_home_alarm_config(self) -> None:
        result = lock_status.normalize_door_lock_properties(
            {"doorLock": {"leaveHomeAlarmCfg": "on"}}
        )
        self.assertIs(result["leave_home_armed"], True)
        self.assertIsNone(result["locked"])

    def test_partial_inside_lock_update(self) -> None:
        result = lock_status.normalize_door_lock_properties(
            {"doorLock": {"insideLockState": "on"}}
        )
        self.assertIsNone(result["locked"])
        self.assertIsNone(result["door_open"])
        self.assertIs(result["inside_locked"], True)


class MessageEventTests(unittest.TestCase):
    def test_lock_text_message(self) -> None:
        event = lock_status.normalize_message_event(
            {
                "cmd": 82,
                "infoType": 12,
                "messageType": 0,
                "data": (
                    '{"deviceType":522,"uid":"lock-uid-1","isAlarm":0,'
                    '"subDeviceType":463,"time":1785651770,'
                    '"deviceId":"w-lock-0001"}'
                ),
                "text": "14:22 门锁(客厅):2 用指纹打开门锁",
            }
        )
        self.assertEqual(event["kind"], "message")
        self.assertEqual(event["device_id"], "w-lock-0001")
        self.assertEqual(event["uid"], "lock-uid-1")
        self.assertEqual(event["is_alarm"], 0)
        self.assertEqual(event["text"], "14:22 门锁(客厅):2 用指纹打开门锁")

    def test_doorbell_visitor_message(self) -> None:
        event = lock_status.normalize_message_event(
            {
                "cmd": 82,
                "infoType": 68,
                "messageType": 0,
                "data": (
                    '{"deviceType":522,"uid":"lock-uid-1","picUrl":"/LOCK/pictureUploadRing/ring_1.jpg",'
                    '"isAlarm":0,"subDeviceType":463,"time":1785652288,'
                    '"deviceId":"w-lock-0001","doorbell_local_Ip":"192.168.1.20"}'
                ),
                "text": "14:31 门锁(客厅):有客人来访",
            }
        )
        self.assertEqual(event["kind"], "message")
        self.assertEqual(event["pic_url"], "/LOCK/pictureUploadRing/ring_1.jpg")
        self.assertEqual(event["time"], 1785652288)

    def test_alarm_message(self) -> None:
        event = lock_status.normalize_message_event(
            {
                "cmd": 82,
                "infoType": 39,
                "messageType": 1,
                "data": (
                    '{"deviceType":522,"uid":"lock-uid-1","isAlarm":1,'
                    '"subDeviceType":463,"time":1785652328,"deviceId":"w-lock-0001"}'
                ),
                "text": "14:32 门锁(客厅):开锁身份多次验证失败，门锁暂时被锁定，请确认现场情况！",
            }
        )
        self.assertEqual(event["is_alarm"], 1)
        self.assertEqual(event["message_type"], 1)

    def test_non_lock_message_is_ignored(self) -> None:
        self.assertIsNone(
            lock_status.normalize_message_event(
                {
                    "cmd": 82,
                    "infoType": 1,
                    "data": '{"deviceType":1,"uid":"light-1"}',
                    "text": "灯已打开",
                }
            )
        )

    def test_cmd_42_is_not_a_message(self) -> None:
        self.assertIsNone(lock_status.normalize_message_event({"cmd": 42}))


class DoorAttributionTests(unittest.TestCase):
    def test_recent_unlock_attributes_door_open(self) -> None:
        last = {"user_id": 2, "unlock_type": "fingerprint"}
        self.assertEqual(
            lock_status.resolve_opened_by(last, 4.0, window=30),
            {"user_id": "2", "unlock_type": "fingerprint"},
        )

    def test_stale_unlock_not_attributed(self) -> None:
        last = {"user_id": 2, "unlock_type": "fingerprint"}
        self.assertIsNone(lock_status.resolve_opened_by(last, 31.0, window=30))

    def test_missing_user_id_not_attributed(self) -> None:
        self.assertIsNone(
            lock_status.resolve_opened_by({"unlock_type": "password"}, 1.0)
        )

    def test_no_last_unlock_not_attributed(self) -> None:
        self.assertIsNone(lock_status.resolve_opened_by(None, 1.0))


class LockStatusDerivationTests(unittest.TestCase):
    def test_open_door_means_unlocked(self) -> None:
        self.assertEqual(
            lock_status.derive_lock_status(True, True, None),
            "unlocked",
        )
        self.assertEqual(
            lock_status.derive_lock_status(False, True, True),
            "unlocked",
        )

    def test_closed_door_inside_lock(self) -> None:
        self.assertEqual(
            lock_status.derive_lock_status(True, False, True),
            "inside_locked",
        )

    def test_closed_door_locked_and_unlocked(self) -> None:
        self.assertEqual(
            lock_status.derive_lock_status(True, False, False),
            "locked",
        )
        self.assertEqual(
            lock_status.derive_lock_status(False, False, False),
            "unlocked",
        )

    def test_unknown_when_no_fields(self) -> None:
        self.assertIsNone(lock_status.derive_lock_status(None, None, None))


class UnlockLabelTests(unittest.TestCase):
    def test_named_user(self) -> None:
        self.assertEqual(lock_status.format_unlock_label(2, "张三"), "张三开门")

    def test_unnamed_user_falls_back_to_id(self) -> None:
        self.assertEqual(lock_status.format_unlock_label(2, None), "用户2开门")

    def test_no_event_yet(self) -> None:
        self.assertEqual(lock_status.format_unlock_label(None, None), "无")


class LockUserMappingTests(unittest.TestCase):
    def test_parse_equals_and_colon(self) -> None:
        result = lock_status.parse_lock_user_names(
            "1=张三\n2:李四\n\nbad line\n3= 王五 "
        )
        self.assertEqual(result, {"1": "张三", "2": "李四", "3": "王五"})

    def test_parse_empty(self) -> None:
        self.assertEqual(lock_status.parse_lock_user_names(""), {})
        self.assertEqual(lock_status.parse_lock_user_names(None), {})

    def test_format_roundtrip(self) -> None:
        text = lock_status.format_lock_user_names({"2": "李四", "1": "张三"})
        self.assertEqual(text, "1=张三\n2=李四")
        self.assertEqual(
            lock_status.parse_lock_user_names(text),
            {"1": "张三", "2": "李四"},
        )

    def test_format_invalid_input(self) -> None:
        self.assertEqual(lock_status.format_lock_user_names(None), "")
        self.assertEqual(lock_status.format_lock_user_names("nope"), "")


if __name__ == "__main__":
    unittest.main()
