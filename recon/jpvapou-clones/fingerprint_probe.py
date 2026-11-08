#!/usr/bin/env python3
"""Probe hosts for the same ASP.NET stack fingerprint as jpvapou.com."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

IDOR_PATH = "/Client/GetClientInfoByID?ID=4"
ADMIN_PATH = "/Admin/Login"
ADMIN_MARK = "后台管理系统".encode("utf-8")


@dataclass
class Hit:
    host: str
    kind: str
    detail: str
    server: str = ""
    aspnet_mvc: str = ""


def _open(url: str, ctx: ssl.SSLContext, timeout: float) -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": "jpvapou-fingerprint/1.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        body = resp.read(12_000)
        headers = {k: v for k, v in resp.headers.items()}
        return body, headers


def probe_host(host: str, timeout: float, ctx: ssl.SSLContext) -> Hit | None:
    host = host.strip().lower()
    if not host or host.startswith("#"):
        return None
    for scheme in ("http", "https"):
        base = f"{scheme}://{host}"
        try:
            body, headers = _open(base + IDOR_PATH, ctx, timeout)
            if body.startswith(b'{"Code"'):
                return Hit(
                    host=host,
                    kind="same_api_idor",
                    detail=body[:160].decode("utf-8", "replace"),
                    server=headers.get("Server", ""),
                    aspnet_mvc=headers.get("X-AspNetMvc-Version", ""),
                )
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
        try:
            body, headers = _open(base + ADMIN_PATH, ctx, timeout)
            if b"tailwindcss.js" in body and ADMIN_MARK in body and b"AdminLogin" in body:
                return Hit(
                    host=host,
                    kind="same_admin_ui",
                    detail="tailwindcss.js + AdminLogin + 后台管理系统",
                    server=headers.get("Server", ""),
                    aspnet_mvc=headers.get("X-AspNetMvc-Version", ""),
                )
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("hosts", nargs="*", help="hosts to probe (or stdin / --file)")
    p.add_argument("--file", help="host list, one per line")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--workers", type=int, default=40)
    p.add_argument("--json-out", help="write hits as JSON array")
    args = p.parse_args()

    hosts: list[str] = list(args.hosts)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            hosts.extend(ln.strip() for ln in fh if ln.strip())
    if not hosts and not sys.stdin.isatty():
        hosts = [ln.strip() for ln in sys.stdin if ln.strip()]

    if not hosts:
        p.error("no hosts")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    hits: list[Hit] = []
    with concurrent.futures.ThreadPoolExecutor(args.workers) as ex:
        for hit in ex.map(lambda h: probe_host(h, args.timeout, ctx), hosts):
            if hit:
                hits.append(hit)
                print(f"{hit.kind}\t{hit.host}\t{hit.aspnet_mvc}\t{hit.detail[:80]}", flush=True)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump([asdict(h) for h in hits], f, ensure_ascii=False, indent=2)

    print(f"# scanned={len(hosts)} hits={len(hits)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
