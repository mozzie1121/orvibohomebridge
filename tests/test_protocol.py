"""Tests for dependency-free Orvibo protocol helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibohomebridge"
    / "protocol.py"
)
SPEC = importlib.util.spec_from_file_location("orvibohomebridge_protocol", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)


class ProtocolTests(unittest.TestCase):
    def test_password_hash_matches_cloud_format(self) -> None:
        self.assertEqual(
            protocol.password_hash("password"),
            "5F4DCC3B5AA765D61D8327DEB882CF99",
        )

    def test_family_request_is_stable_and_signed(self) -> None:
        body = protocol.build_family_request(
            access_token="access",
            user_id="user-1",
            timestamp_ms=1700000000123,
            nonce=123456,
        )
        self.assertEqual(body["accessToken"], "access")
        self.assertEqual(body["timestamp"], "1700000000123")
        self.assertEqual(
            body["sign"],
            "DE2412D3A82D85E151378ADFC1FF81F25F19E7606AEAC20E73BD6048709EECA5",
        )

    def test_parse_families_normalizes_and_deduplicates(self) -> None:
        families = protocol.parse_families(
            {
                "data": [
                    {"familyId": "one", "familyName": "Home"},
                    {"family_id": "two", "name": "Office"},
                    {"familyId": "one", "familyName": "Duplicate"},
                    {"familyName": "Missing ID"},
                ]
            }
        )
        self.assertEqual(
            [(family.family_id, family.name) for family in families],
            [("one", "Home"), ("two", "Office")],
        )

    def test_readtable_request_is_stable_and_signed(self) -> None:
        body = protocol.build_readtable_request(
            access_token="access",
            user_id="user-1",
            family_id="family-1",
            session_id="session-1",
            timestamp_ms=1700000000123,
            serial=1700000000,
            nonce="0123456789abcdef0123456789abcdef",
        )

        self.assertEqual(body["dataType"], "all")
        self.assertEqual(body["lastUpdateTime"], 0)
        self.assertEqual(body["userName"], "user-1")
        self.assertEqual(
            body["sign"],
            "709C8461A366ED6563072B2B04ECB4612BE4B582651284554C4EABF0CADCE76D",
        )

    def test_parse_readtable_devices_joins_only_device_tables(self) -> None:
        """account/gateway/room 表不产生设备，只有 device[] 表产生。"""
        devices = protocol.parse_readtable_devices(
            {
                "code": 0,
                "data": {
                    "account": {"uid": "account-should-not-be-a-device"},
                    "gateway": [
                        {"uid": "gateway-row", "online": 1}
                    ],
                    "room": [
                        {"roomId": "room-1", "roomName": "Kitchen", "uid": "room-row"}
                    ],
                    "device": [
                        {
                            "deviceId": "device-child-0001",
                            "uid": "shared-gw-uid",
                            "deviceName": "Ceiling light",
                            "deviceType": 1,
                            "subDeviceType": 6,
                            "roomId": "room-1",
                            "parentId": "gateway-device-1",
                            "delFlag": 0,
                        },
                        {
                            "deviceId": "device-child-0002",
                            "uid": "shared-gw-uid",
                            "deviceName": "Removed curtain",
                            "deviceType": 34,
                            "delFlag": 1,
                        },
                    ],
                    "deviceStatus": [
                        {
                            "deviceId": "device-child-0001",
                            "uid": "status-row",
                            "online": 1,
                            "value1": 67,
                            "value2": 146,
                            "value3": 250,
                            "value4": 0,
                            "delFlag": 0,
                        }
                    ],
                },
            }
        )

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].uid, "device-child-0001")
        self.assertEqual(devices[0].name, "Ceiling light")
        self.assertEqual(devices[0].room, "Kitchen")
        self.assertEqual(devices[0].parent_uid, "gateway-device-1")
        self.assertTrue(devices[0].online)
        self.assertEqual(devices[0].value1, 67)
        self.assertEqual(devices[0].value2, 146)
        self.assertEqual(devices[0].value3, 250)
        self.assertEqual(devices[0].value4, 0)

    def test_extract_devices_handles_nested_and_duplicate_devices(self) -> None:
        devices = protocol.extract_devices(
            [
                {
                    "cmd": 230,
                    "deviceList": [
                        {
                            "uid": "0123456789ABCDEF",
                            "deviceName": "Living room light",
                            "deviceType": 10,
                            "roomName": "Living room",
                        },
                        {
                            "deviceId": "camera-device-01",
                            "modelName": "S1",
                            "isOnline": 1,
                        },
                    ],
                },
                {
                    "data": {
                        "uid": "0123456789ABCDEF",
                        "model": "MixSwitch",
                        "online": "online",
                    }
                },
            ]
        )

        self.assertEqual(len(devices), 2)
        light = next(d for d in devices if d.uid == "0123456789ABCDEF")
        camera = next(d for d in devices if d.uid == "camera-device-01")
        self.assertEqual(light.name, "Living room light")
        self.assertEqual(light.model, "MixSwitch")
        self.assertEqual(light.device_type, "10")
        self.assertTrue(light.online)
        self.assertEqual(camera.model, "S1")
        self.assertTrue(camera.online)

    def test_extract_devices_does_not_treat_account_ids_as_devices(self) -> None:
        devices = protocol.extract_devices(
            {"userId": "user-123456", "familyId": "family-123456", "cmd": 2}
        )
        self.assertEqual(devices, ())

    def test_extract_devices_keeps_children_that_share_a_gateway_uid(self) -> None:
        devices = protocol.extract_devices(
            {
                "cmd": 147,
                "tableNameList": [
                    {
                        "tableName": "room",
                        "dataList": [{"roomId": "room-1", "roomName": "Kitchen"}],
                    },
                    {
                        "tableName": "device",
                        "dataList": [
                            {
                                "deviceId": "device-child-0001",
                                "uid": "5ccf7f140597",
                                "deviceName": "Ceiling light",
                                "deviceType": 1,
                                "roomId": "room-1",
                            },
                            {
                                "deviceId": "device-child-0002",
                                "uid": "5ccf7f140597",
                                "deviceName": "Curtain",
                                "deviceType": 34,
                                "roomId": "room-1",
                            },
                        ],
                    },
                    {
                        "tableName": "permission",
                        "dataList": [
                            {
                                "deviceId": "permission-row-0001",
                                "uid": "5ccf7f140597",
                                "userId": "user-1",
                            }
                        ],
                    },
                ],
            }
        )

        self.assertEqual(
            [device.uid for device in devices],
            ["device-child-0001", "device-child-0002"],
        )
        self.assertEqual({device.room for device in devices}, {"Kitchen"})

    def test_device_to_dict_compatible_with_https_client(self) -> None:
        """验证 device_to_dict() 与 https_client 的 dict 格式兼容。"""
        dev = protocol.OrviboDevice(
            uid="test-device-001",
            name="Test Light",
            model="S2",
            device_type="1",
            sub_device_type="1",
            room="Living Room",
            parent_uid="gateway-001",
            online=True,
            cloud_uid="hardware-uid-001",
            value1=0,
            value2=128,
            value3=250,
            value4=0,
        )
        d = protocol.device_to_dict(dev)
        self.assertEqual(d["device_id"], "test-device-001")
        self.assertEqual(d["device_name"], "Test Light")
        self.assertEqual(d["device_type_raw"], 1)
        self.assertEqual(d["room_name"], "Living Room")

    def test_parse_readtable_rooms_extracts_room_mapping(self) -> None:
        rooms = protocol.parse_readtable_rooms(
            {
                "data": {
                    "room": [
                        {"roomId": "room-1", "roomName": "Kitchen"},
                        {"roomId": "room-2", "roomName": "Living Room"},
                        {"roomId": "room-3", "roomName": "Bedroom", "delFlag": 1},
                    ],
                }
            }
        )
        self.assertEqual(rooms["room-1"], "Kitchen")
        self.assertEqual(rooms["room-2"], "Living Room")
        self.assertNotIn("room-3", rooms)

    def test_parse_families_to_dicts(self) -> None:
        dicts = protocol.parse_families_to_dicts(
            {
                "data": [
                    {"familyId": "fam-1", "familyName": "Home"},
                    {"familyId": "fam-2", "familyName": "Office"},
                ]
            }
        )
        self.assertEqual(len(dicts), 2)
        self.assertEqual(dicts[0]["familyId"], "fam-1")
        self.assertEqual(dicts[0]["familyName"], "Home")

    def test_parse_readtable_to_device_dicts(self) -> None:
        """验证 parse_readtable_to_device_dicts 产生完整的 dict 列表。"""
        dicts = protocol.parse_readtable_to_device_dicts(
            {
                "code": 0,
                "data": {
                    "room": [{"roomId": "room-1", "roomName": "Kitchen"}],
                    "device": [
                        {
                            "deviceId": "dev-001",
                            "deviceName": "Light",
                            "deviceType": 1,
                            "subDeviceType": 1,
                            "roomId": "room-1",
                        },
                    ],
                    "deviceStatus": [
                        {"deviceId": "dev-001", "online": 1, "value1": 0}
                    ],
                },
            }
        )
        self.assertEqual(len(dicts), 1)
        self.assertEqual(dicts[0]["device_id"], "dev-001")
        self.assertEqual(dicts[0]["device_name"], "Light")
        self.assertEqual(dicts[0]["room_name"], "Kitchen")

    def test_OrviboDevice_frozen(self) -> None:
        dev = protocol.OrviboDevice(
            uid="x", name="", model="", device_type="",
            sub_device_type="", room="", parent_uid="", online=None,
        )
        with self.assertRaises(AttributeError):
            dev.uid = "new-uid"  # type: ignore[misc]

    def test_OrviboFamily_frozen(self) -> None:
        fam = protocol.OrviboFamily(family_id="x", name="y")
        with self.assertRaises(AttributeError):
            fam.family_id = "z"  # type: ignore[misc]

    def test_safe_int(self) -> None:
        self.assertIsNone(protocol._safe_int(None))
        self.assertEqual(protocol._safe_int("42"), 42)
        self.assertEqual(protocol._safe_int(0), 0)
        self.assertIsNone(protocol._safe_int("not-a-number"))

    def test_parse_online_various_formats(self) -> None:
        """验证 _parse_online 支持多种格式。"""
        devs = protocol.extract_devices([
            {"uid": "xxxxxxxxxxx1", "deviceName": "A", "online": True},
            {"uid": "xxxxxxxxxxx2", "deviceName": "B", "online": 1},
            {"uid": "xxxxxxxxxxx3", "deviceName": "C", "online": "online"},
            {"uid": "xxxxxxxxxxx4", "deviceName": "D", "online": "connected"},
            {"uid": "xxxxxxxxxxx5", "deviceName": "E", "online": False},
            {"uid": "xxxxxxxxxxx6", "deviceName": "F", "online": 0},
            {"uid": "xxxxxxxxxxx7", "deviceName": "G", "online": "offline"},
        ])
        self.assertEqual(len(devs), 7)
        self.assertTrue(devs[0].online)
        self.assertTrue(devs[1].online)
        self.assertTrue(devs[2].online)
        self.assertTrue(devs[3].online)
        self.assertFalse(devs[4].online)
        self.assertFalse(devs[5].online)
        self.assertFalse(devs[6].online)

    def test_readtable_request_default_version(self) -> None:
        body = protocol.build_readtable_request(
            access_token="t", user_id="u", family_id="f",
            session_id="s", timestamp_ms=1000, serial=1, nonce="n",
        )
        self.assertEqual(body["ver"], "5.2.6.302")

    def test_first_text_multiple_keys(self) -> None:
        item = {"deviceId": "id1", "deviceID": "id2"}
        self.assertEqual(protocol._first_text(item, ("deviceId", "deviceID")), "id1")
        self.assertEqual(
            protocol._first_text({"deviceID": "id2"}, ("deviceId", "deviceID")), "id2"
        )
        self.assertEqual(protocol._first_text({}, ("a", "b")), "")

    def test_device_to_dict_infers_correct_initial_state(self) -> None:
        # type=1, value1=0 → active-low → state=True
        d1 = protocol.OrviboDevice(
            uid="d1", name="", model="", device_type="1",
            sub_device_type="1", room="", parent_uid="",
            online=True, value1=0,
        )
        self.assertTrue(protocol.device_to_dict(d1)["state"])

        # type=1, value1=1 → active-low → state=False
        d2 = protocol.OrviboDevice(
            uid="d2", name="", model="", device_type="1",
            sub_device_type="1", room="", parent_uid="",
            online=True, value1=1,
        )
        self.assertFalse(protocol.device_to_dict(d2)["state"])

        # type=135, value1=0 → active-high → state=False
        d3 = protocol.OrviboDevice(
            uid="d3", name="", model="", device_type="135",
            sub_device_type="", room="", parent_uid="",
            online=True, value1=0,
        )
        self.assertFalse(protocol.device_to_dict(d3)["state"])

        # type=135, value1=1 → active-high → state=True
        d4 = protocol.OrviboDevice(
            uid="d4", name="", model="", device_type="135",
            sub_device_type="", room="", parent_uid="",
            online=True, value1=1,
        )
        self.assertTrue(protocol.device_to_dict(d4)["state"])

        # type=34 (窗帘), value1=0 → state=False
        d5 = protocol.OrviboDevice(
            uid="d5", name="", model="", device_type="34",
            sub_device_type="", room="", parent_uid="",
            online=True, value1=0,
        )
        self.assertFalse(protocol.device_to_dict(d5)["state"])

        # type=34 (窗帘), value1=50 → state=True
        d6 = protocol.OrviboDevice(
            uid="d6", name="", model="", device_type="34",
            sub_device_type="", room="", parent_uid="",
            online=True, value1=50,
        )
        self.assertTrue(protocol.device_to_dict(d6)["state"])


if __name__ == "__main__":
    unittest.main()
