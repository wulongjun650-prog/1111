#!/usr/bin/env python3
"""Run FOFA + Shodan dorks for jpvapou clones; merge hosts; optional fingerprint."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

DORKS_FOFA = [
    'body="/Client/GetClientInfoByID"',
    'body="NuclearProtesterFee"',
    'body="GetOrderDetailByMainOrder"',
    'title="系统登录 - 后台管理系统" && body="tailwindcss.js"',
    'header="X-AspNetMvc-Version: 5.2" && body="AdminLogin"',
    'cname="us.hygjcdn.com"',
    'body="查询失败:未将对象引用设置到对象的实例"',
]

DORKS_SHODAN = [
    'http.html:GetClientInfoByID',
    'http.html:NuclearProtesterFee',
    'http.html:GetOrderDetailByMainOrder',
    'http.html:tailwindcss.js http.html:AdminLogin',
    'http.headers:"X-AspNetMvc-Version: 5.2" http.html:AdminLogin',
    'ssl.cert.subject.cn:jpvapou.com',
]


def _get(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "jpvapou-clone-search/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def fofa_search(email: str, key: str, query: str, size: int = 100) -> list[str]:
    q64 = base64.b64encode(query.encode()).decode()
    params = urllib.parse.urlencode(
        {"email": email, "key": key, "qbase64": q64, "size": size, "fields": "host,ip,port,title"}
    )
    data = _get(f"https://fofa.info/api/v1/search/all?{params}")
    if data.get("error"):
        raise RuntimeError(f"FOFA error: {data}")
    hosts: list[str] = []
    for row in data.get("results") or []:
        if not row:
            continue
        host = row[0] if isinstance(row, list) else row.get("host")
        if host:
            hosts.append(str(host).split(":")[0].lower())
    return hosts


def shodan_search(api_key: str, query: str, pages: int = 2) -> list[str]:
    hosts: list[str] = []
    for page in range(1, pages + 1):
        params = urllib.parse.urlencode({"key": api_key, "query": query, "page": page})
        data = _get(f"https://api.shodan.io/shodan/host/search?{params}")
        for m in data.get("matches") or []:
            h = m.get("hostnames") or []
            ip = m.get("ip_str")
            if h:
                hosts.extend(x.lower() for x in h)
            elif ip:
                hosts.append(ip)
    return hosts


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="candidates.txt")
    p.add_argument("--json", default="search_results.json")
    p.add_argument("--probe", action="store_true", help="run fingerprint_probe.py on hits")
    args = p.parse_args()

    fofa_email = os.environ.get("FOFA_EMAIL", "").strip()
    fofa_key = os.environ.get("FOFA_KEY", "").strip()
    shodan_key = os.environ.get("SHODAN_API_KEY", "").strip()

    if not (fofa_email and fofa_key) and not shodan_key:
        print(
            "Set FOFA_EMAIL+FOFA_KEY and/or SHODAN_API_KEY in Cursor Secrets "
            "(recon/jpvapou-clones); values are not available on this VM until bound.",
            file=sys.stderr,
        )
        return 2

    all_hosts: set[str] = set()
    report: dict = {"fofa": {}, "shodan": {}}

    if fofa_email and fofa_key:
        for d in DORKS_FOFA:
            try:
                found = fofa_search(fofa_email, fofa_key, d)
                report["fofa"][d] = found
                all_hosts.update(found)
                print(f"[fofa] {d!r} -> {len(found)}", file=sys.stderr)
            except Exception as e:
                report["fofa"][d] = {"error": str(e)}
                print(f"[fofa] {d!r} ERROR {e}", file=sys.stderr)

    if shodan_key:
        for d in DORKS_SHODAN:
            try:
                found = shodan_search(shodan_key, d)
                report["shodan"][d] = found
                all_hosts.update(found)
                print(f"[shodan] {d!r} -> {len(found)}", file=sys.stderr)
            except Exception as e:
                report["shodan"][d] = {"error": str(e)}
                print(f"[shodan] {d!r} ERROR {e}", file=sys.stderr)

    Path(args.out).write_text("\n".join(sorted(all_hosts)) + ("\n" if all_hosts else ""), encoding="utf-8")
    Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(all_hosts)} hosts -> {args.out}", file=sys.stderr)

    if args.probe and all_hosts:
        import subprocess

        probe = Path(__file__).with_name("fingerprint_probe.py")
        subprocess.run(
            [sys.executable, str(probe), "--file", args.out, "--json-out", "hits.json"],
            check=False,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
