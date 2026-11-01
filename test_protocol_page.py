from __future__ import annotations

import unittest

from .protocol import (
    _SELECTACCOUNT_URL,
    ensure_page_url,
    extract_xsrf_token,
    is_google_login_url,
)


class _Page:
    def __init__(self, href: str) -> None:
        self.href = href
        self.commands: list[tuple[str, dict | None]] = []

    def evaluate(self, expression: str, timeout: float = 45.0) -> str:
        if "location.href" in expression:
            return self.href
        if "readyState" in expression:
            return "complete"
        raise AssertionError(expression)

    def command(self, method: str, params: dict | None = None, *, timeout: float = 15.0) -> dict:
        self.commands.append((method, params))
        if method == "Page.navigate":
            self.href = str((params or {}).get("url") or self.href)
        return {}


class _RedirectPage(_Page):
    def command(self, method: str, params: dict | None = None, *, timeout: float = 15.0) -> dict:
        self.commands.append((method, params))
        if method == "Page.navigate":
            self.href = "https://ads.google.com/aw/overview?ocid=1"
        return {}


class PageNavigationTest(unittest.TestCase):
    def test_blank_tab_is_navigated_to_selectaccount(self) -> None:
        page = _Page("about:blank")
        landed = ensure_page_url(page, _SELECTACCOUNT_URL, timeout=3)
        self.assertEqual(landed, _SELECTACCOUNT_URL)
        self.assertEqual(page.commands[0][0], "Page.navigate")
        self.assertEqual(page.commands[0][1]["url"], _SELECTACCOUNT_URL)

    def test_existing_selectaccount_page_is_not_navigated_again(self) -> None:
        page = _Page(_SELECTACCOUNT_URL)
        landed = ensure_page_url(page, _SELECTACCOUNT_URL, timeout=3)
        self.assertEqual(landed, _SELECTACCOUNT_URL)
        self.assertEqual(page.commands, [])

    def test_login_page_is_reported_without_another_navigation(self) -> None:
        page = _Page("https://accounts.google.com/v3/signin/identifier")
        landed = ensure_page_url(page, _SELECTACCOUNT_URL, timeout=3)
        self.assertTrue(is_google_login_url(landed))
        self.assertEqual(page.commands, [])

    def test_redirect_to_ads_overview_is_accepted(self) -> None:
        page = _RedirectPage("about:blank")
        landed = ensure_page_url(page, _SELECTACCOUNT_URL, timeout=3)
        self.assertEqual(landed, "https://ads.google.com/aw/overview?ocid=1")
        self.assertEqual(len(page.commands), 1)


class XsrfTokenTest(unittest.TestCase):
    def test_classic_token_and_wiz_global_data(self) -> None:
        classic = "AA" + ("b" * 20) + ":1710000000000"
        self.assertEqual(extract_xsrf_token(f'<script>"{classic}"</script>'), classic)
        wiz = "AFoagUexampletokenvalue:1710000000000"
        self.assertEqual(extract_xsrf_token(f'SNlM0e":"{wiz}"'), wiz)

    def test_blank_page_has_no_token(self) -> None:
        self.assertEqual(extract_xsrf_token("<html><body></body></html>"), "")


if __name__ == "__main__":
    unittest.main()
