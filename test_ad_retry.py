from __future__ import annotations

import unittest

import json

from .portable_copy import (
    _service_error,
    claim_named_group,
    copy_failure_message,
    place_ad_letting_user_unlock,
    place_ad_with_retries,
    poll_until_one,
    portable_display_ad_request,
    recover_after_poll,
    verification_failure,
)
from .protocol import (
    capture_reauth_header,
    rapt_from_location,
    reauth_header_from_event,
    rpc_http_failure,
    rpc_rejected_message,
    security_key_page_url,
    user_saved_on_page,
)
from .targeting_copy import HumanActionRequired, human_gate_for_error
from .user_confirm import user_confirmation


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
        invalid = rpc_rejected_message("/aw/_/rpc/AdGroupAdService/Mutate", {
            "5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_INVALID"]},
        })
        self.assertIn("没有弹出密钥", invalid)
        self.assertIn("AUTH_ERROR_REAUTH_PROOF_TOKEN_INVALID", invalid)
        self.assertNotIn("摸一下密钥", invalid)
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


class ReauthCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        user_confirmation.clear()

    def tearDown(self) -> None:
        user_confirmation.clear()

    def test_header_is_taken_from_the_page_request(self) -> None:
        name, value = reauth_header_from_event({
            "params": {"request": {"headers": {
                "X-Framework-Xsrf-Token": "abc:1",
                "X-Goog-First-Party-Reauth": "rapt-token",
            }}},
        })
        self.assertEqual(name, "X-Goog-First-Party-Reauth")
        self.assertEqual(value, "rapt-token")

    def test_rapt_query_is_a_header(self) -> None:
        name, value = reauth_header_from_event({
            "params": {"request": {"url": "https://ads.google.com/aw?rapt=from-url", "headers": {}}},
        })
        self.assertEqual((name, value), ("X-Goog-First-Party-Reauth", "from-url"))

    def test_rapt_in_the_verify_page_address_is_kept(self) -> None:
        token = "real-rapt-from-the-key-window"
        self.assertEqual(
            rapt_from_location(f"https://ads.google.com/aw/overview?rapt={token}"),
            token,
        )
        self.assertEqual(rapt_from_location("https://ads.google.com/aw/overview"), "")
        self.assertIn("accounts.google.com/account/reauth", security_key_page_url())

    def test_xsrf_alone_is_not_a_reauth_token(self) -> None:
        self.assertEqual(reauth_header_from_event({
            "params": {"request": {"headers": {"X-Framework-Xsrf-Token": "abc:1"}}},
        }), ("", ""))

    def test_capture_returns_the_first_token_and_stops(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.extra_headers: dict[str, str] = {}
                self.calls: list[str] = []

            def command(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
                self.calls.append(method)
                return {}

            def drain_events(self, prefix: str = "") -> list:
                return [{
                    "method": "Network.requestWillBeSent",
                    "params": {"request": {"headers": {"X-Goog-First-Party-Reauth": "tok"}}},
                }]

        page = Page()
        self.assertEqual(capture_reauth_header(page, timeout=0, pause=lambda _seconds: None), (
            "X-Goog-First-Party-Reauth", "tok",
        ))
        self.assertEqual(page.calls, ["Network.enable"])

    def test_ad_create_retries_once_after_the_page_sends_the_token(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.extra_headers: dict[str, str] = {}

            def command(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
                return {}

            def drain_events(self, prefix: str = "") -> list:
                return [{
                    "params": {"request": {"headers": {"X-Goog-First-Party-Reauth": "tok"}}},
                }]

        submits: list[int] = []

        def submit() -> dict:
            submits.append(1)
            if len(submits) == 1:
                return {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}}
            return {}

        page = Page()
        logs: list[str] = []
        ad, submitted = place_ad_letting_user_unlock(
            page,
            account="110-669-3913",
            log=logs.append,
            read_ads=lambda: [{"ad_id": "9", "ad_group_id": "g", "status_raw": 2}] if len(submits) > 1 else [],
            submit=submit,
            poll_new_ad=lambda _known: {"ad_id": "9", "ad_group_id": "g", "status_raw": 2},
            group_id="g",
            expected_status=2,
            reserved_ids=set(),
            gate_for_error=lambda error: human_gate_for_error("新建广告", error, account="110-669-3913"),
            pause=lambda _seconds: None,
        )
        self.assertEqual(ad["ad_id"], "9")
        self.assertTrue(submitted)
        self.assertEqual(submits, [1, 1])
        self.assertEqual(page.extra_headers, {"X-Goog-First-Party-Reauth": "tok"})
        self.assertTrue(any("密钥" in line for line in logs))
        self.assertTrue(any("正在建广告" in line for line in logs))

    def test_wait_ends_when_the_page_never_sends_a_token(self) -> None:
        class Page:
            def command(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
                return {}

            def drain_events(self, prefix: str = "") -> list:
                return []

        with self.assertRaises(HumanActionRequired) as caught:
            place_ad_letting_user_unlock(
                Page(),
                account="110-669-3913",
                log=lambda _text: None,
                read_ads=lambda: [],
                submit=lambda: {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}},
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                gate_for_error=lambda error: human_gate_for_error("新建广告", error),
                pause=lambda _seconds: None,
                reauth_timeout=0,
            )
        self.assertIn("3 分钟", str(caught.exception))
        self.assertIn("广告没有建", str(caught.exception))

    def test_our_own_write_is_not_treated_as_the_user(self) -> None:
        event = {"params": {"request": {
            "method": "POST",
            "url": "https://ads.google.com/aw_cm/editing/_/rpc/BatchService/Batch",
            "headers": {"X-GGAds-Copy": "1", "X-Goog-First-Party-Reauth": "tok"},
        }}}
        self.assertEqual(reauth_header_from_event(event), ("", ""))
        self.assertFalse(user_saved_on_page(event))

    def test_budget_save_without_a_key_popup_still_creates_the_ad(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.extra_headers: dict[str, str] = {}

            def command(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
                return {}

            def drain_events(self, prefix: str = "") -> list:
                return [{
                    "params": {"request": {
                        "method": "POST",
                        "url": "https://ads.google.com/aw_cm/_/rpc/CampaignBudgetService/Mutate",
                        "headers": {"X-Framework-Xsrf-Token": "abc:1"},
                    }},
                }]

        submits: list[int] = []

        def submit() -> dict:
            submits.append(1)
            if len(submits) == 1:
                return {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}}
            return {}

        with self.assertRaises(HumanActionRequired) as caught:
            place_ad_letting_user_unlock(
                Page(),
                account="110-669-3913",
                log=lambda _text: None,
                read_ads=lambda: [],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                gate_for_error=lambda error: human_gate_for_error("新建广告", error),
                pause=lambda _seconds: None,
                reauth_timeout=0,
            )
        self.assertEqual(submits, [1])
        self.assertIn("广告没有建", str(caught.exception))
        self.assertIn("预算不用改", str(caught.exception))

    def test_black_verification_line_is_enough(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.extra_headers: dict[str, str] = {}

            def command(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
                return {}

            def drain_events(self, prefix: str = "") -> list:
                return []

            def evaluate(self, expression: str, timeout: float = 45) -> bool:
                return True

        submits: list[int] = []

        def submit() -> dict:
            submits.append(1)
            if len(submits) == 1:
                return {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}}
            return {}

        with self.assertRaises(HumanActionRequired) as caught:
            place_ad_letting_user_unlock(
                Page(),
                account="110-669-3913",
                log=lambda _text: None,
                read_ads=lambda: [],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                gate_for_error=lambda error: human_gate_for_error("新建广告", error),
                pause=lambda _seconds: None,
                reauth_timeout=0,
            )
        self.assertEqual(submits, [1])
        self.assertIn("验证页", str(caught.exception))

    def test_a_stale_verification_line_does_not_ask_for_another_save(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.extra_headers: dict[str, str] = {}

            def command(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
                return {}

            def drain_events(self, prefix: str = "") -> list:
                return [{
                    "params": {"request": {
                        "method": "POST",
                        "url": "https://ads.google.com/aw_cm/editing/_/rpc/BatchService/Batch",
                        "headers": {},
                    }},
                }]

            def evaluate(self, expression: str, timeout: float = 45) -> bool:
                return "完成身份验证" in expression or expression.endswith(">= 0; })()")

        submits: list[int] = []
        logs: list[str] = []

        def submit() -> dict:
            submits.append(1)
            return {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}}

        with self.assertRaises(HumanActionRequired) as caught:
            place_ad_letting_user_unlock(
                Page(),
                account="110-669-3913",
                log=logs.append,
                read_ads=lambda: [],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                gate_for_error=lambda error: human_gate_for_error("新建广告", error),
                pause=lambda _seconds: None,
                reauth_timeout=0.05,
            )
        self.assertEqual(submits, [1])
        self.assertFalse(any("正在建广告" in line for line in logs))
        self.assertNotIn("再改一次", " ".join(logs))
        self.assertIn("广告没有建", str(caught.exception))
        self.assertIn("预算不用改", str(caught.exception))

    def _reauth_page(self):
        class Page:
            def __init__(self) -> None:
                self.extra_headers: dict[str, str] = {}
                self.commands: list[str] = []

            def command(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
                self.commands.append(method)
                return {}

            def drain_events(self, prefix: str = "") -> list:
                return []

        return Page()

    def _place(self, page, submit, logs: list[str], submits: list[int], *, reads=None):
        def wrapped() -> dict:
            submits.append(1)
            return submit()

        def read_ads() -> list:
            if reads is None:
                return [{"ad_id": "9", "ad_group_id": "g", "status_raw": 2}] if len(submits) > 1 else []
            return reads()

        return place_ad_letting_user_unlock(
            page,
            account="110-669-3913",
            log=logs.append,
            read_ads=read_ads,
            submit=wrapped,
            poll_new_ad=lambda _known: {"ad_id": "9", "ad_group_id": "g", "status_raw": 2},
            group_id="g",
            expected_status=2,
            reserved_ids=set(),
            gate_for_error=lambda error: human_gate_for_error("新建广告", error, account="110-669-3913"),
            pause=lambda _seconds: None,
            reauth_timeout=0,
        )

    def test_a_saved_confirmation_does_not_submit_without_the_key(self) -> None:
        user_confirmation.confirm()
        logs: list[str] = []
        submits: list[int] = []
        with self.assertRaises(HumanActionRequired) as caught:
            self._place(
                self._reauth_page(),
                lambda: {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}},
                logs,
                submits,
            )
        self.assertEqual(submits, [1])
        self.assertIn("验证页", str(caught.exception))
        self.assertIn("广告没有建", str(caught.exception))
        self.assertNotIn("再改一次", str(caught.exception))
        self.assertTrue(any("摸密钥" in line for line in logs))

    def test_the_confirm_button_during_the_wait_submits_once(self) -> None:
        page = self._reauth_page()
        logs: list[str] = []
        submits: list[int] = []

        def pause(_seconds: float) -> None:
            user_confirmation.confirm()

        def submit() -> dict:
            submits.append(1)
            return {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}}

        with self.assertRaises(HumanActionRequired) as caught:
            place_ad_letting_user_unlock(
                page,
                account="110-669-3913",
                log=logs.append,
                read_ads=lambda: [],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                gate_for_error=lambda error: human_gate_for_error("新建广告", error),
                pause=pause,
                reauth_timeout=0,
            )
        self.assertEqual(submits, [1])
        self.assertIn("广告没有建", str(caught.exception))

    def test_confirming_again_during_a_rejected_retry_does_not_loop(self) -> None:
        logs: list[str] = []
        submits: list[int] = []

        def pause(_seconds: float) -> None:
            user_confirmation.confirm()

        def submit() -> dict:
            submits.append(1)
            return {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}}

        with self.assertRaises(HumanActionRequired) as caught:
            place_ad_letting_user_unlock(
                self._reauth_page(),
                account="110-669-3913",
                log=logs.append,
                read_ads=lambda: [],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                gate_for_error=lambda error: human_gate_for_error("新建广告", error),
                pause=pause,
                reauth_timeout=0,
            )
        self.assertEqual(submits, [1])
        self.assertIn("广告没有建", str(caught.exception))
        self.assertNotIn("再改一次", " ".join(logs) + str(caught.exception))

    def test_a_finished_ad_forgets_a_stale_confirmation(self) -> None:
        user_confirmation.confirm()
        submits: list[int] = []
        ad, submitted = place_ad_letting_user_unlock(
            self._reauth_page(),
            account="110-669-3913",
            log=lambda _text: None,
            read_ads=lambda: [{"ad_id": "9", "ad_group_id": "g", "status_raw": 2}],
            submit=lambda: submits.append(1) or {},
            poll_new_ad=lambda _known: {},
            group_id="g",
            expected_status=2,
            reserved_ids=set(),
            gate_for_error=lambda error: human_gate_for_error("新建广告", error),
            pause=lambda _seconds: None,
            reauth_timeout=0,
        )
        self.assertEqual(ad["ad_id"], "9")
        self.assertFalse(submitted)
        self.assertEqual(submits, [])
        self.assertFalse(user_confirmation.is_set())

    def test_a_real_key_replaces_the_invalid_token(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.extra_headers = {"X-Goog-First-Party-Reauth": "scraped-not-a-real-rapt-token"}

            def command(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
                return {}

            def drain_events(self, prefix: str = "") -> list:
                return [{
                    "params": {"request": {"headers": {
                        "X-Goog-First-Party-Reauth": "real-rapt-from-the-key-window",
                    }}},
                }]

        page = Page()
        seen: list[dict[str, str]] = []

        def submit() -> dict:
            seen.append(dict(page.extra_headers))
            if seen[-1].get("X-Goog-First-Party-Reauth") != "real-rapt-from-the-key-window":
                return {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_INVALID"]}}
            return {}

        ad, _submitted = place_ad_letting_user_unlock(
            page,
            account="110-669-3913",
            log=lambda _text: None,
            read_ads=lambda: [{"ad_id": "9", "ad_group_id": "g", "status_raw": 2}] if len(seen) > 1 else [],
            submit=submit,
            poll_new_ad=lambda _known: {"ad_id": "9", "ad_group_id": "g", "status_raw": 2},
            group_id="g",
            expected_status=2,
            reserved_ids=set(),
            gate_for_error=lambda error: human_gate_for_error("新建广告", error),
            pause=lambda _seconds: None,
        )
        self.assertEqual(ad["ad_id"], "9")
        self.assertEqual(seen[0].get("X-Goog-First-Party-Reauth"), "scraped-not-a-real-rapt-token")
        self.assertEqual(seen[1].get("X-Goog-First-Party-Reauth"), "real-rapt-from-the-key-window")

    def test_the_black_line_does_not_attach_text_from_the_page(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.extra_headers: dict[str, str] = {}

            def command(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
                return {}

            def drain_events(self, prefix: str = "") -> list:
                return []

            def evaluate(self, expression: str, timeout: float = 45) -> str:
                return "reauth-proof-from-page-storage-" + ("x" * 40)

        submits: list[dict[str, str]] = []
        page = Page()

        def submit() -> dict:
            submits.append(dict(page.extra_headers))
            if len(submits) == 1:
                return {"5": {"2": ["AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED"]}}
            return {}

        with self.assertRaises(HumanActionRequired):
            place_ad_letting_user_unlock(
                page,
                account="110-669-3913",
                log=lambda _text: None,
                read_ads=lambda: [],
                submit=submit,
                poll_new_ad=lambda _known: {},
                group_id="g",
                expected_status=2,
                reserved_ids=set(),
                gate_for_error=lambda error: human_gate_for_error("新建广告", error),
                pause=lambda _seconds: None,
                reauth_timeout=0,
            )
        self.assertEqual(submits, [{}])
        self.assertEqual(page.extra_headers, {})


if __name__ == "__main__":
    unittest.main()
