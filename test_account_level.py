from __future__ import annotations

import unittest

from .account_level_copy import (
    CAMPAIGN_GOAL_MUTATE_PATH,
    CUSTOMER_MUTATE_PATH,
    INVENTORY_FIELD,
    apply_conversion_actions,
    apply_exclusions,
    apply_inventory,
    exclusion_fingerprint,
    is_contact_action,
    missing_exclusions,
    read_account_level,
    retarget_exclusion,
    retarget_goal_row,
)
from .portable_copy_workflow import snapshot_identity
from .targeting_copy import HumanActionRequired


SOURCE = "8429376413"
TARGET = "7660960056"
CAMPAIGN = "24102672126"


def _website(ocid: str, criterion_id: str, url: str) -> dict:
    return {"1": ocid, "3": criterion_id, "114": {"1": url}}


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_ignores_account_and_criterion_ids(self) -> None:
        left = exclusion_fingerprint(_website(SOURCE, "111122223333", "https://a.example"), {SOURCE})
        right = exclusion_fingerprint(_website(TARGET, "999", "https://a.example"), {TARGET})
        self.assertEqual(left, right)
        other = exclusion_fingerprint(_website(SOURCE, "111122223333", "https://b.example"), {SOURCE})
        self.assertNotEqual(left, other)

    def test_int_and_string_enums_match(self) -> None:
        left = exclusion_fingerprint({"1": SOURCE, "3": "5", "9": 4}, {SOURCE})
        right = exclusion_fingerprint({"1": TARGET, "3": "8", "9": "4"}, {TARGET})
        self.assertEqual(left, right)

    def test_missing_rows_are_the_ones_b_does_not_have(self) -> None:
        source = [
            {"row": _website(SOURCE, "1", "https://keep.example")},
            {"row": _website(SOURCE, "2", "https://add.example")},
        ]
        target = [_website(TARGET, "9", "https://keep.example")]
        missing = missing_exclusions(
            source, target,
            source_ignore={SOURCE}, target_ignore={TARGET}, applied=set(),
        )
        self.assertEqual(
            [item[1]["row"]["114"]["1"] for item in missing],
            ["https://add.example"],
        )


class RetargetTest(unittest.TestCase):
    def test_exclusion_rewrites_ids_and_keeps_nested_constants(self) -> None:
        row = {
            "1": SOURCE,
            "3": "111122223333",
            "114": {"1": "https://a.example"},
            "23": {"1": "1234567890123"},
        }
        entity = retarget_exclusion(
            row, target_ocid=TARGET, temporary_id="-1",
            source_ocid=SOURCE, source_campaign_id=CAMPAIGN,
        )
        self.assertEqual(entity["1"], TARGET)
        self.assertEqual(entity["3"], "-1")
        self.assertEqual(entity["114"]["1"], "https://a.example")
        self.assertEqual(entity["23"]["1"], "1234567890123")
        self.assertEqual(row["3"], "111122223333")

    def test_goal_row_follows_the_new_campaign(self) -> None:
        row = {"1": SOURCE, "2": CAMPAIGN, "9": 18, "10": True}
        entity = retarget_goal_row(
            row, target_ocid=TARGET, target_campaign_id="555",
            temporary_id="-1", source_ocid=SOURCE, source_campaign_id=CAMPAIGN,
        )
        self.assertEqual(entity["1"], TARGET)
        self.assertEqual(entity["2"], "555")
        self.assertEqual(entity["9"], 18)
        self.assertIs(entity["10"], True)


class _Recorder:
    def __init__(self, rows_for_fields):
        self.rows_for_fields = rows_for_fields
        self.calls: list[tuple[str, dict]] = []
        self.fail_mutate = False

    def rpc(self, path: str, request: dict) -> dict:
        self.calls.append((path, request))
        if path.endswith("/Mutate"):
            if self.fail_mutate:
                raise RuntimeError("AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED")
            return {"1": {}}
        fields = request["2"]["1"]
        for key, rows in self.rows_for_fields.items():
            if key in fields:
                return {"1": rows}
        return {"1": []}


