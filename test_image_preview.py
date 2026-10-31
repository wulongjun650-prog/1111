from __future__ import annotations

import unittest

from .image_upload import (
    asset_folder_list_request,
    download_first_preview,
    image_urls_by_asset,
    missing_image_message,
    parse_asset_folder_readback,
    preview_urls_from_asset_row,
    resolve_image_preview_urls,
)
from .portable_copy_workflow import (
    PortableCampaignWorkflow,
    referenced_image_filenames,
)
from .protocol import AccountPage
from .snapshot_copy import SnapshotExporter


ACCOUNT = "1111111111"
MANAGER = "2222222222"
DUPLICATE = "430839323380"
CATALOG = "430933007277"
EMBEDDED = "430933007583"
ORIGINAL = "430933007200"
PREVIEW = "https://tpc.googlesyndication.com/simgad/{}"


def _row(asset_id: str, preview: str = "", **extra: object) -> dict:
    row = {"1": ACCOUNT, "2": asset_id, "5": f"{asset_id}.png", "8": 2, "9": 1}
    if preview:
        row["24"] = preview
    row.update(extra)
    return row


def _asset_filter(request: dict) -> str:
    for item in request["2"]["2"]:
        if item.get("1") == "asset_id":
            return str(item["4"][0]["3"])
    return ""


class Page:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.url = (
            "https://ads.google.com/aw/campaigns"
            f"?ocid={ACCOUNT}&uscid={MANAGER}"
        )

    def rpc(self, path, request, timeout=60):
        self.calls.append((path, request, timeout))
        return self.handler(path, request)


def _display_ad() -> dict:
    def image(asset_id: str, url: str = "") -> dict:
        asset = {"1": asset_id, "2": f"{asset_id}.png"}
        wrapper = {"1": asset}
        if url:
            wrapper["5"] = {"1": url}
        return wrapper

    return {
        "13": {
            "100": {
                "41": {
                    "1": [image(DUPLICATE), image(CATALOG)],
                    "2": [image(EMBEDDED, PREVIEW.format("embedded"))],
                    "5": [{"1": {"1": "111111111111", "3": {"1": "标题"}}}],
                    "20": [],
                }
            }
        }
    }


class PreviewParseTest(unittest.TestCase):
    def test_integer_flag_is_not_a_url_and_nested_url_is_kept(self) -> None:
        urls = preview_urls_from_asset_row({
            "17": 1,
            "30": {"2": PREVIEW.format("nested")},
        })
        self.assertEqual(urls, [PREVIEW.format("nested")])

    def test_explicit_preview_comes_before_a_nested_url(self) -> None:
        urls = preview_urls_from_asset_row({
            "24": PREVIEW.format("full"),
            "17": PREVIEW.format("thumb"),
        })
        self.assertEqual(urls[0], PREVIEW.format("full"))

    def test_foreign_account_still_raises_when_strict(self) -> None:
        with self.assertRaises(PermissionError):
            parse_asset_folder_readback(
                {"1": [{"1": "999", "2": DUPLICATE, "24": PREVIEW.format("x")}]},
                account_id=ACCOUNT,
            )

    def test_image_list_request_still_asks_for_image_assets(self) -> None:
        request = asset_folder_list_request(ACCOUNT, MANAGER, asset_id=DUPLICATE)
        self.assertEqual(request["3"][0], {"1": "enabledAssetTypes", "2": "(2)"})
        self.assertEqual(request["2"]["2"][0]["1"], "asset_id")


