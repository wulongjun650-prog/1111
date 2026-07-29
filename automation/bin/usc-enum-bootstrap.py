#!/usr/bin/env python3
"""构建/增量合并全局去重库: tested.db (SQLite WAL) + tested.txt 备份"""
import argparse
import json
import itertools
import re
import sqlite3
import time
from datetime import date
from pathlib import Path

BASE = Path("/data/automation/results/us-campus.co.kr")
STATE_DIR = BASE / "email_enum_state"
TESTED_TXT = STATE_DIR / "tested.txt"
TESTED_DB = STATE_DIR / "tested.db"
STAMP_FILE = STATE_DIR / ".bootstrap_stamp"
PROGRESS_FILE = BASE / "email_enum_2k_20260727_191720" / "progress.txt"
BATCH = 20000


def legacy_candidates():
    cands = []
    surnames = [
        "kim", "lee", "park", "choi", "jung", "jeong", "kang", "cho", "jo", "yoon", "yun", "jang", "lim", "im",
        "han", "oh", "seo", "shin", "kwon", "hwang", "ahn", "an", "song", "hong", "yu", "yoo", "moon", "mun",
        "son", "bae", "baek", "heo", "hur", "nam", "ha", "kwak", "gu", "ko", "go", "jeon", "jun", "ryu", "noh",
        "roh", "cha", "min", "jin", "sim", "shim", "yang", "won", "byun", "byeon", "chung", "woo", "gil", "gang",
        "gong", "kwang", "ma", "mok", "ban", "bang", "bong", "seok", "suk", "shin", "sok", "um", "uhm", "eom",
        "yeo", "yo", "ok", "oon", "wook", "pyo", "hae", "hyun", "in", "jae", "jin", "chae", "cheon", "chun",
    ]
    given = [
        "min", "jun", "soo", "su", "young", "jin", "hyun", "hyeon", "woo", "ho", "ji", "yeon", "seo", "yoon", "yun",
        "ha", "bin", "gyu", "kyu", "seok", "suk", "won", "chan", "sung", "seong", "dong", "jae", "tae", "sang", "chul",
        "cheol", "hee", "hui", "eun", "in", "ah", "a", "ri", "rae", "kyung", "gyeong", "hye", "mi", "ye", "bo", "na",
        "minjun", "minjae", "seojun", "seojoon", "jiho", "jiwoo", "doyun", "yejun", "siwoo", "hajun", "hajoon",
        "minseo", "haeun", "jimin", "jisoo", "sujin", "hyunwoo", "sangmin", "jaehyun", "donghyun", "seungmin",
        "seunghyun", "taehyun", "jihoon", "junho", "sungmin", "jonghyun", "minsu", "minho", "jieun", "hyunju",
        "eunji", "yujin", "yuna", "jiyeon", "sohee", "juhee", "haneul", "areum", "sora", "nari", "bora", "hara",
        "gunwoo", "yong", "chaeun", "seoah", "doyun", "haru", "siyun", "jiyun", "yejin", "hyerin", "dahye",
        "seungho", "youngjae", "jungmin", "kyungmin", "hyunsoo", "junsuk", "taemin", "kai", "mark", "jay",
        "tom", "sam", "alex", "daniel", "david", "james", "john", "mike", "chris", "kevin", "ryan", "jason",
        "andrew", "brian", "eric", "tony", "paul", "peter", "steve", "jack", "bob", "tom", "max", "leo", "jay",
        "love", "happy", "money", "stock", "king", "sky", "star", "moon", "sun", "rain", "wind", "blue", "red",
        "user", "test", "admin", "vip", "gold", "korea", "seoul", "campus", "hello", "world", "apple", "orange",
    ]
    years = [f"{y:02d}" for y in range(70, 100)] + [f"{y:02d}" for y in range(0, 10)]
    domains_pri = ["naver.com", "daum.net", "hanmail.net", "gmail.com"]
    top = surnames[:25]

    for s, g, yr in itertools.product(surnames, given, years):
        for loc in (s + g + yr, g + s + yr):
            cands.append(f"{loc}@naver.com")
        if s in surnames[:30]:
            cands.append(f"{s}{g}{yr}@daum.net")
            cands.append(f"{g}{s}{yr}@daum.net")

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
                    if s in top[:12]:
                        cands.append(f"{s}{bd}@daum.net")
                        cands.append(f"{s}{bd}@gmail.com")

    for s in surnames:
        for y in years:
            cands.append(f"{s}{y}@naver.com")
            for n in range(0, 100):
                cands.append(f"{s}{y}{n:02d}@naver.com")
        for n in range(0, 10000):
            if n < 1000 or s in top[:15]:
                cands.append(f"{s}{n}@naver.com")

    for s, g in itertools.product(surnames, given):
        for loc in (s + g, g + s, s + "." + g, s + "_" + g, s + g + "1", s + g + "12", s + g + "123"):
            for d in domains_pri[:2]:
                cands.append(f"{loc}@{d}")

    commons = [
        "user", "test", "love", "happy", "money", "stock", "invest", "king", "sky", "star", "moon", "sun",
        "abc", "qwer", "admin", "vip", "gold", "korea", "seoul", "campus", "hello", "world", "apple", "blue",
        "red", "black", "white", "green", "orange", "pink", "purple", "crypto", "coin", "bit", "rich", "queen",
        "student", "teacher", "master", "member", "guest", "demo", "aaaa", "bbbb", "1111", "1234", "12345",
    ]
    for c in commons:
        for n in range(0, 10000):
            cands.append(f"{c}{n}@naver.com")
        for yr in years:
            cands.append(f"{c}{yr}@naver.com")
            for n in range(0, 100):
                cands.append(f"{c}{yr}{n:02d}@naver.com")

    xato = Path("/data/wordlists/seclists/Usernames/xato-net-10-million-usernames.txt")
    if xato.exists():
        with open(xato, encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= 200000:
                    break
                u = line.strip().lower()
                if u and re.match(r"^[a-z0-9._-]{2,24}$", u):
                    cands.append(u + "@naver.com")
                    if i < 50000:
                        cands.append(u + "@daum.net")
                        cands.append(u + "@gmail.com")

    names = Path("/data/wordlists/seclists/Usernames/Names/names.txt")
    if names.exists():
        with open(names, encoding="utf-8", errors="ignore") as f:
            for line in f:
                u = line.strip().lower()
                if u and re.match(r"^[a-z]{2,20}$", u):
                    for d in domains_pri:
                        cands.append(f"{u}@{d}")
                    for yr in years:
                        cands.append(f"{u}{yr}@naver.com")

    seen = set()
    out = []
    for e in cands:
        e = e.lower()
        if e in seen:
            continue
        seen.add(e)
        out.append(e)
    return out


def source_mtimes():
    mtimes = {}
    for label, p in [
        ("tested_txt", TESTED_TXT),
        ("progress", PROGRESS_FILE),
        ("hits_all", STATE_DIR / "hits_all.jsonl"),
    ]:
        if p.exists():
            mtimes[label] = p.stat().st_mtime
    for p in sorted(BASE.glob("email_enum*/hits.jsonl")):
        mtimes[f"hits:{p.name}"] = p.stat().st_mtime
    for p in sorted(STATE_DIR.glob("shard_*/tested_shard.txt")):
        mtimes[f"shard:{p.parent.name}"] = p.stat().st_mtime
    if TESTED_DB.exists():
        mtimes["tested_db_mtime"] = TESTED_DB.stat().st_mtime
    return mtimes


def stamp_is_fresh():
    if not TESTED_DB.exists() or not STAMP_FILE.exists():
        return False
    try:
        stamp = json.loads(STAMP_FILE.read_text(encoding="utf-8"))
        if stamp.get("sources") != source_mtimes():
            return False
        conn = sqlite3.connect(TESTED_DB)
        count = conn.execute("SELECT COUNT(*) FROM tested").fetchone()[0]
        conn.close()
        return count > 0 and count >= stamp.get("count", 0)
    except Exception:
        return False


def collect_new_emails():
    """只收集可能新增的来源，不重复读全库。"""
    emails = set()

    if TESTED_TXT.exists():
        with open(TESTED_TXT, encoding="utf-8", errors="ignore") as f:
            for line in f:
                e = line.strip().lower()
                if e:
                    emails.add(e)

    for p in sorted(BASE.glob("email_enum*/hits.jsonl")):
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                em = (json.loads(line).get("email") or "").lower()
                if em:
                    emails.add(em)
            except Exception:
                pass

    hits_all = STATE_DIR / "hits_all.jsonl"
    if hits_all.exists():
        for line in hits_all.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                em = (json.loads(line).get("email") or "").lower()
                if em:
                    emails.add(em)
            except Exception:
                pass

    if PROGRESS_FILE.exists():
        m = re.search(r"tested=(\d+)", PROGRESS_FILE.read_text(encoding="utf-8"))
        if m:
            n = int(m.group(1))
            print(f"legacy prefix {n}", flush=True)
            for e in legacy_candidates()[:n]:
                emails.add(e)

    for p in sorted(STATE_DIR.glob("shard_*/tested_shard.txt")):
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                e = line.strip().lower()
                if e:
                    emails.add(e)

    return emails


def merge_db(emails, force: bool = False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TESTED_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("CREATE TABLE IF NOT EXISTS tested(email TEXT PRIMARY KEY)")

    if force:
        conn.execute("DELETE FROM tested")
        print("force rebuild: cleared tested table", flush=True)

    before = conn.execute("SELECT COUNT(*) FROM tested").fetchone()[0]
    buf = []
    t0 = time.time()
    for e in emails:
        buf.append((e,))
        if len(buf) >= BATCH:
            conn.executemany("INSERT OR IGNORE INTO tested VALUES(?)", buf)
            conn.commit()
            buf.clear()
    if buf:
        conn.executemany("INSERT OR IGNORE INTO tested VALUES(?)", buf)
        conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM tested").fetchone()[0]
    conn.close()
    added = count - before
    print(
        f"tested.db rows={count} added={added} elapsed={time.time()-t0:.1f}s",
        flush=True,
    )
    return count, added


def write_stamp(count: int):
    STAMP_FILE.write_text(
        json.dumps({"count": count, "sources": source_mtimes(), "ts": time.time()}, indent=2),
        encoding="utf-8",
    )


def export_txt_from_db():
    conn = sqlite3.connect(TESTED_DB)
    rows = [r[0] for r in conn.execute("SELECT email FROM tested ORDER BY email")]
    conn.close()
    with open(TESTED_TXT, "w", encoding="utf-8") as f:
        for e in rows:
            f.write(e + "\n")
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="us-campus tested email bootstrap")
    ap.add_argument("--force", action="store_true", help="full rebuild (DELETE + reinsert)")
    ap.add_argument("--check", action="store_true", help="exit 0 if stamp fresh, 1 if bootstrap needed")
    args = ap.parse_args()

    if args.check:
        raise SystemExit(0 if stamp_is_fresh() else 1)

    if not args.force and stamp_is_fresh():
        conn = sqlite3.connect(TESTED_DB)
        count = conn.execute("SELECT COUNT(*) FROM tested").fetchone()[0]
        conn.close()
        print(f"bootstrap skipped (fresh), rows={count}", flush=True)
        return

    emails = collect_new_emails()
    print(f"collected {len(emails)}", flush=True)
    count, added = merge_db(emails, force=args.force)
    if args.force or added > 1000:
        export_txt_from_db()
    write_stamp(count)
    print(f"TESTED_DB={TESTED_DB} count={count}", flush=True)


if __name__ == "__main__":
    main()
