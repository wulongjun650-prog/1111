#!/usr/bin/env python3
"""Collect public emails from Korea stock/securities/investing YouTube channels only."""

from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any

OUT_CSV = "/workspace/kr_stock_youtube_public_emails.csv"
OUT_JSON = "/workspace/kr_stock_youtube_public_emails.json"

# Strict topic keywords (stock / securities / investing)
INCLUDE_KW = [
    "주식",
    "증권",
    "투자",
    "재테크",
    "코스피",
    "코스닥",
    "차트",
    "배당",
    "etf",
    "미국주식",
    "해외주식",
    "가치투자",
    "단타",
    "스윙",
    "펀드",
    "채권",
    "증시",
    "매매",
    "포트폴리오",
    "금융",
    "경제",
    "stock",
    "invest",
    "securit",
    "trading",
    "kospi",
    "kosdaq",
]
# Soft exclude: mainly crypto/real-estate-only if no stock keyword
EXCLUDE_ONLY = ["코인만", "비트코인만"]

EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b")
JUNK_EMAIL = (
    "example.com",
    "email.com",
    "sentry.io",
    "wixpress.com",
    "cloudflare",
    "schema.org",
    "googleapis.com",
    "gstatic.com",
    "youtube.com",
    "google.com",
    "noreply",
    "no-reply",
)

# Seed channels known to be KR finance/stock (handles or channel URLs/ids)
SEED_CHANNELS = [
    "@samproTV",
    "@shinsaImdang",
    "@kimwriter",
    "@stockdante",
    "@miraeassetsmartmoney",
    "@koreaneconomyTV",
    "@mtnmoneytoday",
    "@jeoningoo",
    "@pilottalk",
    "@bueeingnam",
    "@winnersTV",
    "@parkhodoo",
    "@dalantinvestment",
    "@sosumonkey",
    "@bakgomhee",
    "@shukaWorld",
    "@소수몽키",
]


def http_get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            "Accept": "text/html,application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def uniq(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        x = (x or "").strip()
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def extract_emails(text: str) -> list[str]:
    out = []
    for e in EMAIL_RE.findall(text or ""):
        el = e.lower()
        if any(j in el for j in JUNK_EMAIL):
            continue
        out.append(el)
    return uniq(out)


def is_stock_related(title: str, desc: str) -> bool:
    text = f"{title} {desc}".lower()
    return any(k.lower() in text for k in INCLUDE_KW)


def resolve_channel(query: str) -> dict[str, Any] | None:
    """Resolve @handle / URL / UC.. id via YouTube page + ytInitialData/about-ish HTML."""
    q = query.strip()
    if q.startswith("UC") and len(q) >= 22:
        url = f"https://www.youtube.com/channel/{q}/about"
    elif "youtube.com/" in q:
        if "/about" not in q:
            q = q.rstrip("/") + "/about"
        url = q
    elif q.startswith("@"):
        url = f"https://www.youtube.com/{q}/about"
    else:
        url = f"https://www.youtube.com/@{q}/about"

    try:
        html = http_get(url)
    except Exception as e:
        return {"query": query, "error": str(e)[:160]}

    # channel id
    m = re.search(r'"channelId":"(UC[\w-]{22})"', html)
    if not m:
        m = re.search(r"https://www\.youtube\.com/channel/(UC[\w-]{22})", html)
    channel_id = m.group(1) if m else ""

    # title
    title = ""
    mt = re.search(r'"channelMetadataRenderer":\{"title":"([^"]+)"', html)
    if mt:
        title = mt.group(1).encode("utf-8").decode("unicode_escape", "ignore")
    if not title:
        mt = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        if mt:
            title = re.sub(r"\s*-?\s*YouTube\s*$", "", mt.group(1)).strip()

    # description from meta / JSON
    desc = ""
    md = re.search(r'"description":\{"simpleText":"(.*?)"\}', html)
    if md:
        desc = md.group(1).encode("utf-8").decode("unicode_escape", "ignore")
    if not desc:
        md = re.search(r'<meta name="description" content="([^"]*)"', html)
        if md:
            desc = md.group(1)

    # subscriber rough
    subs = ""
    ms = re.search(r'"subscriberCountText":\{"simpleText":"([^"]+)"', html)
    if ms:
        subs = ms.group(1)

    emails = extract_emails(html) + extract_emails(desc)
    emails = uniq(emails)

    # external links
    links = []
    for u in re.findall(r"https?://[^\s\"'\\<>]+", html):
        u = urllib.parse.unquote(u.replace("\\u0026", "&").replace("\\/", "/")).rstrip(".,);]")
        ul = u.lower()
        if any(
            h in ul
            for h in (
                "linktr.ee",
                "lit.link",
                "instagram.com",
                "twitter.com",
                "x.com",
                "facebook.com",
                "naver.com",
                "kakao",
                "t.me",
                "blog.",
            )
        ) and "youtube.com" not in ul and "google." not in ul:
            links.append(u)
    links = uniq(links)[:12]

    business_email_button = ("이메일 주소 보기" in html) or ("View email address" in html) or ("businessEmail" in html)

    return {
        "query": query,
        "channel_id": channel_id,
        "title": title,
        "subscribers_text": subs,
        "url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else url.replace("/about", ""),
        "about_url": url if channel_id else url,
        "emails_public": ";".join(emails),
        "has_business_email_button": business_email_button,
        "links": " | ".join(links),
        "description_snippet": (desc or "")[:240].replace("\n", " "),
        "stock_related": is_stock_related(title, desc),
        "error": "",
    }


