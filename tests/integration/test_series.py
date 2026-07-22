from __future__ import annotations

import unittest

from imagegen.errors import ServiceError
from imagegen.services.series import SeriesAnchor


class TestSeriesAnchor(unittest.TestCase):
    def test_parse_sanitizes_and_serializes_the_supported_contract(self):
        anchor = SeriesAnchor.parse(
            {
                "asset_id": "A" * 32,
                "source_item_id": "B" * 40,
                "contract": {
                    "identity_anchors": [" 同一主体 ", "同一主体", ""],
                    "allowed_changes": ["动作和场景"],
                    "unsupported": ["不会保留"],
                },
            }
        )

        self.assertIsNotNone(anchor)
        self.assertEqual(
            anchor.as_dict(),
            {
                "asset_id": "a" * 32,
                "source_item_id": "b" * 32,
                "contract": {
                    "identity_anchors": ["同一主体"],
                    "allowed_changes": ["动作和场景"],
                },
            },
        )

    def test_parse_rejects_invalid_ids_and_empty_contracts(self):
        self.assertIsNone(
            SeriesAnchor.parse(
                {
                    "asset_id": "z" * 32,
                    "contract": {"identity_anchors": ["主体"]},
                }
            )
        )
        self.assertIsNone(
            SeriesAnchor.parse(
                {
                    "asset_id": "a" * 32,
                    "contract": {"unsupported": ["无效规则"]},
                }
            )
        )

    def test_require_keeps_missing_and_invalid_errors_distinct(self):
        with self.assertRaises(ServiceError) as missing:
            SeriesAnchor.require(None)
        self.assertEqual(missing.exception.status_code, 409)
        self.assertEqual(missing.exception.code, "invalid_request")

        with self.assertRaises(ServiceError) as invalid:
            SeriesAnchor.require({})
        self.assertEqual(invalid.exception.status_code, 409)
        self.assertEqual(invalid.exception.code, "series_anchor_invalid")

        with self.assertRaises(ServiceError) as planning_invalid:
            SeriesAnchor.require(
                {},
                invalid_message="系列基准已失效，请重新选择",
                invalid_code="invalid_request",
            )
        self.assertEqual(str(planning_invalid.exception), "系列基准已失效，请重新选择")
        self.assertEqual(planning_invalid.exception.code, "invalid_request")

    def test_reference_order_always_places_the_anchor_first(self):
        anchor = SeriesAnchor.parse(
            {
                "asset_id": "a" * 32,
                "source_item_id": "b" * 32,
                "contract": {"identity_anchors": ["主体"]},
            }
        )

        self.assertIsNotNone(anchor)
        self.assertEqual(
            anchor.order_reference_ids(("c" * 32, "a" * 32, "d" * 32)),
            ("a" * 32, "c" * 32, "d" * 32),
        )
