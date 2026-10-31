from __future__ import annotations

import csv
import html
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import normalize_websites


WEBSITE_CATEGORIES = (
    "游戏", "博彩", "成人", "视频", "新闻", "社交", "购物", "体育",
    "金融", "教育", "科技", "音乐", "下载", "生活服务", "其他",
)

_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "成人": ("porn", "xxx", "adult", "sex", "hentai", "成人视频", "情色", "성인", "야동"),
    "博彩": ("casino", "betting", "sportsbook", "poker", "slot", "gambling", "博彩", "投注", "赌场", "토토", "카지노", "베팅"),
    "游戏": ("game", "gaming", "gameplay", "gamer", "esport", "steam", "roblox", "minecraft", "游戏", "手游", "网游", "电竞", "遊戲", "ゲーム", "게임", "모바일게임", "온라인게임"),
    "视频": ("video", "streaming", "watch online", "movie", "drama", "television", "影视", "电影", "电视剧", "视频", "동영상", "영화", "드라마"),
    "新闻": ("news", "daily", "journal", "press", "breaking", "新闻", "资讯", "日报", "뉴스", "신문", "일보"),
    "社交": ("social", "community", "forum", "chat", "dating", "社区", "论坛", "交友", "커뮤니티", "채팅"),
    "购物": ("shop", "shopping", "store", "marketplace", "coupon", "购物", "商城", "优惠", "쇼핑", "스토어", "쿠팡"),
    "体育": ("sport", "football", "soccer", "baseball", "basketball", "体育", "足球", "篮球", "스포츠", "축구", "야구"),
    "金融": ("finance", "bank", "stock", "crypto", "loan", "insurance", "投资", "股票", "贷款", "保险", "금융", "주식", "대출"),
    "教育": ("education", "school", "university", "course", "academy", "学习", "教育", "课程", "学校", "교육", "학원", "대학교"),
    "科技": ("technology", "software", "developer", "computer", "mobile", "科技", "软件", "数码", "기술", "소프트웨어", "IT뉴스"),
    "音乐": (
        "music", "song", "lyrics", "radio", "audio", "artist", "album", "playlist",
        "spotify", "soundcloud", "音乐", "歌曲", "歌手", "专辑", "뮤직", "음악", "노래",
        "가사", "앨범", "아티스트", "플레이리스트",
    ),
    "下载": ("download", "torrent", "apk", "crack", "下载", "磁力", "다운로드", "토렌트"),
    "生活服务": ("weather", "travel", "food", "recipe", "health", "real estate", "天气", "旅游", "美食", "房产", "날씨", "여행", "맛집", "부동산"),
}

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META_RE = re.compile(
    r"<meta[^>]+(?:name|property)=[\"'](?:description|keywords|og:title|og:description)[\"'][^>]+content=[\"'](.*?)[\"'][^>]*>",
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def classify_website(domain: str, *, title: str = "", page_text: str = "") -> dict[str, Any]:
    """Classify a website deterministically from its domain and public page metadata."""
    source = html.unescape(f"{domain} {domain} {domain} {title} {title} {page_text}").casefold()
    scores: dict[str, int] = {}
    matches: dict[str, list[str]] = {}
    for category, terms in _CATEGORY_TERMS.items():
        found = []
        score = 0
        for term in terms:
            count = source.count(term.casefold())
            if count:
                found.append(term)
                score += min(count, 4)
        if score:
            scores[category] = score
            matches[category] = found
    if not scores:
        return {"category": "其他", "category_confidence": 0, "category_reason": "未命中分类关键词"}
    category, score = max(scores.items(), key=lambda item: (item[1], -WEBSITE_CATEGORIES.index(item[0])))
    confidence = min(99, 45 + score * 9)
    reason = "、".join(matches[category][:6])
    return {
        "category": category,
        "category_confidence": confidence,
        "category_reason": f"命中：{reason}",
    }


def _validate_public_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持 HTTP/HTTPS 网站")
    host = parsed.hostname.rstrip(".")
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {row[4][0] for row in socket.getaddrinfo(host, parsed.port or default_port)}
    except socket.gaierror as exc:
        raise OSError(f"DNS 解析失败：{exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        # Clash/sing-box and similar TUN clients commonly use 198.18.0.0/15 as
        # a synthetic public-DNS range. It isn't a routable LAN destination and
        # must remain usable for the user's outbound proxy. All real private,
        # loopback, link-local and metadata-style destinations stay blocked.
        proxy_fake_ip = isinstance(ip, ipaddress.IPv4Address) and ip in _PROXY_FAKE_IP_NETWORK
        if not ip.is_global and not proxy_fake_ip:
            raise PermissionError(f"拒绝访问非公网地址：{address}")
    return url


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return super().redirect_request(req, fp, code, msg, headers, _validate_public_url(newurl))


def _decode_html(payload: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w.-]+)", content_type, re.I)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8", "euc-kr", "gb18030", "shift_jis", "latin-1"])
    for encoding in encodings:
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _fetch_website(domain: str, timeout: float) -> dict[str, Any]:
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _PublicRedirectHandler(),
    )
    last_error = ""
    for scheme in ("https", "http"):
        url = _validate_public_url(f"{scheme}://{domain}/")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GGAdsWebsiteCheck/1.0",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "zh-CN,zh;q=0.8,en;q=0.6",
                "Range": "bytes=0-524287",
            },
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                status = int(response.status or 0)
                body = response.read(524288)
                content_type = str(response.headers.get("Content-Type") or "")
                document = _decode_html(body, content_type)
                title_match = _TITLE_RE.search(document)
                title = _TAG_RE.sub(" ", title_match.group(1)).strip()[:300] if title_match else ""
                metadata = " ".join(_TAG_RE.sub(" ", value) for value in _META_RE.findall(document))
                return {
                    "reachable": 200 <= status < 400,
                    "access_status": "可访问" if 200 <= status < 400 else f"HTTP {status}",
                    "http_status": status,
                    "final_url": response.geturl(),
                    "title": html.unescape(re.sub(r"\s+", " ", title)),
                    "page_text": html.unescape(re.sub(r"\s+", " ", metadata))[:2000],
                    "access_error": "",
                }
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code not in {400, 403, 405, 406, 429}:
                continue
            return {
                "reachable": False, "access_status": last_error, "http_status": exc.code,
                "final_url": exc.geturl(), "title": "", "page_text": "", "access_error": str(exc),
            }
        except Exception as exc:
            last_error = str(exc)
    return {
        "reachable": False, "access_status": "不可访问", "http_status": 0,
        "final_url": "", "title": "", "page_text": "", "access_error": last_error[:500],
    }


