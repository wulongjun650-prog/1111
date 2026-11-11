#!/usr/bin/env python3
"""Upload changed ablab modules and restart services via aaPanel. Run on a machine with panel access."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ABLAB = ROOT / 'ablab'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
FILES = [
    'visitor_surface.py', 'web.py', 'auth.py', 'human_check.py', 'page_implant.py', 'models.py',
    'cloudflare.py', 'watrust.py', 'certificate_acceptance.py', 'nginx_entry.py', 'nginx_tls.py',
    'origin_setup.py', 'panel_sites.py', 'site_erasure.py', 'speedcheck.py',
]
REMOTE_ROOT = '/opt/ab-lab/ablab'
SWITCH = '/opt/ab-lab/deploy/switch_admin_origin.sh'


def _handoff_panel():
    text = (Path('/workspace/HANDOFF.md') if Path('/workspace/HANDOFF.md').is_file() else Path(__file__).resolve().parents[2] / 'HANDOFF.md').read_text(encoding='utf-8')
    m = re.search(r'面板在 `(http[^`]+)`，账号 `([^`]+)`，密码 `([^`]+)`', text)
    if not m:
        raise SystemExit('HANDOFF panel line missing')
    return m.group(1).rstrip('/'), m.group(2), m.group(3)


def _login(client: httpx.Client, base: str, user: str, password: str) -> str:
    page = client.get(f'{base}/login', follow_redirects=True)
    page.raise_for_status()
    token_m = re.search(r'id="request_token_head"[^>]*token="([^"]+)"', page.text)
    key_m = re.search(r'id="request_token_head"[^>]*public_key="([^"]+)"', page.text)
    if not token_m or not key_m:
        raise SystemExit('panel login page tokens missing')
    import rsa  # noqa: WPS433

    digest = hashlib.md5(hashlib.md5(password.encode()).hexdigest().encode()).hexdigest()
    encrypted = base64.b64encode(rsa.encrypt(digest.encode(), rsa.PublicKey.load_pkcs1_openssl_pem(key_m.group(1).encode()))).decode()
    resp = client.post(f'{base}/login', data={'username': user, 'password': encrypted}, headers={'x-http-token': token_m.group(1)})
    resp.raise_for_status()
    if '宝塔Linux面板' not in resp.text and resp.status_code != 302:
        raise SystemExit('panel login failed')
    session = resp.cookies.get('bt_session') or client.cookies.get('bt_session')
    if not session:
        for cookie in client.cookies.jar:
            if cookie.name == 'bt_session':
                session = cookie.value
    home = client.get(f'{base}/', follow_redirects=True)
    token_m = re.search(r'id="request_token_head"[^>]*token="([^"]+)"', home.text)
    if not token_m:
        raise SystemExit('panel session token missing')
    return token_m.group(1)


def _save_file(client: httpx.Client, base: str, token: str, path: str, body: str) -> None:
    resp = client.post(
        f'{base}/files?action=SaveFileBody',
        data={'data': body, 'encoding': 'utf-8', 'path': path},
        headers={'x-http-token': token},
    )
    payload = resp.json()
    if payload.get('status') is not True:
        raise SystemExit(f'SaveFileBody failed for {path}: {payload!r}')


def _exec(client: httpx.Client, base: str, token: str, shell: str) -> str:
    nonce = f'ABSHIP{int(time.time())}'
    script = f'echo {nonce}; {shell}; echo EXIT:$?'
    client.post(f'{base}/files?action=SaveFileBody', data={'data': script, 'path': '/tmp/panelShell.sh', 'encoding': 'utf-8'}, headers={'x-http-token': token})
    client.post(f'{base}/files?action=ExecShell', data={'shell': '/tmp/panelShell.sh', 'path': '/tmp'}, headers={'x-http-token': token})
    for _ in range(30):
        time.sleep(1)
        log = client.post(f'{base}/files?action=GetFileBody', data={'path': '/tmp/panelShell.pl'}, headers={'x-http-token': token}).json()
        body = log.get('data') or ''
        if nonce in body and 'EXIT:' in body:
            if 'EXIT:0' not in body:
                raise SystemExit(f'remote command failed:\n{body[-4000:]}')
            return body
    raise SystemExit('panel shell timed out')


def main() -> int:
    base, user, password = _handoff_panel()
    with httpx.Client(headers={'User-Agent': UA}, timeout=60, verify=False) as client:
        token = _login(client, base, user, password)
        session_prefix = client.cookies.get('bt_session_id') or ''
        if session_prefix:
            client.headers['Cookie'] = client.headers.get('Cookie', '')
        for name in FILES:
            text = (ABLAB / name).read_text(encoding='utf-8')
            _save_file(client, base, token, f'{REMOTE_ROOT}/{name}', text)
        switch = (ROOT / 'deploy' / 'switch_admin_origin.sh').read_text(encoding='utf-8')
        _save_file(client, base, token, SWITCH, switch)
        compile_cmd = (
            f'/opt/ab-lab/.venv/bin/python -m py_compile {" ".join(REMOTE_ROOT + "/" + f for f in FILES)}'
        )
        _exec(client, base, token, compile_cmd)
        _exec(client, base, token, f'chmod +x {SWITCH} && bash {SWITCH}')
    print('ship_ok')
    return 0


if __name__ == '__main__':
    try:
        import rsa  # noqa: F401
    except ImportError:
        raise SystemExit('pip install rsa httpx') from None
    raise SystemExit(main())
