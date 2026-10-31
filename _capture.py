"""One-off CDP helpers for stage-A protocol reverse engineering.

Not part of the shipped tool -- temporary script, safe to delete after use.
"""
from __future__ import annotations

import base64
import json
import sys
import time

from ggads_tool.protocol import ProtocolRunner

CANDIDATE_SUBSTRINGS = ("/rpc/", "/aw_dva/", "/aw_displayads/", "BatchService")
EXCLUDE_SUBSTRINGS = ("/gen_204", "CollectService", "LogClientEventService",
                      "BotGuardCreationService", "TmlraGlsIEFnYXJ3YWw",
                      "CustomerUserAppDataService")


def click(page, x: float, y: float) -> None:
    page.command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    time.sleep(0.2)
    page.command("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    page.command("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


def screenshot(page, out_path: str) -> None:
    result = page.command("Page.captureScreenshot", {"format": "png"})
    data = base64.b64decode(result["data"])
    with open(out_path, "wb") as f:
        f.write(data)
    print("saved to", out_path)


def listen(page, duration_seconds: float) -> None:
    page.command("Network.enable", {"maxPostDataSize": 65536})
    requests_by_id: dict[str, dict] = {}
    seen_ids: set[str] = set()
    deadline = time.time() + duration_seconds
    while time.time() < deadline:
        time.sleep(1.0)
        events = page.drain_events("Network.")
        for event in events:
            method = event.get("method")
            params = event.get("params") or {}
            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                url = str(req.get("url") or "")
                if req.get("method") != "POST":
                    continue
                if not any(sub in url for sub in CANDIDATE_SUBSTRINGS):
                    continue
                if any(sub in url for sub in EXCLUDE_SUBSTRINGS):
                    continue
                request_id = params.get("requestId")
                requests_by_id[request_id] = {
                    "url": url, "postData": req.get("postData"), "headers": req.get("headers"),
                }
            elif method == "Network.loadingFinished":
                request_id = params.get("requestId")
                if request_id in requests_by_id and request_id not in seen_ids:
                    seen_ids.add(request_id)
                    info = requests_by_id[request_id]
                    try:
                        body = page.command("Network.getResponseBody", {"requestId": request_id})
                    except Exception as exc:  # noqa: BLE001
                        body = {"error": str(exc)}
                    print("=" * 80)
                    print("URL:", info["url"])
                    print("POST DATA:", info.get("postData"))
                    print("RESPONSE:", json.dumps(body, ensure_ascii=False)[:8000])
                    print("=" * 80)


def main() -> None:
    action = sys.argv[1]
    profile_id = sys.argv[2]
    target_cid = sys.argv[3]

    runner = ProtocolRunner(profile_id, print, browser_type="bitbrowser")
    accounts = runner.discover_accounts({target_cid})
    account = accounts.get(target_cid)
    if not account:
        print(f"未找到 CID {target_cid}")
        return
    page = account.page

    if action == "shot":
        out_path = sys.argv[4]
        screenshot(page, out_path)
    elif action == "click":
        x, y = float(sys.argv[4]), float(sys.argv[5])
        out_path = sys.argv[6] if len(sys.argv) > 6 else None
        click(page, x, y)
        time.sleep(1.2)
        if out_path:
            screenshot(page, out_path)
    elif action == "click_listen":
        x, y, duration = float(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6])
        page.command("Network.enable", {"maxPostDataSize": 65536})
        click(page, x, y)
        time.sleep(1.0)
        print("after click, real url:", page.evaluate("window.location.href"))
        listen(page, duration)
    elif action == "listen":
        duration = float(sys.argv[4])
        listen(page, duration)
    elif action == "eval":
        expr = sys.argv[4]
        print(page.evaluate(expr))
    elif action == "nav":
        url = sys.argv[4]
        page.command("Page.navigate", {"url": url})
        time.sleep(3.5)
        print("real current url:", page.evaluate("window.location.href"))
    elif action == "nav_listen":
        url = sys.argv[4]
        duration = float(sys.argv[5])
        page.command("Network.enable", {"maxPostDataSize": 65536})
        page.command("Page.navigate", {"url": url})
        time.sleep(1.0)
        print("real current url:", page.evaluate("window.location.href"))
        listen(page, duration)

    runner.close()


if __name__ == "__main__":
    main()
