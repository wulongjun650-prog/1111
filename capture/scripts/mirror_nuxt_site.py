#!/usr/bin/env python3
"""Mirror a Nuxt site: sitemap pages, /_nuxt assets, same-origin resources."""

import argparse
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})

ASSET_RE = re.compile(
    r"""(?:href|src)=["']([^"']+)["']|"""
    r"""import\s*\(\s*["']([^"']+)["']\s*\)|"""
    r"""from\s+["']([^"']+)["']"""
)

STATIC_PREFIXES = ("/_nuxt/", "/assets/", "/static/", "/favicon", "/robots.txt")


def fetch(url: str, timeout: int = 60) -> requests.Response:
    for attempt in range(4):
        try:
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            return r
        except requests.RequestException:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url}")


def collect_sitemap_urls(base: str) -> list[str]:
    urls: set[str] = set()
    sm_url = urljoin(base, "/sitemap.xml")
    r = fetch(sm_url)
    if r.status_code != 200:
        return [base.rstrip("/") + "/"]
    root = ET.fromstring(r.content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemaps = root.findall(".//sm:sitemap/sm:loc", ns)
    if sitemaps:
        for loc in sitemaps:
            sub = fetch(loc.text.strip())
            if sub.status_code != 200:
                continue
            sub_root = ET.fromstring(sub.content)
            for u in sub_root.findall(".//sm:url/sm:loc", ns):
                urls.add(u.text.strip())
    else:
        for u in root.findall(".//sm:url/sm:loc", ns):
            urls.add(u.text.strip())
    if not urls:
        urls.add(base.rstrip("/") + "/")
    return sorted(urls)


def url_to_local(out_dir: Path, page_url: str, base_netloc: str) -> Path:
    parsed = urlparse(page_url)
    if parsed.netloc and parsed.netloc != base_netloc:
        raise ValueError(f"foreign host {parsed.netloc}")
    path = parsed.path or "/"
    if path.endswith("/"):
        return out_dir / path.lstrip("/") / "index.html"
    if "." not in Path(path).name:
        return out_dir / path.lstrip("/") / "index.html"
    return out_dir / path.lstrip("/")


def asset_url_to_local(out_dir: Path, asset_url: str, base: str, base_netloc: str) -> Path | None:
    if asset_url.startswith("data:") or asset_url.startswith("javascript:"):
        return None
    full = urljoin(base, asset_url)
    parsed = urlparse(full)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc and parsed.netloc != base_netloc:
        return None
    path = parsed.path
    if not path or path == "/":
        return None
    return out_dir / path.lstrip("/")


def extract_assets(text: str, base_path: str = "/") -> set[str]:
    """Extract asset paths; resolve relative imports against base_path (URL path of file)."""
    found: set[str] = set()
    for m in ASSET_RE.finditer(text):
        for g in m.groups():
            if not g or g.startswith(("data:", "javascript:", "http://", "https://")):
                continue
            if g.startswith("/"):
                found.add(g.split("?", 1)[0])
            elif base_path.startswith("/_nuxt/") and not g.startswith("../"):
                # relative chunk next to current module
                dirp = base_path.rsplit("/", 1)[0] + "/"
                found.add(urljoin(dirp, g).split("?", 1)[0])
    return found


def is_site_asset(path: str) -> bool:
    return path.startswith(STATIC_PREFIXES) or path in ("/favicon.ico", "/robots.txt")


def rewrite_html(html: str, page_url: str, base_netloc: str) -> str:
    """Convert same-origin absolute URLs to root-relative paths."""

    def repl(m: re.Match) -> str:
        attr, q, url = m.group(1), m.group(2), m.group(3)
        if url.startswith(("/", "./", "../")) or url.startswith("data:"):
            return m.group(0)
        parsed = urlparse(url)
        if parsed.netloc == base_netloc:
            new = parsed.path
            if parsed.query:
                new += "?" + parsed.query
            return f'{attr}={q}{new}{q}'
        return m.group(0)

    return re.sub(
        r'(href|src)=(["\'])(https?://[^"\']+)(["\'])',
        repl,
        html,
        flags=re.I,
    )


def download_file(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return False
    r = fetch(url)
    if r.status_code != 200:
        return False
    dest.write_bytes(r.content)
    return True


def mirror_site(base_url: str, out_dir: Path, workers: int = 8) -> dict:
    base_url = base_url.rstrip("/") + "/"
    parsed_base = urlparse(base_url)
    base_netloc = parsed_base.netloc
    out_dir.mkdir(parents=True, exist_ok=True)

    page_urls = collect_sitemap_urls(base_url)
    stats = {"pages": 0, "assets": 0, "errors": []}

    asset_queue: set[tuple[str, str]] = set()  # (asset_path, context_path)
    for page_url in page_urls:
        try:
            r = fetch(page_url)
            if r.status_code != 200:
                stats["errors"].append(f"{page_url} -> {r.status_code}")
                continue
            ct = r.headers.get("content-type", "")
            local = url_to_local(out_dir, page_url, base_netloc)
            local.parent.mkdir(parents=True, exist_ok=True)
            body = r.content
            if "text/html" in ct or page_url.endswith("/") or ".html" in local.name:
                text = r.text
                for a in extract_assets(text, urlparse(page_url).path or "/"):
                    if is_site_asset(a):
                        asset_queue.add((a, urlparse(page_url).path or "/"))
                text = rewrite_html(text, page_url, base_netloc)
                local.write_text(text, encoding="utf-8")
            else:
                local.write_bytes(body)
            stats["pages"] += 1
        except Exception as e:
            stats["errors"].append(f"{page_url} -> {e}")

    # Bootstrap from homepage for any assets missed by sitemap-only pages
    try:
        home = fetch(base_url)
        if home.status_code == 200:
            for a in extract_assets(home.text, "/"):
                if is_site_asset(a):
                    asset_queue.add((a, "/"))
    except Exception:
        pass

    # BFS through JS/CSS for lazy chunks
    seen_assets: set[str] = set()
    while asset_queue:
        batch = [t for t in asset_queue if t[0] not in seen_assets]
        if not batch:
            break
        for a, _ in batch:
            seen_assets.add(a)
        new_refs: set[tuple[str, str]] = set()
        for asset, ctx in batch:
            if not is_site_asset(asset):
                continue
            full = urljoin(base_url, asset.lstrip("/"))
            dest = asset_url_to_local(out_dir, asset, base_url, base_netloc)
            if not dest:
                continue
            try:
                if download_file(full, dest):
                    stats["assets"] += 1
                if dest.suffix in (".js", ".css", ".html"):
                    try:
                        text = dest.read_text(encoding="utf-8", errors="ignore")
                        for na in extract_assets(text, asset):
                            if is_site_asset(na):
                                new_refs.add((na, asset))
                    except Exception:
                        pass
            except Exception as e:
                stats["errors"].append(f"{full} -> {e}")
        asset_queue = {t for t in new_refs if t[0] not in seen_assets}

    # robots + favicon
    for extra in ("robots.txt", "favicon.ico"):
        try:
            u = urljoin(base_url, extra)
            d = out_dir / extra
            if download_file(u, d):
                stats["assets"] += 1
        except Exception:
            pass

    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("out_dir")
    args = ap.parse_args()
    stats = mirror_site(args.base_url, Path(args.out_dir))
    print(stats)


if __name__ == "__main__":
    main()
