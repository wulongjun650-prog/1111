#!/usr/bin/env python3
"""从历次枚举结果 + 旧版候选顺序，生成全局 tested.txt 去重库。"""
import json
import itertools
import re
from datetime import date
from pathlib import Path

BASE = Path("/data/automation/results/us-campus.co.kr")
STATE_DIR = BASE / "email_enum_state"
TESTED = STATE_DIR / "tested.txt"
PROGRESS_FILE = BASE / "email_enum_2k_20260727_191720" / "progress.txt"


def legacy_candidates():
    """与 usc-enum-2k.py 相同顺序，用于还原已测前缀。"""
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


def load_prior_tested_count():
    if PROGRESS_FILE.exists():
        m = re.search(r"tested=(\d+)", PROGRESS_FILE.read_text(encoding="utf-8"))
        if m:
            return int(m.group(1))
    return 0


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tested = set()

    if TESTED.exists():
        for line in TESTED.read_text(encoding="utf-8", errors="ignore").splitlines():
            e = line.strip().lower()
            if e:
                tested.add(e)
        print("existing tested", len(tested), flush=True)

    for p in sorted(BASE.glob("email_enum*/hits.jsonl")):
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                em = (json.loads(line).get("email") or "").lower()
                if em:
                    tested.add(em)
            except Exception:
                pass

    prior_n = load_prior_tested_count()
    if prior_n > 0:
        print("rebuilding legacy prefix", prior_n, flush=True)
        legacy = legacy_candidates()
        for e in legacy[:prior_n]:
            tested.add(e)
        print("legacy prefix added", min(prior_n, len(legacy)), flush=True)

    for shard in sorted(STATE_DIR.glob("shard_*/tested_shard.txt")):
        for line in shard.read_text(encoding="utf-8", errors="ignore").splitlines():
            e = line.strip().lower()
            if e:
                tested.add(e)

    with open(TESTED, "w", encoding="utf-8") as f:
        for e in sorted(tested):
            f.write(e + "\n")

    print("TESTED", TESTED, "count", len(tested), flush=True)


if __name__ == "__main__":
    main()
