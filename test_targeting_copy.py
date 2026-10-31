from __future__ import annotations

import unittest
from decimal import Decimal

from .portable_copy_workflow import snapshot_identity
from .targeting_copy import (
    _rewrite_ad_group_row,
    _rewrite_campaign_row,
    budget_gate_message,
    human_gate_for_error,
    read_targeting,
    row_has_payload,
    select_existing_budget,
    _CAMPAIGN_IDENTITY,
)


class BudgetSelectionTest(unittest.TestCase):
    def test_picks_matching_amount_and_skips_removed_when_another_matches(self) -> None:
        rows = [
            {"budget_id": "removed", "daily_budget": Decimal("5.00"), "status_raw": 7},
            {"budget_id": "live", "daily_budget": Decimal("5.00"), "status_raw": 5},
            {"budget_id": "other", "daily_budget": Decimal("8.00"), "status_raw": 5},
        ]
        self.assertEqual(select_existing_budget(rows, 5_000_000), "live")

    def test_missing_budget_is_empty(self) -> None:
        rows = [{"budget_id": "live", "daily_budget": Decimal("8.00"), "status_raw": 5}]
        self.assertEqual(select_existing_budget(rows, 5_000_000), "")
        self.assertIn("5.00", budget_gate_message(5_000_000, "USD"))


class CriterionRowTest(unittest.TestCase):
    def test_identity_only_row_is_not_a_payload(self) -> None:
        self.assertFalse(row_has_payload({"2": "9", "3": "4", "11": False}, _CAMPAIGN_IDENTITY))

    def test_location_payload_is_kept_and_ids_are_rewritten(self) -> None:
        row = {"2": "source-campaign", "3": "55", "11": False, "70": {"1": "2344"}}
        self.assertTrue(row_has_payload(row, _CAMPAIGN_IDENTITY))
        entity = _rewrite_campaign_row(row, "target-ocid", "new-campaign", "-1")
        self.assertEqual(entity["1"], "target-ocid")
        self.assertEqual(entity["2"], "new-campaign")
        self.assertEqual(entity["3"], "-1")
        self.assertEqual(entity["70"], {"1": "2344"})
        self.assertEqual(row["2"], "source-campaign")

    def test_ad_group_topic_row_points_at_the_target_group(self) -> None:
        row = {"2": "source-campaign", "3": "source-group", "4": "99", "42": {"1": "18"}}
        entity = _rewrite_ad_group_row(row, "target-ocid", "new-group", "-2")
        self.assertEqual(entity["1"], "target-ocid")
        self.assertEqual(entity["3"], "new-group")
        self.assertEqual(entity["4"], "-2")
        self.assertNotIn("2", entity)
        self.assertEqual(entity["42"], {"1": "18"})


class HumanGateTest(unittest.TestCase):
    def test_unphishable_url_asks_for_the_key(self) -> None:
        gate = human_gate_for_error(
            "新建广告", "AD_FINAL_URL_UNPHISHABLE", final_url="https://example.com",
        )
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate.reason, "final_url")
        self.assertIn("https://example.com", str(gate))
        self.assertIn("密钥", str(gate))

    def test_reauth_asks_for_the_key(self) -> None:
        gate = human_gate_for_error("新建广告系列", "AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED")
        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(gate.reason, "reauth")

    def test_other_errors_stay_errors(self) -> None:
        self.assertIsNone(human_gate_for_error("新建广告组", "FIELD_ERROR_REQUIRED"))


class ReadTargetingTest(unittest.TestCase):
    def test_location_row_is_kept_and_blank_language_row_is_a_gap_only_if_uncovered(self) -> None:
        location_row = {"2": "c", "3": "55", "11": False, "70": {"1": "2344"}}
        keyword_only = {"2": "c", "3": "77", "11": True}

        class Page:
            def rpc(self, path: str, request: dict) -> dict:
                fields = request["2"]["1"]
                if "location.geo_target_constant" in fields and "ad_group_id" not in fields:
                    return {"1": [location_row]}
                if "type" in fields:
                    return {"1": [location_row, keyword_only]}
                return {"1": []}

        result = read_targeting(Page(), "ocid", "c", [])
        self.assertEqual(len(result["campaign_criteria"]), 1)
        self.assertEqual(result["campaign_criteria"][0]["kind"], "location")
        self.assertEqual(result["targeting_gaps"][0]["source_criterion_id"], "77")


class SnapshotIdentityTest(unittest.TestCase):
    def test_old_snapshot_hash_ignores_missing_targeting_keys(self) -> None:
        base = {
            "source": {"cid": "1", "campaign_id": "2"},
            "campaign": {"11": "A"},
            "ad_groups": [],
            "ads": [],
            "assets": [],
            "asset_references": [],
            "negative_keywords": [],
            "shared_sets": [],
            "custom_audiences": [],
        }
        original = snapshot_identity(base)
        self.assertEqual(snapshot_identity(dict(base)), original)
        changed = dict(base)
        changed["campaign_criteria"] = [{"kind": "location"}]
        self.assertNotEqual(snapshot_identity(changed), original)


if __name__ == "__main__":
    unittest.main()