def analyze_website(domain: str, *, timeout: float = 8.0) -> dict[str, Any]:
    normalized = normalize_websites([domain])[0]
    try:
        access = _fetch_website(normalized, timeout)
    except Exception as exc:
        access = {
            "reachable": False, "access_status": "检测失败", "http_status": 0,
            "final_url": "", "title": "", "page_text": "", "access_error": str(exc)[:500],
        }
    classification = classify_website(
        normalized, title=str(access.get("title") or ""), page_text=str(access.get("page_text") or ""),
    )
    access.pop("page_text", None)
    return {"domain": normalized, **access, **classification}


def analyze_websites(
    domains: Iterable[str], *, timeout: float = 8.0, workers: int = 10,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_websites(list(domains))
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 16))) as executor:
        futures = {executor.submit(analyze_website, domain, timeout=timeout): domain for domain in normalized}
        total = len(futures)
        for done, future in enumerate(as_completed(futures), 1):
            domain = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # defensive: one site must not abort the batch
                row = {
                    "domain": domain, "reachable": False, "access_status": "检测失败",
                    "http_status": 0, "final_url": "", "title": "", "access_error": str(exc),
                    **classify_website(domain),
                }
            results[domain] = row
            if progress:
                progress(done, total, row)
    return [results[domain] for domain in normalized]


def import_website_file(path: str | Path) -> list[str]:
    source = Path(path)
    suffix = source.suffix.casefold()
    values: list[Any] = []
    if suffix == ".json":
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            rows = payload.get("websites") or payload.get("rows") or payload.get("values")
        else:
            rows = payload
        if not isinstance(rows, list):
            raise ValueError("JSON 必须是数组，或包含 websites/rows/values 数组")
        for row in rows:
            if isinstance(row, dict):
                values.append(next((row.get(key) for key in (
                    "normalized_placement", "placement", "website", "domain", "url",
                ) if row.get(key)), ""))
            else:
                values.append(row)
    elif suffix == ".csv":
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        if rows:
            header = [str(value).strip().casefold() for value in rows[0]]
            names = (
                "normalized_placement", "placement", "website", "domain", "url", "value",
                "网站", "域名", "网址",
            )
            index = next((header.index(name) for name in names if name in header), 0)
            start = 1 if any(name in header for name in names) else 0
            values.extend(row[index] for row in rows[start:] if len(row) > index)
    elif suffix in {".txt", ".list", ""}:
        values.extend(source.read_text(encoding="utf-8-sig").splitlines())
    else:
        raise ValueError("仅支持 TXT、CSV、JSON 网站名单")
    websites = normalize_websites(values)
    if not websites:
        raise ValueError("文件中没有有效网站域名")
    return websites
