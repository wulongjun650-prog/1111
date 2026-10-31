from __future__ import annotations

import unittest

import json

from .portable_copy import (
    _service_error,
    claim_named_group,
    copy_failure_message,
    place_ad_with_retries,
    poll_until_one,
    portable_display_ad_request,
    recover_after_poll,
    verification_failure,
)
from .protocol import rpc_http_failure, rpc_rejected_message
from .targeting_copy import HumanActionRequired, human_gate_for_error


def _ad(ad_id: str, status: int, group: str = "g") -> dict:
    return {"ad_id": ad_id, "ad_group_id": group, "status_raw": status}


class PlaceAdRetryTest(unittest.TestCase):
    def _run(self, reads: list, submits: list | None = None, polls: list | None = None):
        submit_calls: list[int] = []
        poll_calls: list[int] = []
        read_calls: list[int] = []
        submit_payloads = list(submits or [])
        poll_payloads = list(polls or [])

        def read_ads() -> list:
            read_calls.append(1)
            if not reads:
                return []
            return reads.pop(0)

        def submit() -> dict:
            submit_calls.append(1)
            if not submit_payloads:
                return {}
            item = submit_payloads.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def poll(_known: set[str]) -> dict:
            poll_calls.append(1)
            if not poll_payloads:
                raise RuntimeError("广告协议返回后未能回读新对象")
            item = poll_payloads.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        try:
            ad, submitted = place_ad_with_retries(
                read_ads=read_ads,
                submit=submit,
                poll_new_ad=poll,
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                log=lambda _text: None,
                gate_for_error=lambda error: human_gate_for_error("新建广告", error),
                pause=lambda _seconds: None,
            )
        except Exception:
            return {
                "reads": len(read_calls),
                "submits": len(submit_calls),
                "polls": len(poll_calls),
                "error": True,
            }
        return {
            "ad": ad,
            "submitted": submitted,
            "submits": len(submit_calls),
            "polls": len(poll_calls),
            "error": False,
        }

    def test_paused_ad_already_there_is_reused(self) -> None:
        result = self._run([[_ad("9", 2)]])
        self.assertEqual(result["ad"]["ad_id"], "9")
        self.assertFalse(result["submitted"])
        self.assertEqual(result["submits"], 0)

    def test_second_try_finds_the_ad(self) -> None:
        result = self._run(
            [[], [], [_ad("9", 2)]],
            submits=[{}, {}],
            polls=[RuntimeError("广告协议返回后未能回读新对象")],
        )
        self.assertEqual(result["ad"]["ad_id"], "9")
        self.assertEqual(result["submits"], 1)
        self.assertTrue(result["submitted"])

    def test_three_misses_explain_what_was_seen(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            place_ad_with_retries(
                read_ads=lambda: [],
                submit=lambda: {},
                poll_new_ad=lambda _known: (_ for _ in ()).throw(
                    RuntimeError("广告协议返回后未能回读新对象")
                ),
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                log=lambda _text: None,
                pause=lambda _seconds: None,
            )
        message = str(caught.exception)
        self.assertIn("试了 3 次还没成功", message)
        self.assertIn("没有新广告", message)
        self.assertIn("没有再建系列", message)
        self.assertIn("没有要求物理密钥", message)
        self.assertEqual(message.count("第"), 3)

    def test_wrong_status_is_not_created_again(self) -> None:
        submits: list[int] = []

        def submit() -> dict:
            submits.append(1)
            return {}

        with self.assertRaises(RuntimeError) as caught:
            place_ad_with_retries(
                read_ads=lambda: [_ad("9", 1)],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                log=lambda _text: None,
                pause=lambda _seconds: None,
            )
        self.assertEqual(submits, [])
        self.assertIn("已启用", str(caught.exception))
        self.assertIn("不是已暂停", str(caught.exception))

    def test_reauth_stops_on_the_first_try(self) -> None:
        submits: list[int] = []

        def submit() -> dict:
            submits.append(1)
            return {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}}

        with self.assertRaises(HumanActionRequired):
            place_ad_with_retries(
                read_ads=lambda: [],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                log=lambda _text: None,
                gate_for_error=lambda error: human_gate_for_error("新建广告", error),
                pause=lambda _seconds: None,
            )
        self.assertEqual(submits, [1])

    def test_several_paused_ads_use_the_newest(self) -> None:
        result = self._run([[_ad("10", 2), _ad("30", 2)]])
        self.assertEqual(result["ad"]["ad_id"], "30")
        self.assertEqual(result["submits"], 0)

    def test_unready_ad_is_described_without_another_create(self) -> None:
        submits: list[int] = []

        def submit() -> dict:
            submits.append(1)
            return {}

        with self.assertRaises(RuntimeError) as caught:
            place_ad_with_retries(
                read_ads=lambda: [_ad("9", 0)],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                log=lambda _text: None,
                pause=lambda _seconds: None,
            )
        self.assertEqual(submits, [])
        self.assertIn("还没有状态", str(caught.exception))

    def test_one_late_row_is_accepted_after_the_poll_gives_up(self) -> None:
        def reader():
            return [{"2": "9", "name": "A", "status_raw": 2}]

        def poll():
            raise RuntimeError("广告组协议返回后未能回读新对象")

        found = recover_after_poll(
            poll,
            reader,
            lambda row: row.get("name") == "A",
            lambda rows: "说明",
            "广告组",
        )
        self.assertEqual(found["2"], "9")

    def test_missing_row_says_what_the_account_has(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            recover_after_poll(
                lambda: (_ for _ in ()).throw(RuntimeError("广告系列协议返回后未能回读新对象")),
                lambda: [{"name": "cv2da", "status_raw": 3}],
                lambda row: row.get("name") == "cv2da" and row.get("status_raw") == 5,
                lambda rows: "找到「cv2da」，状态是已启用",
                "广告系列",
            )
        self.assertIn("没有按预期出现", str(caught.exception))
        self.assertIn("已启用", str(caught.exception))

    def test_verification_names_the_missing_ad(self) -> None:
        message = verification_failure({"ok": False, "checks": {
            "all_ads_exist_expected_status": False,
        }})
        self.assertIn("广告不在或状态不对", message)

    def test_same_name_group_with_another_status_is_not_created(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            claim_named_group(
                [{
                    "campaign_id": "c",
                    "ad_group_id": "4",
                    "name": "组",
                    "status_raw": 0,
                }],
                campaign_id="c",
                name="组",
                expected_status=2,
                mapped_ids=set(),
            )
        self.assertIn("还没有状态", str(caught.exception))
        self.assertIsNone(claim_named_group(
            [],
            campaign_id="c",
            name="组",
            expected_status=2,
            mapped_ids=set(),
        ))

    def test_poll_names_what_the_last_read_saw(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            poll_until_one(
                lambda: [{"name": "cv2da", "campaign_id": "9", "status_raw": 3}],
                lambda row: row.get("status_raw") == 5,
                "广告系列",
                campaign=True,
                attempts=1,
                pause=lambda _seconds: None,
            )
        message = str(caught.exception)
        self.assertIn("查了 1 次", message)
        self.assertIn("cv2da", message)
        self.assertIn("已启用", message)

    def test_copy_failure_puts_the_reason_before_the_progress_file(self) -> None:
        message = copy_failure_message(
            RuntimeError("新建广告失败：写入被拒绝（MUTATE_ERROR_X）"),
            step="groups_created",
            state_path=r"C:\state\rebuild.json",
        )
        self.assertLess(message.index("原因："), message.index("进度文件："))
        self.assertIn("广告组", message)
        self.assertIn("MUTATE_ERROR_X", message)

    def test_rpc_rejection_keeps_the_code_and_a_chinese_meaning(self) -> None:
        message = rpc_rejected_message("/aw/_/rpc/AdGroupAdService/Mutate", {
            "5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]},
        })
        self.assertIn("AdGroupAdService/Mutate", message)
        self.assertIn("密钥", message)
        self.assertIn("AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED", message)
        http = rpc_http_failure("/aw/_/rpc/CampaignService/List", 0, "<html>denied</html>")
        self.assertIn("HTTP 0", http)
        self.assertIn("没有连上页面", http)
        self.assertNotIn("<html>", http)

    def test_batch_error_hidden_in_a_json_string_is_read(self) -> None:
        message = _service_error({
            "1": ['{"5":{"2":["FIELD_ERROR_REQUIRED"]}}'],
        })
        self.assertIn("FIELD_ERROR_REQUIRED", message)
        self.assertIn("字段", message)

    def test_field_error_is_not_retried(self) -> None:
        submits: list[int] = []

        def submit() -> dict:
            submits.append(1)
            return {"1": ['{"5":{"2":["FIELD_ERROR_REQUIRED"]}}']}

        with self.assertRaises(RuntimeError) as caught:
            place_ad_with_retries(
                read_ads=lambda: [],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                log=lambda _text: None,
                pause=lambda _seconds: None,
            )
        self.assertEqual(submits, [1])
        self.assertIn("新建广告被拒绝", str(caught.exception))
        self.assertIn("FIELD_ERROR_REQUIRED", str(caught.exception))

    def test_display_ad_drops_source_asset_ids(self) -> None:
        source = {"13": {"3": 103, "19": 35, "100": {"1": ["https://shop.example/a"], "41": {
            "5": [{"1": {"1": "999999999999", "3": {"1": "标题一"}, "17": 1}}],
            "6": {"1": {"1": "888888888888", "3": {"1": "长标题"}, "17": 1}},
            "7": [{"1": {"3": {"1": "描述一"}, "17": 1}}],
            "8": "商店名",
            "9": [{"1": {"1": "555555555555", "17": 1}}],
        }}}}
        request = portable_display_ad_request(
            ocid="7660960056",
            campaign_id="10",
            ad_group_id="20",
            temporary_id="-1",
            source_ad=source,
            landscape_assets=[{"target_asset_id": "111111111111", "filename": "l.jpg"}],
            square_assets=[{"target_asset_id": "222222222222", "filename": "s.jpg"}],
        )
        body = json.dumps(request, ensure_ascii=False)
        self.assertNotIn("999999999999", body)
        self.assertNotIn("888888888888", body)
        self.assertNotIn("555555555555", body)
        self.assertIn("111111111111", body)
        self.assertIn("标题一", body)
        self.assertIn("商店名", body)


if __name__ == "__main__":
    unittest.main()