def search_playboard_seed() -> list[str]:
    """Use Playboard search pages for stock-related channel names/links if accessible."""
    seeds = []
    queries = ["주식", "주식투자", "재테크", "증권", "미국주식"]
    for q in queries:
        url = "https://playboard.co/en/search?q=" + urllib.parse.quote(q)
        try:
            html = http_get(url, timeout=20)
        except Exception:
            continue
        # channel links
        for m in re.findall(r"/youtube/channel/(UC[\w-]{22})", html):
            seeds.append(m)
        for m in re.findall(r"youtube\.com/(?:channel/(UC[\w-]{22})|@([\w.\-]+))", html):
            if m[0]:
                seeds.append(m[0])
            if m[1]:
                seeds.append("@" + m[1])
        time.sleep(0.5)
    return uniq(seeds)


def search_yt_suggest_pages() -> list[str]:
    """Fallback: Google-ish via duckduckgo html for site:youtube.com Korean stock channels."""
    seeds = []
    qs = [
        'site:youtube.com 주식 유튜브',
        'site:youtube.com 주식투자 채널',
        'site:youtube.com 재테크 주식',
        'site:youtube.com "미국주식"',
    ]
    for q in qs:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q)
        try:
            html = http_get(url, timeout=20)
        except Exception:
            continue
        for m in re.findall(r"https?://www\.youtube\.com/(?:@[\w.\-]+|channel/UC[\w-]{22})", html):
            seeds.append(m)
        time.sleep(0.8)
    return uniq(seeds)


def main() -> None:
    seeds = list(SEED_CHANNELS)
    print("collecting more channel seeds...")
    try:
        seeds += search_playboard_seed()
    except Exception as e:
        print("playboard seed fail", e)
    try:
        seeds += search_yt_suggest_pages()
    except Exception as e:
        print("ddg seed fail", e)
    seeds = uniq(seeds)
    print(f"seeds: {len(seeds)}")

    rows = []
    for i, s in enumerate(seeds, 1):
        print(f"[{i}/{len(seeds)}] {s}")
        row = resolve_channel(s)
        if not row:
            continue
        rows.append(row)
        time.sleep(0.7)

    # keep only stock-related OR seed known finance names with empty desc still from seed list
    filtered = []
    for r in rows:
        if r.get("error") and not r.get("channel_id"):
            continue
        title = r.get("title") or ""
        desc = r.get("description_snippet") or ""
        if r.get("stock_related") or is_stock_related(title, desc) or r.get("query") in SEED_CHANNELS:
            # drop if clearly unrelated and no include kw
            if not is_stock_related(title, desc) and r.get("query") not in SEED_CHANNELS:
                continue
            filtered.append(r)

    # Dedupe by channel_id/title
    dedup = {}
    for r in filtered:
        key = r.get("channel_id") or r.get("title") or r.get("query")
        prev = dedup.get(key)
        if not prev or (r.get("emails_public") and not prev.get("emails_public")):
            dedup[key] = r
    final_rows = list(dedup.values())
    final_rows.sort(key=lambda x: (1 if x.get("emails_public") else 0, x.get("title") or ""), reverse=True)

    with_email = [r for r in final_rows if r.get("emails_public")]

    fields = [
        "title",
        "emails_public",
        "has_business_email_button",
        "subscribers_text",
        "url",
        "about_url",
        "links",
        "channel_id",
        "query",
        "description_snippet",
        "error",
    ]
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in final_rows:
            w.writerow(r)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(final_rows, f, ensure_ascii=False, indent=2)

    print("\nDONE")
    print("channels_kept", len(final_rows))
    print("with_public_email", len(with_email))
    print("CSV", OUT_CSV)
    for r in with_email[:30]:
        print("-", r.get("title"), r.get("emails_public"), r.get("url"))


if __name__ == "__main__":
    main()