class ResolvePreviewTest(unittest.TestCase):
    def test_duplicate_rows_manager_miss_catalog_and_embedded_url(self) -> None:
        def handler(path, request):
            asset_id = _asset_filter(request)
            manager = request["1"]["3"]["3"]
            if asset_id == DUPLICATE and manager == MANAGER:
                return {"1": [
                    _row(DUPLICATE, PREVIEW.format("dup")),
                    _row(DUPLICATE, PREVIEW.format("dup")),
                ]}
            if asset_id:
                return {"1": []}
            return {"1": [_row(CATALOG, PREVIEW.format("catalog"))]}

        page = Page(handler)
        resolved = resolve_image_preview_urls(
            page, ACCOUNT, MANAGER, [DUPLICATE, CATALOG, EMBEDDED], [_display_ad()],
        )
        self.assertEqual(resolved[DUPLICATE], [PREVIEW.format("dup")])
        self.assertEqual(resolved[CATALOG], [PREVIEW.format("catalog")])
        self.assertEqual(resolved[EMBEDDED], [PREVIEW.format("embedded")])

    def test_child_scope_is_used_when_the_manager_scope_is_empty(self) -> None:
        def handler(path, request):
            if request["1"]["3"]["3"] == ACCOUNT and _asset_filter(request) == DUPLICATE:
                return {"1": [_row(DUPLICATE, PREVIEW.format("child"))]}
            return {"1": []}

        resolved = resolve_image_preview_urls(
            Page(handler), ACCOUNT, MANAGER, [DUPLICATE], [],
        )
        self.assertEqual(resolved[DUPLICATE], [PREVIEW.format("child")])

    def test_crop_uses_the_original_asset_preview(self) -> None:
        def handler(path, request):
            asset_id = _asset_filter(request)
            if asset_id == DUPLICATE:
                return {"1": [_row(DUPLICATE, "", **{"40": ORIGINAL})]}
            if asset_id == ORIGINAL:
                return {"1": [_row(ORIGINAL, PREVIEW.format("original"))]}
            return {"1": []}

        resolved = resolve_image_preview_urls(
            Page(handler), ACCOUNT, ACCOUNT, [DUPLICATE], [],
        )
        self.assertEqual(resolved[DUPLICATE], [PREVIEW.format("original")])

    def test_missing_asset_is_omitted(self) -> None:
        resolved = resolve_image_preview_urls(
            Page(lambda path, request: {"1": []}),
            ACCOUNT, ACCOUNT, [DUPLICATE], [], max_catalog_pages=1,
        )
        self.assertEqual(resolved, {})
        self.assertIn(DUPLICATE, missing_image_message([DUPLICATE], "素材库没有这些图片的预览地址，广告数据里也没有"))

    def test_urls_are_not_taken_from_a_different_image(self) -> None:
        ad = _display_ad()
        found = image_urls_by_asset(ad, [DUPLICATE, EMBEDDED])
        self.assertEqual(found[DUPLICATE], [])
        self.assertEqual(found[EMBEDDED], [PREVIEW.format("embedded")])

    def test_headline_asset_id_is_not_an_image(self) -> None:
        names = referenced_image_filenames([_display_ad()])
        self.assertEqual(set(names), {DUPLICATE, CATALOG, EMBEDDED})

    def test_second_preview_is_used_when_the_first_download_fails(self) -> None:
        def fetch(page, url):
            if url.endswith("bad"):
                raise RuntimeError("下载失败")
            return b"img", "image/png", url

        content, mime, final = download_first_preview(
            None,
            {
                "source_asset_id": DUPLICATE,
                "preview_urls": [PREVIEW.format("bad"), PREVIEW.format("good")],
            },
            downloader=fetch,
        )
        self.assertEqual(content, b"img")
        self.assertEqual(final, PREVIEW.format("good"))
        self.assertEqual(mime, "image/png")


class ExporterImageRecordTest(unittest.TestCase):
    def test_snapshot_exporter_keeps_all_three_sources(self) -> None:
        def handler(path, request):
            asset_id = _asset_filter(request)
            manager = request["1"]["3"]["3"]
            if asset_id == DUPLICATE and manager == MANAGER:
                return {"1": [
                    _row(DUPLICATE, PREVIEW.format("dup")),
                    _row(DUPLICATE),
                ]}
            if asset_id:
                return {"1": []}
            return {"1": [_row(CATALOG, PREVIEW.format("catalog"))]}

        page = Page(handler)
        source = AccountPage(cid="1234567890", ocid=ACCOUNT, page=page)
        exporter = SnapshotExporter(source, lambda text: None)
        records = exporter._image_records([_display_ad()])
        by_id = {item["source_asset_id"]: item["preview_url"] for item in records}
        self.assertEqual(by_id[DUPLICATE], PREVIEW.format("dup"))
        self.assertEqual(by_id[CATALOG], PREVIEW.format("catalog"))
        self.assertEqual(by_id[EMBEDDED], PREVIEW.format("embedded"))

    def test_missing_image_still_fails_before_download(self) -> None:
        ads = [_display_ad()]
        with self.assertRaises(RuntimeError) as caught:
            PortableCampaignWorkflow._require_image_previews(ads, [])
        message = str(caught.exception)
        self.assertIn("来源图片素材无法完整下载", message)
        self.assertIn(DUPLICATE, message)
        self.assertIn(CATALOG, message)
        self.assertIn(EMBEDDED, message)


if __name__ == "__main__":
    unittest.main()
