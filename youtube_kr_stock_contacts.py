#!/usr/bin/env python3
"""
韩国股票/理财 YouTube 频道 — 公开商务联系信息采集

用途：广告合作 outreach（仅采集频道主动公开的联系方式）
能力：
  1) YouTube Data API 搜索韩语股票/理财频道
  2) 从频道简介提取公开邮箱
  3) 提取官网 / Linktree / SNS 链接（可再人工找商务邮箱）
  4) 可选：抓 About 页源码中明文出现的邮箱（不破解「查看邮箱」验证码）

使用前：
  1. 去 Google Cloud 开通 YouTube Data API v3，创建 API Key
  2. export YOUTUBE_API_KEY='你的key'
  3. pip install requests
  4. python3 youtube_kr_stock_contacts.py

输出：kr_stock_youtube_contacts.csv
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any

API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
OUT_CSV = os.environ.get("OUT_CSV", "kr_stock_youtube_contacts.csv")
OUT_JSON = os.environ.get("OUT_JSON", "kr_stock_youtube_contacts.json")

# 搜索词：可按需增删
SEARCH_QUERIES = [
    "주식",
    "주식투자",
    "주식강의",
    "주식차트",
    "재테크",
    "투자",
    "미국주식",
    "코스피",
    "ETF 투자",
    "배당주",
    "주식단타",
    "가치투자",
]

EMAIL_RE = re.compile(
    r"(?i)\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b"
)
# 常见商务相关外链
LINK_HINTS = (
    "linktr.ee",
    "lit.link",
    "litt.ly",
    "bio.link",
    "carrd.co",
    "instagram.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "threads.net",
    "t.me",
    "kakao",
    "naver.com",
    "blog.naver",
    "cafe.naver",
    "open.kakao",
)


def api_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    if not API_KEY:
        raise SystemExit(
            "缺少 YOUTUBE_API_KEY。请先：export YOUTUBE_API_KEY='你的Google API Key'"
        )
    params = dict(params)
    params["key"] = API_KEY
    url = "https://www.googleapis.com/youtube/v3/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def uniq(seq: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in seq:
        x = (x or "").strip()
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def extract_emails(text: str) -> list[str]:
    if not text:
        return []
    # 过滤明显非联系邮箱
    junk = ("example.com", "email.com", "sentry.io", "wixpress.com", "cloudflare")
    emails = []
    for e in EMAIL_RE.findall(text):
        el = e.lower()
        if any(j in el for j in junk):
            continue
        emails.append(el)
    return uniq(emails)


def extract_links_from_description(text: str) -> list[str]:
    if not text:
        return []
    urls = re.findall(r"https?://[^\s\]\)\"'<>]+", text)
    cleaned = []
    for u in urls:
        u = u.rstrip(".,);]")
        cleaned.append(u)
    return uniq(cleaned)


def search_channels(query: str, max_pages: int = 2) -> list[str]:
    """返回 channelId 列表。regionCode=KR + relevanceLanguage=ko 偏向韩国内容。"""
    ids: list[str] = []
    token = None
    for _ in range(max_pages):
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "channel",
            "q": query,
            "maxResults": 50,
            "regionCode": "KR",
            "relevanceLanguage": "ko",
            "order": "relevance",
        }
        if token:
            params["pageToken"] = token
        data = api_get("search", params)
        for item in data.get("items", []):
            cid = item.get("snippet", {}).get("channelId") or item.get("id", {}).get("channelId")
            if cid:
                ids.append(cid)
        token = data.get("nextPageToken")
        if not token:
            break
        time.sleep(0.2)
    return uniq(ids)


def fetch_channel_details(channel_ids: list[str]) -> list[dict[str, Any]]:
    rows = []
    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i : i + 50]
        data = api_get(
            "channels",
            {
                "part": "snippet,statistics,brandingSettings,contentDetails",
                "id": ",".join(batch),
            },
        )
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            st = item.get("statistics", {})
            brand = item.get("brandingSettings", {}).get("channel", {})
            desc = sn.get("description") or ""
            title = sn.get("title") or ""
            custom = sn.get("customUrl") or ""
            country = sn.get("country") or brand.get("country") or ""
            emails = extract_emails(desc)
            links = extract_links_from_description(desc)
            useful_links = [
                u
                for u in links
                if any(h in u.lower() for h in LINK_HINTS) or not u.lower().startswith("https://www.youtube.com")
            ]
            rows.append(
                {
                    "channel_id": item.get("id"),
                    "title": title,
                    "handle": custom,
                    "url": f"https://www.youtube.com/channel/{item.get('id')}",
                    "about_url": f"https://www.youtube.com/channel/{item.get('id')}/about",
                    "country": country,
                    "subscribers": st.get("subscriberCount"),
                    "videos": st.get("videoCount"),
                    "views": st.get("viewCount"),
                    "emails_from_description": ";".join(emails),
                    "links_from_description": " | ".join(useful_links[:15]),
                    "description_snippet": desc[:300].replace("\n", " "),
                }
            )
        time.sleep(0.2)
    return rows


def enrich_about_page_emails(rows: list[dict[str, Any]], limit: int = 80) -> None:
    """
    尝试从 About 页 HTML 里抓明文邮箱。
    注意：YouTube「查看邮箱地址」按钮受验证码保护，脚本无法也不应绕过。
    """
    for i, row in enumerate(rows[:limit]):
        about = row.get("about_url")
        try:
            html = http_get(about)
        except Exception as e:
            row["about_page_emails"] = ""
            row["about_fetch_error"] = str(e)[:120]
            continue
        emails = extract_emails(html)
        # 也抓 about 页常见外链编码片段
        extra_links = re.findall(r"https?://[^\s\"'\\]+", html)
        extra_links = [
            urllib.parse.unquote(u.replace("\\u0026", "&").replace("\\/", "/"))
            for u in extra_links
        ]
        useful = [
            u
            for u in uniq(extra_links)
            if any(h in u.lower() for h in LINK_HINTS)
            and "youtube.com" not in u.lower()
            and "google.com" not in u.lower()
        ]
        row["about_page_emails"] = ";".join(emails)
        if useful and not row.get("links_from_description"):
            row["links_from_description"] = " | ".join(useful[:15])
        elif useful:
            merged = uniq((row.get("links_from_description") or "").split(" | ") + useful)
            row["links_from_description"] = " | ".join([x for x in merged if x][:20])
        row["about_fetch_error"] = ""
        print(f"[{i+1}/{min(limit, len(rows))}] {row.get('title')} emails={emails}")
        time.sleep(0.6)


def write_csv(rows: list[dict[str, Any]], path: str) -> None:
    if not rows:
        print("无数据可写")
        return
    fields = [
        "title",
        "handle",
        "subscribers",
        "videos",
        "country",
        "emails_from_description",
        "about_page_emails",
        "links_from_description",
        "url",
        "about_url",
        "channel_id",
        "views",
        "description_snippet",
        "about_fetch_error",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    print("== 搜索韩国股票/理财相关频道 ==")
    all_ids: list[str] = []
    for q in SEARCH_QUERIES:
        print(f"search: {q}")
        ids = search_channels(q, max_pages=2)
        print(f"  got {len(ids)}")
        all_ids.extend(ids)
        time.sleep(0.3)
    all_ids = uniq(all_ids)
    print(f"去重后频道数: {len(all_ids)}")

    print("== 拉取频道详情 ==")
    rows = fetch_channel_details(all_ids)
    # 优先保留看起来相关的（标题/简介含股票理财词）
    keywords = ("주식", "투자", "재테크", "차트", "코스피", "ETF", "배당", "증권", "경제", "금융")
    ranked = []
    for r in rows:
        text = (r.get("title") or "") + " " + (r.get("description_snippet") or "")
        score = sum(1 for k in keywords if k.lower() in text.lower())
        r["_score"] = score
        ranked.append(r)
    ranked.sort(key=lambda x: (x.get("_score", 0), int(x.get("subscribers") or 0)), reverse=True)

    print("== 补充抓取 About 页明文邮箱（前 100 个） ==")
    enrich_about_page_emails(ranked, limit=100)

    # 有公开邮箱的排前面
    ranked.sort(
        key=lambda x: (
            1 if (x.get("emails_from_description") or x.get("about_page_emails")) else 0,
            int(x.get("subscribers") or 0),
        ),
        reverse=True,
    )

    write_csv(ranked, OUT_CSV)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(ranked, f, ensure_ascii=False, indent=2)

    with_email = [
        r
        for r in ranked
        if r.get("emails_from_description") or r.get("about_page_emails")
    ]
    print("\n完成")
    print(f"频道总数: {len(ranked)}")
    print(f"已找到公开邮箱: {len(with_email)}")
    print(f"CSV: {OUT_CSV}")
    print(f"JSON: {OUT_JSON}")
    print(
        "\n说明：很多频道商务邮箱藏在「查看邮箱」按钮后（验证码），"
        "脚本不会绕过。对这些频道请走 links_from_description 里的官网/Linktree，"
        "或在 YouTube 登录后手动点 About 页查看。"
    )


if __name__ == "__main__":
    main()