class ReadAndApplyTest(unittest.TestCase):
    def test_one_failed_spec_is_a_gap_and_the_website_is_kept(self) -> None:
        class Page:
            def rpc(self, path: str, request: dict) -> dict:
                fields = request["2"]["1"]
                if "content_label.type" in fields:
                    raise RuntimeError("unknown field")
                if "placement.url" in fields:
                    return {"1": [_website(SOURCE, "7", "https://a.example")]}
                if "name" in fields:
                    return {"1": [
                        {"1": SOURCE, "3": "10", "4": "购买"},
                        {"1": SOURCE, "3": "11", "4": "联系人", "5": 18},
                    ]}
                if "campaign_conversion_goal.category" in fields:
                    return {"1": [{"1": SOURCE, "2": CAMPAIGN, "9": 18, "10": True}]}
                if INVENTORY_FIELD in fields:
                    return {"1": [{"1": SOURCE, "12": 4}]}
                return {"1": []}

        result = read_account_level(Page(), SOURCE, CAMPAIGN)
        self.assertEqual(
            [item["kind"] for item in result["exclusions"]],
            ["website"],
        )
        self.assertTrue(any(gap["kind"] == "content_label" for gap in result["gaps"]))
        self.assertEqual(len(result["conversion_actions"]), 1)
        self.assertTrue(is_contact_action(result["conversion_actions"][0]["row"]))
        self.assertFalse(is_contact_action({"4": "购买"}))
        self.assertEqual(result["inventory"]["row"]["12"], 4)
        self.assertEqual(len(result["campaign_goals"]), 1)
        self.assertIn("/Mutate", result["exclusions"][0]["mutate_path"])

    def test_every_exclusion_spec_failing_stops_the_export(self) -> None:
        class Page:
            def rpc(self, path: str, request: dict) -> dict:
                raise RuntimeError("list down")

        with self.assertRaises(RuntimeError) as caught:
            read_account_level(Page(), SOURCE, CAMPAIGN)
        self.assertIn("全部读取失败", str(caught.exception))

    def test_reauth_stops_for_the_key(self) -> None:
        class Page:
            def rpc(self, path: str, request: dict) -> dict:
                raise RuntimeError("AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED")

        with self.assertRaises(HumanActionRequired) as caught:
            read_account_level(Page(), SOURCE, CAMPAIGN)
        self.assertEqual(caught.exception.reason, "reauth")
        self.assertIn("密钥", str(caught.exception))

    def test_duplicate_exclusion_is_not_written(self) -> None:
        page = _Recorder({
            "placement.url": [_website(TARGET, "9", "https://a.example")],
        })
        state: dict = {}
        account_level = {
            "source_ocid": SOURCE,
            "source_campaign_id": CAMPAIGN,
            "exclusions": [
                {
                    "kind": "website",
                    "section": "排除的网站",
                    "write_fields": ["customer_id", "criterion_id", "placement"],
                    "mutate_path": "/aw_dva/_/rpc/NegativeTargetedContentService/Mutate",
                    "row": _website(SOURCE, "1", "https://a.example"),
                },
                {
                    "kind": "website",
                    "section": "排除的网站",
                    "write_fields": ["customer_id", "criterion_id", "placement"],
                    "mutate_path": "/aw_dva/_/rpc/NegativeTargetedContentService/Mutate",
                    "row": _website(SOURCE, "2", "https://a.example"),
                },
            ],
        }
        wrote = apply_exclusions(page, TARGET, account_level, state, lambda _text: None)
        self.assertFalse(wrote)
        self.assertFalse(any(path.endswith("/Mutate") for path, _request in page.calls))
        self.assertTrue(state["account_exclusion_keys"])

    def test_missing_exclusion_uses_create(self) -> None:
        page = _Recorder({"placement.url": []})
        state: dict = {}
        account_level = {
            "source_ocid": SOURCE,
            "source_campaign_id": CAMPAIGN,
            "exclusions": [{
                "kind": "website",
                "section": "排除的网站",
                "write_fields": ["customer_id", "criterion_id", "placement"],
                "mutate_path": "/aw_dva/_/rpc/NegativeTargetedContentService/Mutate",
                "row": _website(SOURCE, "1", "https://add.example"),
            }],
        }
        wrote = apply_exclusions(page, TARGET, account_level, state, lambda _text: None)
        self.assertTrue(wrote)
        mutates = [request for path, request in page.calls if path.endswith("/Mutate")]
        self.assertEqual(len(mutates), 1)
        operation = mutates[0]["2"][0]
        self.assertEqual(operation["1"], 1)
        self.assertEqual(operation["3"]["1"], TARGET)
        self.assertEqual(operation["3"]["3"], "-1")
        self.assertEqual(operation["3"]["114"]["1"], "https://add.example")

    def test_contact_goal_already_on_b_is_not_created_again(self) -> None:
        page = _Recorder({
            "name": [{"1": TARGET, "3": "50", "4": "联系人", "5": 18}],
        })
        state: dict = {}
        account_level = {
            "source_ocid": SOURCE,
            "conversion_actions": [{
                "write_fields": ["name", "category", "status", "type"],
                "mutate_path": "/aw/_/rpc/ConversionTypeService/Mutate",
                "row": {"1": SOURCE, "3": "11", "4": "联系人", "5": 18},
            }],
        }
        wrote = apply_conversion_actions(page, TARGET, account_level, state, lambda _text: None)
        self.assertFalse(wrote)
        self.assertFalse(any(path.endswith("/Mutate") for path, _request in page.calls))
        self.assertTrue(state["conversion_actions_ready"])

    def test_inventory_update_uses_operator_3_and_skips_the_same_value(self) -> None:
        same = _Recorder({INVENTORY_FIELD: [{"1": TARGET, "12": 4}]})
        state: dict = {}
        account_level = {
            "source_ocid": SOURCE,
            "inventory": {"row": {"1": SOURCE, "12": 4}},
        }
        self.assertFalse(apply_inventory(same, TARGET, account_level, state, lambda _text: None))
        self.assertTrue(state["inventory_applied"])
        self.assertFalse(any(path == CUSTOMER_MUTATE_PATH for path, _request in same.calls))

        changed = _Recorder({INVENTORY_FIELD: [{"1": TARGET, "12": 2}]})
        state = {}
        self.assertTrue(apply_inventory(changed, TARGET, account_level, state, lambda _text: None))
        operation = next(request for path, request in changed.calls if path == CUSTOMER_MUTATE_PATH)["2"][0]
        self.assertEqual(operation["1"], 3)
        self.assertEqual(operation["2"], [INVENTORY_FIELD])
        self.assertEqual(operation["3"]["1"], TARGET)
        self.assertEqual(operation["3"]["12"], 4)

    def test_write_reauth_asks_for_the_key(self) -> None:
        page = _Recorder({"placement.url": []})
        page.fail_mutate = True
        account_level = {
            "source_ocid": SOURCE,
            "exclusions": [{
                "kind": "website",
                "section": "排除的网站",
                "write_fields": ["customer_id", "criterion_id", "placement"],
                "mutate_path": "/aw_dva/_/rpc/NegativeTargetedContentService/Mutate",
                "row": _website(SOURCE, "1", "https://add.example"),
            }],
        }
        with self.assertRaises(HumanActionRequired):
            apply_exclusions(page, TARGET, account_level, {}, lambda _text: None)

    def test_campaign_goal_mutate_path_is_the_inferred_service(self) -> None:
        self.assertTrue(CAMPAIGN_GOAL_MUTATE_PATH.endswith("CampaignConversionGoalService/Mutate"))


class SnapshotIdentityTest(unittest.TestCase):
    def test_old_snapshot_hash_ignores_account_level_until_the_key_exists(self) -> None:
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
        with_note = dict(base)
        with_note["content_suitability"] = None
        self.assertEqual(snapshot_identity(with_note), original)
        changed = dict(base)
        changed["account_level"] = {"exclusions": [{"kind": "website"}]}
        self.assertNotEqual(snapshot_identity(changed), original)


if __name__ == "__main__":
    unittest.main()
