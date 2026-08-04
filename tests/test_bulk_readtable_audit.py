"""Tests for the privacy-preserving multi-family readtable audit."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "tools" / "bulk_readtable_audit.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("bulk_readtable_audit_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class BulkReadtableAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = _load_script()

    def test_analyze_distinguishes_supported_hidden_unknown_and_parser_gap(self) -> None:
        data = {
            "device": [
                {
                    "deviceId": "real-supported-id",
                    "deviceName": "卧室灯",
                    "roomName": "卧室",
                    "deviceType": 1,
                    "subDeviceType": -2,
                    "model": "S20",
                },
                {
                    "deviceId": "real-hidden-id",
                    "deviceName": "家庭网关",
                    "deviceType": 114,
                },
                {
                    "deviceId": "real-unknown-id",
                    "deviceName": "自定义名称",
                    "deviceType": 9001,
                    "protocolType": "zigbee",
                },
                {
                    "deviceId": "real-gap-id",
                    "deviceName": "遗漏设备",
                    "deviceType": 9002,
                },
            ],
            "deviceStatus": [
                {
                    "deviceId": "real-supported-id",
                    "online": 1,
                    "value1": 0,
                }
            ],
        }

        def parser(_data):
            return [
                {
                    "device_id": "real-supported-id",
                    "device_type": "light",
                    "device_type_raw": 1,
                    "sub_device_type": -2,
                },
                {
                    "device_id": "real-unknown-id",
                    "device_type_raw": 9001,
                },
            ]

        result = self.audit.analyze_family(
            data,
            parser,
            salt=b"test-salt",
            family_tag="family-test",
            max_examples=2,
        )
        self.assertEqual(
            result["counts"],
            {
                "hidden": 1,
                "parser_gap": 1,
                "registration_only": 1,
                "supported_verified": 1,
            },
        )

    def test_analyze_finds_missing_and_drifted_platform_mappings(self) -> None:
        data = {
            "device": [
                {"deviceId": "music", "deviceType": 128},
                {"deviceId": "lock", "deviceType": 107},
            ]
        }

        def parser(_data):
            return [
                {
                    "device_id": "music",
                    "device_type": "light",
                    "device_type_raw": 128,
                },
                {
                    "device_id": "lock",
                    "device_type": "light",
                    "device_type_raw": 107,
                },
            ]

        result = self.audit.analyze_family(
            data,
            parser,
            salt=b"mapping-test",
            family_tag="family-test",
            max_examples=2,
        )
        self.assertEqual(
            result["counts"],
            {"registration_only": 2},
        )

    def test_report_does_not_persist_names_or_identifiers(self) -> None:
        secret_values = (
            "real-device-id",
            "真实设备名",
            "真实房间名",
            "real-family-id",
        )
        data = {
            "device": [
                {
                    "deviceId": secret_values[0],
                    "deviceName": secret_values[1],
                    "roomName": secret_values[2],
                    "deviceType": 9999,
                    "properties": {"onoff": {"value": 1}},
                }
            ]
        }

        def parser(_data):
            return [
                {
                    "device_id": secret_values[0],
                    "device_type": "light",
                    "device_type_raw": 9999,
                }
            ]

        analysis = self.audit.analyze_family(
            data,
            parser,
            salt=b"privacy-test-salt",
            family_tag="family-fingerprint",
            max_examples=2,
        )
        state = self.audit._new_state(b"privacy-test-salt")
        self.audit.merge_family_result(
            state,
            {
                "family_index": 0,
                "family_tag": "family-fingerprint",
                "status": "ok",
                "counts": analysis["counts"],
                "raw_device_count": 1,
                "normalized_device_count": 1,
            },
            analysis,
            max_examples=2,
        )
        report_text = __import__("json").dumps(
            self.audit._public_report(state), ensure_ascii=False
        )
        for value in secret_values:
            self.assertNotIn(value, report_text)
        self.assertIn("device-", report_text)

    def test_output_state_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = self.audit._new_state(b"resume-salt")
            state["processed_family_tags"].append("family-one")
            self.audit._atomic_json(path, state)
            loaded = self.audit._load_state(path)
            self.assertEqual(loaded["processed_family_tags"], ["family-one"])
            self.assertEqual(bytes.fromhex(loaded["salt"]), b"resume-salt")

    def test_existing_zero_device_family_is_relabelled_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = self.audit._new_state(b"empty-family-salt")
            state["families"].append(
                {
                    "family_index": 0,
                    "family_tag": "family-empty",
                    "status": "ok",
                    "raw_device_count": 0,
                    "normalized_device_count": 0,
                    "counts": {},
                }
            )
            state["processed_family_tags"].append("family-empty")
            self.audit._atomic_json(path, state)

            loaded = self.audit._load_state(path)

            self.assertEqual(loaded["families"][0]["status"], "empty")

    def test_catalog_enrichment_joins_model_without_exporting_catalog_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device_catalog.json"
            path.write_text(
                __import__("json").dumps(
                    {
                        "deviceDescList": [
                            {
                                "deviceDescId": "catalog-secret-id",
                                "model": "model-one",
                                "internalModel": "HW-ONE",
                            },
                            {
                                "deviceDescId": "catalog-secret-id-2",
                                "model": "model-one",
                                "internalModel": "HW-ONE-B",
                            },
                        ],
                        "deviceLanguageList": [
                            {
                                "dataId": "catalog-secret-id",
                                "language": "zh",
                                "productName": "测试产品",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            catalog = self.audit._load_device_catalog(path)
            fields = self.audit._catalog_fields(
                {"descriptor": {"model": "model-one"}}, catalog
            )

            self.assertTrue(fields["catalog_match"])
            self.assertTrue(fields["catalog_ambiguous"])
            self.assertEqual(fields["catalog_device_desc_count"], 2)
            self.assertEqual(fields["product_names"], ["测试产品"])
            self.assertEqual(fields["internal_models"], ["HW-ONE", "HW-ONE-B"])
            exported = __import__("json").dumps(fields, ensure_ascii=False)
            self.assertNotIn("catalog-secret-id", exported)

    def test_public_report_contains_catalog_coverage(self) -> None:
        state = self.audit._new_state(b"catalog-coverage-salt")
        state["signatures"] = {
            "one": {
                "descriptor": {"model": "known"},
                "category": "unknown",
                "category_label": "未知设备",
                "support_state": "registration_only",
                "count": 3,
                "family_examples": [],
                "device_examples": [],
                "shape": {
                    "device_fields": [],
                    "status_fields": [],
                    "device_property_fields": [],
                    "status_property_fields": [],
                },
            },
            "two": {
                "descriptor": {"model": "missing"},
                "category": "unknown",
                "category_label": "未知设备",
                "support_state": "registration_only",
                "count": 1,
                "family_examples": [],
                "device_examples": [],
                "shape": {
                    "device_fields": [],
                    "status_fields": [],
                    "device_property_fields": [],
                    "status_property_fields": [],
                },
            },
        }
        catalog = {
            "known": {
                "catalog_device_desc_count": 1,
                "product_names": ["已知产品"],
                "internal_models": ["KNOWN-1"],
            }
        }

        report = self.audit._public_report(
            state, catalog, catalog_source="catalog.json"
        )

        self.assertEqual(report["catalog"]["matched_device_records"], 3)
        self.assertEqual(report["catalog"]["unmatched_device_records"], 1)
        self.assertEqual(report["catalog"]["device_record_match_rate"], 75.0)
        self.assertEqual(report["signatures"][0]["product_names"], ["已知产品"])


class BulkReadtableAuditAsyncTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = _load_script()

    async def test_read_family_accepts_missing_device_tables_as_empty(self) -> None:
        class Client:
            access_token = "token"
            user_id = "user"

            def __init__(self):
                self.flags = []

            def set_family(self, family_id):
                self.family_id = family_id

            async def ensure_login(self):
                return True

            async def _readtable(self, device_flag):
                self.flags.append(device_flag)
                return {"room": []} if device_flag == 0 else {}

        client = Client()
        result = await self.audit._read_family(
            client,
            "empty-family",
            retries=1,
            retry_delay=0,
        )

        self.assertEqual(client.flags, [0, 1])
        self.assertEqual(result["device"], [])


if __name__ == "__main__":
    unittest.main()
