import asyncio
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path as PathParameter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import maxminddb
from pydantic import Field

from .archives import EDITABLE_EXTENSIONS, MAX_FILE, MAX_ZIP, editable_file, import_content, read_source, resolve_file, source_files
from .analytics import summarize
from .models import ConfigUpdate, DirectEntry, LinkInput, RedirectApply, RedirectPresetInput, RedirectSplit, SourceEdit, StrictModel, TrackingApply, TrackingSnippetInput, VersionChoice, Visitor, WhatsAppNumberApply, WhatsAppNumberInput, WhatsAppReception, WhatsAppSplit
from .tracking import normalize_conversion, normalize_ga4
from .linkcheck import LinkChecker
from .watrust import TrustChecker, device_from_environ
from .redirects import public_occurrences, rewrite_reception_bytes, rewrite_text, scan_bundle
from .rules import decide
from .cloudflare import Cloudflare
from .provisioning import ProvisioningError
from .reputation import GoogleReputation
from .store import Conflict, Store
from .auth import Auth, COOKIE, SESSION_SECONDS
from .sites import Registry

ROOT = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger('ablab')
SPLIT_COOKIE = 'ab_split'
WA_COOKIE = 'ab_wa'
_split_scans = {}
_split_scan_lock = threading.Lock()
Slot = Literal['A', 'B']
VersionId = Annotated[str, PathParameter(pattern=r'^[a-f0-9]{32}$')]


class SiteMetadata(StrictModel):
    note: str = Field(max_length=200, strict=True)


class AccountCreate(StrictModel):
    username: str = Field(min_length=1, max_length=64, strict=True)
    password: str = Field(min_length=12, max_length=256, strict=True)
    role: Literal['agent', 'observer'] = 'agent'


class AccountEnabled(StrictModel):
    enabled: bool = Field(strict=True)


class AccountPassword(StrictModel):
    password: str = Field(min_length=12, max_length=256, strict=True)


class SiteOwner(StrictModel):
    owner_id: str = Field(min_length=1, max_length=64, strict=True)


def preview_signature(store, account_store, site, expiry, version_id, issuer, local_only):
    # Account revocation and ownership transfers also revoke private drafts.
    # Target reads identity only; it must not initialize or migrate Auth tables.
    if issuer == 'local' and local_only:
        epoch = 0
    else:
        with account_store.connect() as db:
            account = db.execute('SELECT role,enabled,auth_epoch FROM accounts WHERE id=?', (issuer,)).fetchone()
        if not account or not account['enabled'] or (account['role'] != 'admin' and site['owner_id'] != issuer):
            return None
        epoch = account['auth_epoch']
    material = f"{expiry}:{version_id}:{site['id']}:{site['owner_id']}:{site['owner_epoch']}:{issuer}:{epoch}"
    return hmac.new(store.secret, material.encode(), hashlib.sha256).hexdigest()


class LocalBoundary:
    """Loopback only; public mode additionally requires explicit trusted proxy data."""
    def __init__(self, app, port, admin=False, csrf='', target_port=8766, deployment=None, auth=None, registry=None):
        self.app, self.port, self.admin, self.csrf = app, port, admin, csrf
        self.target_port = target_port
        self.deployment, self.auth = deployment, auth
        self.registry = registry

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode('latin1')
        allowed_hosts = (f'127.0.0.1:{self.port}', f'localhost:{self.port}')
        peer = scope.get('client')
        try:
            local = bool(peer) and ipaddress.ip_address(peer[0]).is_loopback
        except ValueError:
            local = False
        origin = headers.get(b'origin')
        expected_origin = f'http://{host}'
        csrf = self.csrf
        state = scope.setdefault('state', {})
        site = None
        is_ingest = self.admin and scope['method'] == 'POST' and re.fullmatch(r'/api/wa-trust/screen/[A-Za-z0-9_-]{20,80}', scope['path']) is not None
        if self.deployment:
            allowed_hosts = (self.deployment.host(self.admin),)
            if not self.admin and self.registry:
                site = self.registry.for_host(host)
                allowed_hosts = (host,) if site else ()
            expected_origin = self.deployment.admin_origin
            visitor_ip = restore_visitor_ip(headers.get(b'x-real-ip', b''), headers.get(b'cf-connecting-ip', b''))
            if visitor_ip is None:
                local = False
            else:
                state['visitor_ip'] = visitor_ip
            local &= headers.get(b'x-forwarded-proto') == b'https'
        invalid = not local or host not in allowed_hosts
        if self.admin:
            invalid |= origin is not None and origin != expected_origin.encode()
            invalid |= headers.get(b'sec-fetch-site', b'none') not in (b'none', b'same-origin')
        if invalid:
            await JSONResponse({'detail': '来源、域名或代理校验失败'}, status_code=403)(scope, receive, send)
            return
        if not self.admin and self.registry:
            site = site or self.registry.get('default')
            # The original target origin also hosts signed, site-specific previews.
            # Local development alone permits full site access under this prefix.
            parts = scope['path'].split('/', 3)
            if len(parts) == 4 and parts[1] == '_sites':
                if self.deployment and (host != self.deployment.host(False) or not parts[3].startswith('_preview/')):
                    await JSONResponse({'detail': '资源不存在'}, status_code=404)(scope, receive, send)
                    return
                try:
                    site = self.registry.get(parts[2])
                except KeyError:
                    await JSONResponse({'detail': '站点不存在'}, status_code=404)(scope, receive, send)
                    return
                state['public_prefix'] = f'/_sites/{site["id"]}'
                scope = dict(scope, path='/' + parts[3])
            if not site['enabled']:
                await JSONResponse({'detail': '站点已暂停'}, status_code=503)(scope, receive, send)
                return
            state['site_id'] = site['id']
        if self.admin and self.auth:
            request = Request(scope)
            session = self.auth.session(request.cookies.get(COOKIE))
            public = is_ingest or (scope['path'] == '/api/login' and scope['method'] == 'POST') or (scope['method'] in ('GET', 'HEAD') and scope['path'] in ('/login', '/static/login.js', '/static/app.css'))
            if not public and session is None:
                response = RedirectResponse('/login', status_code=307) if scope['path'] == '/' else JSONResponse({'detail': '请先登录'}, status_code=401)
                response.headers['Cache-Control'] = 'no-store'
                await response(scope, receive, send)
                return
            if session and not public:
                csrf = session['csrf']
                state['username'] = session['username']
                state['account_id'] = session['account_id']
                state['role'] = session['role']
                # Reject before reading uploads or invoking route dependencies.
                match = re.match(r'^/api/sites/([^/]+)(?:/|$)', scope['path'])
                if match:
                    try:
                        owned = self.registry.get(match.group(1))
                        if session['role'] not in ('admin', 'observer') and owned['owner_id'] != session['account_id']:
                            raise KeyError(match.group(1))
                    except KeyError:
                        await JSONResponse({'detail': '站点不存在'}, status_code=404)(scope, receive, send)
                        return
                # The domains page refreshes Google's cached lookup. That write
                # stays inside the reputation cache and does not change a site.
                observer_may_refresh = scope['method'] == 'POST' and re.fullmatch(
                    r'/api/sites/(?:[a-f0-9]{32}|default)/reputation/check', scope['path'])
                if (session['role'] == 'observer' and scope['method'] not in ('GET', 'HEAD')
                        and scope['path'] != '/api/logout' and not observer_may_refresh):
                    await JSONResponse({'detail': '观察号只能查看'}, status_code=403)(scope, receive, send)
                    return
        state['csrf'] = csrf
        if self.admin and scope['method'] not in ('GET', 'HEAD') and not is_ingest:
            if origin != expected_origin.encode() or not secrets.compare_digest(headers.get(b'x-csrf-token', b''), csrf.encode()):
                await JSONResponse({'detail': '请求校验失败，请刷新页面'}, status_code=403)(scope, receive, send)
                return
        is_upload = re.fullmatch(r'/api/(?:sites/[a-f0-9]{32}/|sites/default/)?upload/[AB]', scope['path'])
        limit = MAX_ZIP if self.admin and is_upload else 256 * 1024
        if is_ingest:
            limit = 2_000_000
        if self.admin and scope['method'] == 'POST' and re.fullmatch(r'/api/(?:sites/(?:[a-f0-9]{32}|default)/)?b-redirects/(?:apply|numbers/apply)', scope['path']):
            limit = 512 * 1024  # Up to 5,000 selected SHA-256 occurrence IDs.
        if self.admin and scope['method'] == 'POST' and re.fullmatch(r'/api/(?:sites/(?:[a-f0-9]{32}|default)/)?source/[AB]/[a-f0-9]{32}', scope['path']):
            # JSON can escape each UTF-8 byte as six ASCII characters.
            limit = MAX_FILE * 6 + 4096
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > limit:
                await JSONResponse({'detail': '请求体超过大小限制'}, status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get('more_body'):
                break
        pending = b''.join(chunks)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': pending, 'more_body': False}
            return await receive()

        async def secured(message):
            if message['type'] == 'http.response.start':
                extra = [(b'x-content-type-options', b'nosniff'), (b'cache-control', b'no-store'), (b'referrer-policy', b'no-referrer'), (b'permissions-policy', b'camera=(), microphone=(), geolocation=()')]
                if self.admin:
                    target_origin = self.deployment.target_origin if self.deployment else f'http://127.0.0.1:{self.target_port}'
                    policy = f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src {target_origin}; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
                    extra += [(b'content-security-policy', policy.encode()), (b'x-frame-options', b'DENY')]
                else:
                    # The page keeps its own origin so GA4 can store a cookie and send the visit.
                    # The admin is a different host, so this page still cannot read the admin session.
                    ancestors = self.deployment.admin_origin if self.deployment else 'http://127.0.0.1:8765 http://localhost:8765'
                    forms = 'https:' if self.deployment else f'https: http://127.0.0.1:{self.port}'
                    # Older landings assign https://api.whatsapp.com in this tab.
                    # Newer ones open https://wa.me in a new tab on desktop and
                    # set location to whatsapp:// on phones. Without the popup
                    # and custom-protocol flags, that click stays on the page.
                    # The new tab must leave this sandbox so WhatsApp Web is a
                    # normal page. This still cannot read the admin session.
                    policy = (
                        "sandbox allow-scripts allow-forms allow-same-origin "
                        "allow-popups allow-popups-to-escape-sandbox "
                        "allow-top-navigation-to-custom-protocols; "
                        f"frame-ancestors {ancestors}; form-action {forms}; object-src 'none'"
                    )
                    extra.append((b'content-security-policy', policy.encode()))
                message['headers'] = [(k, v) for k, v in message.get('headers', []) if k.lower() not in {key for key, _ in extra}] + extra
            await send(message)
        await self.app(scope, replay, secured)


# Published ranges from https://www.cloudflare.com/ips-v4 and /ips-v6.
# A client can set CF-Connecting-IP itself, so it counts only when the
# connecting address is one of these edges.
CLOUDFLARE_NETS = tuple(ipaddress.ip_network(item) for item in (
    '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22',
    '141.101.64.0/18', '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20',
    '197.234.240.0/22', '198.41.128.0/17', '162.158.0.0/15', '104.16.0.0/13',
    '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
    '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32',
    '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32',
))


def _address(value):
    try:
        text = value.decode('ascii').strip() if isinstance(value, (bytes, bytearray)) else str(value).strip()
        return ipaddress.ip_address(text)
    except (AttributeError, ValueError, UnicodeError):
        return None


def displayed_hong_kong(ip, country):
    """True when the visit list shows this row as Hong Kong, including old Japan-edge rows."""
    shown = shown_country(ip, country)
    return isinstance(shown, str) and shown.upper() == 'HK'


def shown_country(ip, country):
    """Old rows stored Cloudflare's Japan edge as a network. Show those as Hong Kong.

    A complete address keeps the country stored for that address.
    """
    if country != 'JP' or '/' not in str(ip or ''):
        return country
    try:
        network = ipaddress.ip_network(str(ip), strict=False)
    except (TypeError, ValueError):
        return country
    if any(network.version == block.version and network.subnet_of(block) for block in CLOUDFLARE_NETS):
        return 'HK'
    return country


def _present_logs(data):
    for item in data.get('items', []):
        item['country'] = shown_country(item.get('ip'), item.get('country'))
    return data


def restore_visitor_ip(edge_header, connecting_header):
    """Use the visitor address Cloudflare saw, not the Japan edge address."""
    edge = _address(edge_header)
    if edge is None:
        return None
    connecting = _address(connecting_header)
    if connecting is not None and connecting.is_global and any(edge in network for network in CLOUDFLARE_NETS):
        return str(connecting)
    return str(edge)


def geo_path():
    return os.environ.get('AB_GEOIP_PATH') or ROOT / 'resources' / 'country.mmdb'


def geo_country(ip):
    try:
        address = ipaddress.ip_address(ip)
        if not address.is_global or address.is_multicast:
            return None
        with maxminddb.open_database(geo_path()) as reader:
            row = reader.get(ip) or {}
            value = row.get('country', {}).get('iso_code')
            return value if isinstance(value, str) and len(value) == 2 and value.isascii() and value.isalpha() and value.isupper() and value not in ('XX', 'ZZ') else None
    except (OSError, ValueError, maxminddb.InvalidDatabaseError):
        return None


def geo_ready():
    return geo_status()['ready']


def geo_status():
    try:
        with maxminddb.open_database(geo_path()) as reader:
            metadata = reader.metadata()
            ready = any(word in metadata.database_type.lower() for word in ('country', 'city'))
            return {'ready': ready, 'reason': 'ready' if ready else 'unsupported', 'database': metadata.database_type[:80], 'built_at': metadata.build_epoch, 'bundled': not bool(os.environ.get('AB_GEOIP_PATH'))}
    except (OSError, ValueError, maxminddb.InvalidDatabaseError):
        return {'ready': False, 'reason': 'unavailable', 'database': '', 'built_at': None, 'bundled': not bool(os.environ.get('AB_GEOIP_PATH'))}


def base_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(Conflict)
    async def conflict(_request, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(_request, _error):
        return JSONResponse({'detail': '站点不存在'}, status_code=404)

    @app.exception_handler(Exception)
    async def failure(_request, error):
        event_id = secrets.token_hex(4)
        LOGGER.error('Internal failure %s (%s)', event_id, type(error).__name__)
        return JSONResponse({'detail': '内部错误，操作未确认完成', 'event_id': event_id}, status_code=500)
    return app


def create_admin(data_dir, port=8765, target_port=8766, deployment=None, registry_dir=None, server_ip='', cloudflare=None):
    if server_ip:
        from .provisioning import public_ipv4
        server_ip = public_ipv4(server_ip)
    app, store = base_app(), Store(data_dir)
    app.state.csrf = secrets.token_urlsafe(32)
    app.state.store = store
    registry = Registry(data_dir, deployment.host(False) if deployment else '', [deployment.host(True)] if deployment else [], registry_dir)
    app.state.registry = registry
    reputation = GoogleReputation(registry, os.environ.get('AB_GOOGLE_WEB_RISK_KEY', ''))
    app.state.reputation = reputation
    if cloudflare is None:
        cloudflare = Cloudflare(os.environ.get('AB_CLOUDFLARE_TOKEN', ''), os.environ.get('AB_CLOUDFLARE_TEMPLATE', ''), server_ip)
        if cloudflare.token:
            def watch_cloudflare_https():
                from .cloudflare import repair_strict_ssl
                while True:
                    try:
                        repair_strict_ssl(registry, cloudflare)
                    except Exception as error:
                        LOGGER.error('Cloudflare HTTPS repair failed (%s)', type(error).__name__)
                    time.sleep(60)
            threading.Thread(target=watch_cloudflare_https, name='cloudflare-https', daemon=True).start()
    app.state.cloudflare = cloudflare
    accounts = Auth(store)
    app.state.auth = accounts
    auth = accounts if deployment else None
    if auth and not auth.ready():
        raise ValueError('公网模式需要先在服务器终端设置管理员账号密码')
    target_url = deployment.target_origin if deployment else f'http://127.0.0.1:{target_port}'
    app.add_middleware(LocalBoundary, port=port, admin=True, csrf=app.state.csrf, target_port=target_port, deployment=deployment, auth=auth, registry=registry)
    templates = Jinja2Templates(directory=str(ROOT / 'templates'))
    app.mount('/static', StaticFiles(directory=str(ROOT / 'static'), check_dir=False), name='static')

    def principal(request):
        if auth:
            return accounts.get_account(request.state.account_id)
        return accounts.admin_account() or {'id': 'admin', 'username': '本地管理员', 'role': 'admin', 'enabled': True, 'created': 0}

    def sees_everything(account):
        return account['role'] in ('admin', 'observer')

    def require_admin(request):
        account = principal(request)
        if account['role'] != 'admin':
            raise HTTPException(403, '需要总管理员权限')
        return account

    def authorized_site(request, site_id):
        site = registry.get(site_id)
        account = principal(request)
        if not sees_everything(account) and site['owner_id'] != account['id']:
            raise HTTPException(404, '站点不存在')
        return site

    def site_store(request: Request):
        if 'site_id' not in request.path_params and not sees_everything(principal(request)):
            raise HTTPException(404, '请使用当前域名的操作入口')
        site_id = request.path_params.get('site_id', 'default')
        authorized_site(request, site_id)
        return registry.store(site_id)

    def state(store, site_id):
        config, revision = store.config()
        site = registry.get(site_id)
        visit_url = target_url if site_id == 'default' else (f'https://{site["domain"]}' if deployment else f'{target_url}/_sites/{site_id}/')
        return {'config': config.model_dump(), 'revision': revision, 'versions': store.versions(), 'slots': store.slots(), 'links': store.links(), 'stats': store.stats(), 'health': {'geoip': geo_ready(), 'geoip_detail': geo_status(), 'local_only': deployment is None}, 'site': site, 'target_url': visit_url, 'preview_origin': target_url}

    def purge_site_cache(site):
        """Drop the cached landing page so a slot change is what visitors see."""
        if not site.get('cf_zone_id') or not cloudflare.configured:
            return None
        try:
            cloudflare.purge(site['cf_zone_id'], site['domain'])
        except ProvisioningError as error:
            return {'ok': False, 'detail': str(error)[:180]}
        return {'ok': True}

    @app.get('/api/sites')
    def list_sites(request: Request):
        account = principal(request)
        everything = sees_everything(account)
        sites = reputation.catalog(registry.list(None if everything else account['id']))
        if everything:
            names = {row['id']: row['username'] for row in accounts.list_accounts()}
            for site in sites:
                site['owner_username'] = names.get(site['owner_id'], account['username'] if site['owner_id'] == 'admin' else '账号不存在')
        return {'sites': sites, 'account': account, 'google_reputation_configured': reputation.configured,
                'cloudflare_configured': cloudflare.configured, 'cloudflare_template': cloudflare.template_label,
                'server_ip': server_ip, 'record_type': 'A', 'local_only': deployment is None}

    @app.get('/api/me')
    def whoami(request: Request):
        return {'account': principal(request)}

    @app.get('/api/accounts')
    def list_accounts(request: Request):
        account = principal(request)
        if not sees_everything(account):
            raise HTTPException(403, '需要总管理员权限')
        counts = {}
        for site in registry.list():
            counts[site['owner_id']] = counts.get(site['owner_id'], 0) + 1
        return {'items': [account | {'domain_count': counts.get(account['id'], 0)} for account in accounts.list_accounts()]}

    @app.post('/api/accounts')
    def create_account(request: Request, body: AccountCreate):
        require_admin(request)
        created = accounts.create_observer(body.username, body.password) if body.role == 'observer' else accounts.create_agent(body.username, body.password)
        return {'account': created}

    @app.patch('/api/accounts/{account_id}')
    def enable_account(account_id: str, request: Request, body: AccountEnabled):
        require_admin(request)
        return {'account': accounts.set_enabled(account_id, body.enabled)}

    @app.post('/api/accounts/{account_id}/password')
    def reset_account_password(account_id: str, request: Request, body: AccountPassword):
        require_admin(request)
        accounts.reset_agent_password(account_id, body.password)
        return {'ok': True}

    @app.put('/api/sites/{site_id}/owner')
    def assign_site_owner(site_id: str, request: Request, body: SiteOwner):
        require_admin(request)
        target = accounts.get_account(body.owner_id)
        if target['role'] not in ('admin', 'agent'):
            raise ValueError('观察号不能作为域名归属')
        return {'site': registry.assign_owner(site_id, body.owner_id)}

    @app.post('/api/sites/{site_id}/reputation/check')
    def check_site_reputation(site_id: str, request: Request):
        authorized_site(request, site_id)
        return {'result': reputation.check(site_id)}

    def apply_cloudflare(site):
        try:
            attached = cloudflare.attach(site['domain'])
        except ProvisioningError as error:
            names = ','.join(getattr(error, 'nameservers', ()) or ())
            zone_id = getattr(error, 'zone_id', '') or ''
            detail = str(error)[:400]
            updated = registry.set_cloudflare(site['id'], zone_id, names, 'failed', detail)
            if zone_id and site['stage'] != 'active':
                registry.transition(updated['id'], updated['generation'], 'waiting_dns', detail)
                updated = registry.get(site['id'])
            return updated, {'ok': False, 'detail': detail, 'nameservers': names.split(',') if names else [], 'status': 'failed'}
        names = ','.join(attached['nameservers'])
        updated = registry.set_cloudflare(site['id'], attached['zone_id'], names, attached['status'], attached['detail'])
        if site['stage'] != 'active':
            if attached['status'] == 'active' and registry.transition(updated['id'], updated['generation'], 'dns_verified', attached['detail']):
                registry.transition(updated['id'], updated['generation'], 'unsupported', '解析条件已满足；面板自动写入适配尚未完成，未创建目录、站点或证书')
            else:
                registry.transition(updated['id'], updated['generation'], 'waiting_dns', attached['detail'])
        return registry.get(site['id']), {
            'ok': True, 'detail': attached['detail'], 'nameservers': attached['nameservers'], 'status': attached['status'],
        }

    @app.post('/api/sites')
    def add_site(body: dict, request: Request):
        if not isinstance(body, dict) or set(body) - {'domain', 'cloudflare'} or 'domain' not in body:
            raise ValueError('只需提交域名，可另选是否套用 Cloudflare')
        use_cloudflare = body.get('cloudflare', False)
        if type(use_cloudflare) is not bool:
            raise ValueError('Cloudflare 选项无效')
        if use_cloudflare and not cloudflare.configured:
            raise ValueError('服务器未配置 Cloudflare，请取消勾选，按原来的 A 记录接入')
        if use_cloudflare and not cloudflare.origin_ip:
            raise ValueError('服务器公网 IP 未配置，无法把 Cloudflare 回源到本机')
        site = registry.add(body['domain'], principal(request)['id'])
        result = {'site': site, 'server_ip': server_ip, 'record_type': 'A', 'cloudflare': None}
        if use_cloudflare:
            result['site'], result['cloudflare'] = apply_cloudflare(site)
        return result

    @app.post('/api/sites/{site_id}/cloudflare')
    def retry_cloudflare(site_id: str, request: Request):
        site = authorized_site(request, site_id)
        if site['id'] == 'default':
            raise ValueError('原有站点不在这里套用 Cloudflare')
        if not cloudflare.configured:
            raise ValueError('服务器未配置 Cloudflare')
        if not cloudflare.origin_ip:
            raise ValueError('服务器公网 IP 未配置，无法把 Cloudflare 回源到本机')
        if not site['cf_status'] and site['stage'] != 'active':
            raise ValueError('请先等本机证书接入完成，再套用 Cloudflare')
        site, attached = apply_cloudflare(site)
        return {'site': site, 'cloudflare': attached}

    @app.post('/api/sites/{site_id}/cloudflare/status')
    def cloudflare_status(site_id: str, request: Request):
        site = authorized_site(request, site_id)
        if not site['cf_zone_id']:
            raise ValueError('这个域名没有接入 Cloudflare')
        if not cloudflare.configured:
            raise ValueError('服务器未配置 Cloudflare')
        try:
            state = cloudflare.confirm(site['cf_zone_id'], site['domain'])
        except ProvisioningError as error:
            detail = str(error)[:400]
            names = [part for part in site['cf_nameservers'].split(',') if part]
            return {'site': site, 'cloudflare': {'ok': False, 'status': site['cf_status'] or 'pending', 'active': False, 'nameservers': names, 'detail': detail}}
        status = 'active' if state['active'] else 'pending'
        names = ','.join(state['nameservers']) or site['cf_nameservers']
        if status == 'active':
            detail = 'Cloudflare 已生效，回源指向服务器'
        else:
            shown = '、'.join(state['nameservers']) or site['cf_nameservers'].replace(',', '、')
            detail = f'NS 尚未生效，正在自动检查。请到注册商改为：{shown}'[:400]
        if (site['cf_status'], site['cf_nameservers'], site['cf_detail']) != (status, names, detail):
            site = registry.set_cloudflare(site['id'], site['cf_zone_id'], names, status, detail)
        if status == 'active' and site['stage'] == 'waiting_dns':
            if registry.transition(site['id'], site['generation'], 'dns_verified', detail):
                registry.transition(site['id'], site['generation'], 'unsupported', '解析条件已满足；面板自动写入适配尚未完成，未创建目录、站点或证书')
            site = registry.get(site['id'])
        return {'site': site, 'cloudflare': {'ok': True, 'status': status, 'active': status == 'active', 'nameservers': [part for part in names.split(',') if part], 'detail': detail}}

    @app.post('/api/sites/{site_id}/cloudflare/disable')
    def disable_cloudflare(site_id: str, request: Request):
        site = authorized_site(request, site_id)
        if site['id'] == 'default':
            raise ValueError('原有站点不在这里关闭 Cloudflare')
        if not site['cf_zone_id'] and not site['cf_status']:
            raise ValueError('这个域名没有接入 Cloudflare')
        record_address = ''
        if site['cf_zone_id']:
            if not cloudflare.configured:
                raise ValueError('服务器未配置 Cloudflare')
            turned = cloudflare.disable(site['cf_zone_id'], site['domain'])
            record_address = turned['address']
        if site['stage'] in ('active', 'legacy', 'paused'):
            detail = '已关闭 Cloudflare 代理。访客将直接访问 A 记录。本机站点保持不变。'
        else:
            target = server_ip or '本机公网 IP'
            detail = f'已关闭 Cloudflare 代理。请把 A 记录指到 {target}，指向本机后会自动建站并申请证书。'
        updated = registry.clear_cloudflare(site['id'], detail)
        return {
            'site': updated, 'server_ip': server_ip, 'record_address': record_address,
            'cloudflare': {'ok': True, 'proxied': False, 'detail': detail},
        }

    @app.post('/api/sites/{site_id}/cloudflare/purge')
    def purge_cloudflare(site_id: str, request: Request):
        site = authorized_site(request, site_id)
        if not site['cf_zone_id']:
            raise ValueError('这个域名没有接入 Cloudflare')
        if not cloudflare.configured:
            raise ValueError('服务器未配置 Cloudflare')
        cloudflare.purge(site['cf_zone_id'], site['domain'])
        return {'ok': True}

    @app.patch('/api/sites/{site_id}/metadata')
    def update_site_metadata(site_id: str, body: SiteMetadata, request: Request):
        authorized_site(request, site_id)
        return {'site': registry.set_note(site_id, body.note)}

    @app.post('/api/sites/{site_id}/availability/{action}')
    def update_site_availability(site_id: str, action: Literal['online', 'offline'], request: Request):
        authorized_site(request, site_id)
        return {'site': registry.set_availability(site_id, action)}

    @app.post('/api/sites/{site_id}/provision/{action}')
    def provision_control(site_id: str, action: Literal['pause', 'retry'], request: Request):
        authorized_site(request, site_id)
        return {'site': registry.control(site_id, action)}

    @app.get('/api/sites/{site_id}/provision/events')
    def provision_events(site_id: str, request: Request):
        authorized_site(request, site_id)
        return {'items': registry.events(site_id)}

    routes = APIRouter()

    @app.get('/', response_class=HTMLResponse)
    def index(request: Request):
        account = principal(request)
        return templates.TemplateResponse(request=request, name='index.html', context={'csrf_token': request.state.csrf, 'target_url': target_url, 'deployment': bool(deployment), 'username': account['username'], 'account': account})

    if auth:
        @app.get('/login', response_class=HTMLResponse)
        def login_page(request: Request):
            return templates.TemplateResponse(request=request, name='login.html', context={'csrf_token': app.state.csrf})

        @app.post('/api/login')
        def login(body: dict, request: Request):
            token, status = auth.login(body.get('username'), body.get('password'), request.state.visitor_ip)
            if token is None:
                return JSONResponse({'detail': '账号或密码错误'}, status_code=status)
            response = JSONResponse({'ok': True, 'csrf': auth.session(token)['csrf']})
            response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite='strict', path='/')
            return response

        @app.post('/api/logout')
        def logout(request: Request):
            auth.logout(request.cookies[COOKIE])
            response = JSONResponse({'ok': True})
            response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
            return response

    @routes.get('/state')
    def get_state(request: Request, store=Depends(site_store)):
        return state(store, request.path_params.get('site_id', 'default'))

    @routes.get('/analytics')
    def analytics(request: Request, period: Literal['today', 'yesterday', '7d', '30d'] = '7d',
                  scope: Literal['current', 'all'] = 'current',
                  tz_offset: int = Query(default=480, ge=-840, le=840)):
        site_id = request.path_params.get('site_id', 'default')
        account = principal(request)
        everything = sees_everything(account)
        if scope != 'all' and 'site_id' not in request.path_params and not everything:
            raise HTTPException(404, '请使用当前域名的操作入口')
        if scope != 'all' or 'site_id' in request.path_params:
            authorized_site(request, site_id)
        return summarize(registry, site_id, period, scope, tz_offset, time.time(), None if everything else account['id'])

    @routes.put('/config')
    def update_config(body: ConfigUpdate, request: Request, store=Depends(site_store)):
        site_id = request.path_params.get('site_id', 'default')
        store.save_config(body.config, body.revision)
        result = state(store, site_id)
        purged = purge_site_cache(registry.get(site_id))
        if purged is not None:
            result['cloudflare'] = purged
        return result

    @routes.post('/upload/{slot}')
    async def upload(slot: Slot, request: Request, name: str = Query(min_length=1, max_length=200), store=Depends(site_store)):
        data = await request.body()
        # CPU/disk work in worker thread keeps health and UI requests responsive.
        from starlette.concurrency import run_in_threadpool
        version = await run_in_threadpool(import_content, data, name, store.pages)
        await run_in_threadpool(store.add_version, slot, version)
        return {'version': version | {'slot': slot}}

    @routes.post('/publish/{slot}')
    def publish(slot: Slot, body: VersionChoice, request: Request, store=Depends(site_store)):
        store.publish(slot, body.version_id)
        result = {'ok': True}
        purged = purge_site_cache(registry.get(request.path_params.get('site_id', 'default')))
        if purged is not None:
            result['cloudflare'] = purged
        return result

    @routes.delete('/versions/B/{version_id}')
    def delete_b_version(version_id: VersionId, store=Depends(site_store)):
        store.delete_version(version_id)
        return {'ok': True}

    @routes.post('/versions/B/cleanup')
    def cleanup_b_versions(store=Depends(site_store)):
        return {'deleted': len(store.delete_unpublished_b_versions())}

    def source_version(store, slot, version_id):
        version = store.version(version_id)
        if not version or version['slot'] != slot:
            raise HTTPException(404, '版本不存在或槽位不符')
        return version

    app.state.link_checker = LinkChecker()
    app.state.trust_checker = TrustChecker(device_from_environ())
    from .desk import DeskError, SheetWriter, buyer_quote, review_tickets
    from .desk_window import WorkOrderWindow
    app.state.desk_reader = WorkOrderWindow(Path(data_dir) / 'desk-browser', timeout=180)
    app.state.sheet_writer = SheetWriter(os.environ.get('AB_GOOGLE_SHEETS_TOKEN', ''))

    def redirect_commit_guard(request, store):
        site_id = request.path_params.get('site_id', 'default')
        expected_site = authorized_site(request, site_id)
        account_id = principal(request)['id']
        token = request.cookies.get(COOKIE)

        def guard(db):
            # Lock identity and ownership with the business commit. A check can
            # take seconds; transfers or revoked sessions must win immediately.
            db.execute('ATTACH DATABASE ? AS redirect_registry', (str(registry.path),))
            identity = 'main'
            if auth and accounts.store.path != store.path:
                db.execute('ATTACH DATABASE ? AS redirect_identity', (str(accounts.store.path),))
                identity = 'redirect_identity'
            db.execute('BEGIN IMMEDIATE')
            if auth:
                session = db.execute(f'''SELECT a.id,a.role FROM {identity}.account_sessions s JOIN {identity}.accounts a
                    ON a.id=s.account_id WHERE s.token_hash=? AND s.expires>? AND a.enabled=1''', (auth.key(token or ''), time.time())).fetchone()
                if not session or session['id'] != account_id:
                    raise HTTPException(401, '登录已失效，请重新登录')
                role = session['role']
            else:
                role = 'admin'
            current = db.execute('SELECT owner_id,owner_epoch FROM redirect_registry.sites WHERE id=?', (site_id,)).fetchone()
            if not current or current['owner_epoch'] != expected_site['owner_epoch'] or (role != 'admin' and current['owner_id'] != account_id):
                raise HTTPException(404, '站点归属已改变，请刷新域名列表')
        return guard

    @routes.get('/b-redirects')
    def get_b_redirects(version_id: str | None = Query(default=None, pattern=r'^[a-f0-9]{32}$'), store=Depends(site_store)):
        published = store.slots()['B']
        version_id = version_id or published
        base, occurrences, warnings = None, [], []
        if version_id:
            base = source_version(store, 'B', version_id)
            occurrences, warnings = scan_bundle(store.pages / version_id, version_id)
        return {'version':base, 'published_version':published, 'occurrences':public_occurrences(occurrences),
                'warnings':warnings, 'presets':store.redirect_presets(), 'numbers':store.whatsapp_numbers(),
                'trust_poll':store.whatsapp_trust_poll_enabled(),
                'active':store.redirect_active(), 'split':store.redirect_split(), 'number_split':store.whatsapp_split()}

    @routes.put('/b-redirects/split')
    def save_b_redirect_split(body: RedirectSplit, store=Depends(site_store)):
        return {'split': store.save_redirect_split(body.enabled, body.mode, body.members)}

    @routes.post('/b-redirects/presets')
    def add_b_redirect_presets(body: RedirectPresetInput, store=Depends(site_store)):
        return {'presets':store.add_redirect_presets(body.urls, body.note)}

    @routes.delete('/b-redirects/presets/{preset_id}')
    def delete_b_redirect_preset(preset_id: int, store=Depends(site_store)):
        store.delete_redirect_preset(preset_id)
        return {'ok':True}

    @routes.post('/b-redirects/presets/{preset_id}/check')
    def check_b_redirect_preset(preset_id: int, request: Request, store=Depends(site_store)):
        guard = redirect_commit_guard(request, store)
        preset = store.redirect_preset(preset_id)
        result = app.state.link_checker.check(preset['url'])
        return {'preset':store.save_redirect_check(preset, result, guard)}

    @routes.post('/b-redirects/apply')
    def apply_b_redirects(body: RedirectApply, request: Request, store=Depends(site_store)):
        guard = redirect_commit_guard(request, store)
        base = source_version(store, 'B', body.version_id)
        if store.slots()['B'] != body.expected_published:
            raise Conflict('当前 B 发布版本已变化，请重新扫描后再换链')
        occurrences, _ = scan_bundle(store.pages / base['id'], base['id'])
        chosen = [item for item in occurrences if item['id'] in set(body.occurrence_ids)]
        if len(chosen) != len(body.occurrence_ids):
            raise Conflict('源码或跳转位置已变化，请重新扫描后再换链')
        if any(item['kind'] == 'whatsapp_number' for item in chosen):
            raise ValueError('WhatsApp 号码请用号码预设更换')
        preset = store.redirect_preset(body.preset_id)
        result = app.state.link_checker.check(preset['url'])
        store.save_redirect_check(preset, result, guard)
        if result['status'] == 'abnormal':
            raise ValueError('此链接不正常：' + result['detail'])
        version, changed = store.apply_redirects(base, preset, body.occurrence_ids, body.expected_published, guard)
        return {'version':version, 'check':result, 'changed':changed}

    @routes.post('/b-redirects/numbers')
    def add_whatsapp_numbers(body: WhatsAppNumberInput, store=Depends(site_store)):
        return {'numbers':store.add_whatsapp_numbers(body.phones, body.note, body.display_name)}

    @routes.post('/b-redirects/numbers/reception')
    def publish_whatsapp_reception(body: WhatsAppReception, request: Request, store=Depends(site_store)):
        guard = redirect_commit_guard(request, store)
        number = store.upsert_whatsapp_name(body.phone, body.display_name)
        base = source_version(store, 'B', body.version_id)
        if store.slots()['B'] != body.expected_published:
            raise Conflict('当前 B 发布版本已变化，请重新扫描后再换号')
        version, changed = store.publish_reception(base, body.display_name, body.expected_published, guard)
        return {'number': number, 'version': version, 'changed': changed, 'sentence': f'本次由助理{body.display_name} 接待'}

    @routes.post('/b-redirects/direct-entry')
    def publish_direct_entry(body: DirectEntry, request: Request, store=Depends(site_store)):
        guard = redirect_commit_guard(request, store)
        base = source_version(store, 'B', body.version_id)
        if store.slots()['B'] != body.expected_published:
            raise Conflict('当前 B 发布版本已变化，请重新扫描后再换号')
        version, changed = store.publish_direct_entry(base, body.expected_published, guard)
        site = registry.get(request.path_params.get('site_id', 'default'))
        purged = None
        if changed and site.get('cf_zone_id') and cloudflare.configured:
            try:
                cloudflare.purge(site['cf_zone_id'], site['domain'])
                purged = {'ok': True}
            except ProvisioningError as error:
                purged = {'ok': False, 'detail': str(error)[:180]}
        return {'version': version, 'changed': changed, 'cloudflare': purged}

    @routes.put('/b-redirects/numbers/split')
    def save_whatsapp_number_split(body: WhatsAppSplit, request: Request, store=Depends(site_store)):
        saved = store.save_whatsapp_split(body.enabled, body.mode, body.members)
        result = {'number_split': saved}
        # The landing stays cached for the speed rule. Drop that copy so the
        # shared picker script lists the pool that was just saved.
        purged = purge_site_cache(registry.get(request.path_params.get('site_id', 'default')))
        if purged is not None:
            result['cloudflare'] = purged
        return result

    @routes.delete('/b-redirects/numbers/{number_id}')
    def delete_whatsapp_number(number_id: int, store=Depends(site_store)):
        store.delete_whatsapp_number(number_id)
        return {'ok':True}

    @routes.post('/wa-trust/screen/{token}')
    async def ingest_whatsapp_screen(token: str, request: Request):
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}', token):
            raise HTTPException(404, '检测已结束')
        raw = await request.body()
        if not app.state.trust_checker.deliver(token, raw.decode('utf-8', 'replace')):
            raise HTTPException(404, '检测已结束')
        return {'ok': True}

    @routes.post('/b-redirects/numbers/{number_id}/trust')
    def check_whatsapp_trust(number_id: int, store=Depends(site_store)):
        number = store.whatsapp_number(number_id)

        def persist(outcome):
            return store.save_whatsapp_trust(number_id, outcome['status'], outcome.get('reason') or '')

        result = app.state.trust_checker.check(number['phone'], persist)
        if result.get('busy'):
            return {'number':store.whatsapp_number(number_id), 'busy':True, 'poll_enabled':store.whatsapp_trust_poll_enabled()}
        return {'number':result['number'], 'busy':False, 'poll_enabled':store.whatsapp_trust_poll_enabled()}

    @routes.post('/desk/review')
    async def review_desk(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, '工单数据无法读取')
        try:
            return await asyncio.to_thread(review_tickets, body.get('tickets'), str(body.get('phone') or ''), app.state.desk_reader)
        except DeskError as error:
            raise HTTPException(400, str(error)) from None

    @routes.get('/desk/screen')
    def desk_screen():
        snap = getattr(app.state.desk_reader, 'snapshot', None)
        frame = snap() if snap else b''
        if not frame:
            return Response(status_code=204)
        return Response(frame, media_type='image/png', headers={'Cache-Control': 'no-store'})

    @routes.post('/desk/screen/click')
    async def desk_screen_click(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, '点击位置不对')
        x, y = body.get('x'), body.get('y')
        if isinstance(x, bool) or isinstance(y, bool):
            raise HTTPException(400, '点击位置不对')
        try:
            point = (float(x), float(y))
        except (TypeError, ValueError):
            raise HTTPException(400, '点击位置不对') from None
        pointer = getattr(app.state.desk_reader, 'pointer', None)
        if pointer is None:
            raise HTTPException(400, '当前没有窗口')
        try:
            pointer(point[0], point[1])
        except DeskError as error:
            raise HTTPException(400, str(error)) from None
        return {'ok': True}

    @routes.post('/desk/quote')
    async def quote_desk(request: Request):
        body = await request.json()
        try:
            return buyer_quote(body)
        except DeskError as error:
            raise HTTPException(400, str(error)) from None

    @routes.post('/desk/sheet')
    async def write_desk_sheet(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, '金额内容无法读取')
        try:
            quote = buyer_quote(body)
            result = app.state.sheet_writer.append(str(body.get('spreadsheet') or ''), quote['row'])
        except DeskError as error:
            raise HTTPException(400, str(error)) from None
        return {'ok': True, 'text': quote['text'], 'row': quote['row'], 'updated': result.get('updated', 1)}

    @routes.post('/b-redirects/numbers/apply')
    def apply_whatsapp_numbers(body: WhatsAppNumberApply, request: Request, store=Depends(site_store)):
        guard = redirect_commit_guard(request, store)
        base = source_version(store, 'B', body.version_id)
        if store.slots()['B'] != body.expected_published:
            raise Conflict('当前 B 发布版本已变化，请重新扫描后再换号')
        occurrences, _ = scan_bundle(store.pages / base['id'], base['id'])
        chosen = [item for item in occurrences if item['id'] in set(body.occurrence_ids)]
        if len(chosen) != len(body.occurrence_ids):
            raise Conflict('源码或跳转位置已变化，请重新扫描后再换号')
        if any(item['kind'] != 'whatsapp_number' for item in chosen):
            raise ValueError('号码预设只能更换 WhatsApp 号码')
        number = store.whatsapp_number(body.number_id)
        version, changed = store.apply_whatsapp_number(base, number, body.occurrence_ids, body.expected_published, guard)
        site = registry.get(request.path_params.get('site_id', 'default'))
        purged = None
        if changed and site.get('cf_zone_id') and cloudflare.configured:
            try:
                cloudflare.purge(site['cf_zone_id'], site['domain'])
                purged = {'ok': True}
            except ProvisioningError as error:
                purged = {'ok': False, 'detail': str(error)[:180]}
        return {'version':version, 'changed':changed, 'cloudflare':purged}

    @routes.get('/tracking')
    def get_tracking(store=Depends(site_store)):
        return {'snippets': registry.tracking_snippets(), 'published': store.published_tracking()}

    @routes.post('/tracking')
    def add_tracking(body: TrackingSnippetInput, store=Depends(site_store)):
        label, snippet = normalize_ga4(body.body) if body.kind == 'ga4' else normalize_conversion(body.body)
        return {'snippet': registry.add_tracking_snippet(body.kind, label, snippet, body.note)}

    @routes.delete('/tracking/{snippet_id}')
    def delete_tracking(snippet_id: int, store=Depends(site_store)):
        registry.delete_tracking_snippet(snippet_id)
        return {'ok': True}

    @routes.post('/tracking/apply')
    def apply_tracking(body: TrackingApply, request: Request, store=Depends(site_store)):
        if body.ga4_id is None and body.conversion_id is None:
            raise ValueError('请选择要写入的 GA4 或转化代码')
        ga4 = registry.tracking_snippet(body.ga4_id) if body.ga4_id else None
        conversion = registry.tracking_snippet(body.conversion_id) if body.conversion_id else None
        if ga4 and ga4['kind'] != 'ga4':
            raise ValueError('选中的不是 GA4 代码')
        if conversion and conversion['kind'] != 'conversion':
            raise ValueError('选中的不是转化代码')
        version = store.apply_tracking(body.slot, ga4['body'] if ga4 else None, conversion['body'] if conversion else None, body.expected_published)
        site = registry.get(request.path_params.get('site_id', 'default'))
        purged = None
        if site.get('cf_zone_id') and cloudflare.configured:
            try:
                cloudflare.purge(site['cf_zone_id'], site['domain'])
                purged = {'ok': True}
            except ProvisioningError as error:
                purged = {'ok': False, 'detail': str(error)[:180]}
        return {'version': version, 'published': store.published_tracking(), 'cloudflare': purged}

    @routes.get('/source/{slot}/{version_id}')
    def get_source(slot: Slot, version_id: VersionId, path: str | None = None, store=Depends(site_store)):
        source_version(store, slot, version_id)
        try:
            files = source_files(store.pages / version_id)
            if path is not None:
                data = {'path': path, 'content': read_source(editable_file(files, path), path).decode('utf-8-sig')}
            else:
                data = {'files': [{'path': name, 'bytes': size} for name, (_, size) in sorted(files.items()) if Path(name).suffix.lower() in EDITABLE_EXTENSIONS]}
        except FileNotFoundError:
            raise HTTPException(404, '源码文件不存在') from None
        return data | {'published_version': store.slots()[slot]}

    @routes.post('/source/{slot}/{version_id}')
    def save_source(slot: Slot, version_id: VersionId, body: SourceEdit, store=Depends(site_store)):
        base = source_version(store, slot, version_id)
        try:
            version = store.save_source(slot, base, body.path, body.content, body.expected_published)
        except FileNotFoundError:
            raise HTTPException(404, '源码文件不存在') from None
        return {'version': version, 'published_version': version['id']}

    @routes.post('/preview')
    def preview(body: VersionChoice, request: Request, store=Depends(site_store)):
        if not store.version(body.version_id):
            raise HTTPException(404, '版本不存在')
        expiry = int(time.time()) + 300
        site_id = request.path_params.get('site_id', 'default')
        issuer = principal(request)['id'] if auth else 'local'
        signature = preview_signature(store, accounts.store, registry.get(site_id), expiry, body.version_id, issuer, not deployment)
        if signature is None:
            raise HTTPException(404, '预览不存在或已过期')
        prefix = '' if site_id == 'default' else f'/_sites/{site_id}'
        return {'url': f'{target_url}{prefix}/_preview/{expiry}/{body.version_id}/{issuer}/{signature}/index.html'}

    @routes.post('/links')
    def add_links(body: LinkInput, store=Depends(site_store)):
        store.add_links(body.slot, body.urls)
        return {'ok': True}

    @routes.delete('/links/{link_id}')
    def delete_link(link_id: int, store=Depends(site_store)):
        store.delete_link(link_id)
        return {'ok': True}

    @routes.post('/counters/reset')
    def reset_counts(store=Depends(site_store)):
        store.reset_counts()
        return {'ok': True}

    @routes.post('/simulate')
    def simulate(body: Visitor, store=Depends(site_store)):
        config, _ = store.config()
        return decide(config, body)

    @routes.get('/logs')
    def logs(days: int = Query(default=7, ge=1, le=30), slot: Literal['', 'A', 'B'] = '', page: int = Query(default=1, ge=1, le=10000), store=Depends(site_store)):
        return _present_logs(store.logs(days, slot, page))

    @routes.get('/logs/rate')
    def log_rate(store=Depends(site_store)):
        return {'count': store.recent_count(60), 'seconds': 60}

    @routes.post('/logs/clear')
    def clear_logs(store=Depends(site_store)):
        return {'deleted': store.clear_logs()}

    @routes.post('/logs/clear-foreign')
    def clear_foreign_logs(store=Depends(site_store)):
        return store.clear_logs_unless(displayed_hong_kong)

    @routes.get('/logs.csv')
    def export_logs(days: int = Query(default=7, ge=1, le=30), slot: Literal['', 'A', 'B'] = '', store=Depends(site_store)):
        fields = ['id', 'created', 'country', 'ip', 'device', 'device_name', 'device_model', 'os', 'browser', 'slot', 'reason', 'path', 'mode']
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(fields)
        for row in _present_logs(store.logs(days, slot, page_size=10000))['items']:
            details = row.get('device_details') or {}
            if '/' in str(row.get('ip') or ''):
                row['ip'] = ''
            row.update(device_name=details.get('device', ''), device_model=details.get('model', ''), os=details.get('os', ''), browser=details.get('browser', ''))
            values = [str(row.get(key) or '') for key in fields]
            writer.writerow(["'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for value in values])
        return Response('\ufeff' + stream.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="ab-lab-visits.csv"'})

    @routes.get('/audit')
    def audit(request: Request, store=Depends(site_store)):
        items = store.audit()
        if not sees_everything(principal(request)):
            site_actions = {'config_updated', 'content_imported', 'version_published',
                            'counters_reset', 'logs_cleared', 'logs_foreign_cleared', 'links_added', 'link_deleted', 'b_redirect_presets_added', 'b_redirect_preset_deleted', 'b_version_deleted', 'b_redirect_split_updated', 'whatsapp_numbers_added', 'whatsapp_number_deleted', 'whatsapp_trust_checked', 'whatsapp_split_updated'}
            items = [item for item in items if item['action'] in site_actions]
        return {'items': items}

    app.include_router(routes, prefix='/api/sites/{site_id}')
    app.include_router(routes, prefix='/api')
    return app


def directory_redirect(store, version_ids, path, request):
    """Canonicalize before counting; the final directory document counts once."""
    if not path or path.endswith('/'):
        return None
    for version_id in version_ids:
        if not version_id:
            continue
        root = store.pages / version_id
        try:
            resolved = resolve_file(root, path)
        except (ValueError, FileNotFoundError):
            continue
        if (root / path).is_dir() and resolved.name == 'index.html':
            url = quote(getattr(request.state, 'public_prefix', '') + request.url.path + '/', safe='/')
            if request.url.query:
                url += '?' + request.url.query
            return RedirectResponse(url, status_code=307)
    return None


def cached_split_scan(root, version_id):
    with _split_scan_lock:
        cached = _split_scans.get(version_id)
    if cached is not None:
        return cached
    try:
        occurrences, _ = scan_bundle(root, version_id)
    except ValueError:
        occurrences = []
    with _split_scan_lock:
        if len(_split_scans) > 16:
            _split_scans.clear()
        _split_scans[version_id] = occurrences
    return occurrences


def assign_visitor_page(data, relative, occurrences, link_url, number):
    """One pass so link offsets and number offsets stay on the original file."""
    positions = [item for item in occurrences if item['path'] == relative]
    chosen = []
    for item in positions:
        if number and item['kind'] == 'whatsapp_number':
            chosen.append(dict(item, _assign=number['phone']))
        elif link_url and item['kind'] != 'whatsapp_number':
            chosen.append(dict(item, _assign=link_url))
    if chosen:
        data = rewrite_text(data, chosen, link_url or number['phone'])
    if number and number.get('display_name'):
        data = rewrite_reception_bytes(data, number['display_name'])
    return data


_NUMBER_SPLIT_SCRIPT = (
    '<script id="ab-number-split">(function(){'
    'var spec=__SPEC__;'
    'var pool=spec.pool||[];'
    'if(!pool.length)return;'
    'var key="ab_wa_pick",saved="",pick=null,i;'
    'try{saved=localStorage.getItem(key)||"";}catch(e){}'
    'for(i=0;i<pool.length;i++){if(pool[i].phone===saved){pick=pool[i];break;}}'
    'if(!pick){'
    'if(spec.mode==="weighted"){'
    'var total=0,j,draw,covered=0;'
    'for(j=0;j<pool.length;j++)total+=pool[j].weight||0;'
    'if(total>0){draw=Math.floor(Math.random()*total);'
    'for(j=0;j<pool.length;j++){covered+=pool[j].weight||0;if(draw<covered){pick=pool[j];break;}}}'
    '}'
    'if(!pick)pick=pool[Math.floor(Math.random()*pool.length)];'
    'try{localStorage.setItem(key,pick.phone);}catch(e){}'
    '}'
    'var phone=String(pick.phone||"").replace(/\\D/g,"");'
    'var name=pick.name||"";'
    'try{if(typeof CONFIG==="object"&&CONFIG){CONFIG.whatsappNumber=phone;if(name)CONFIG.receptionist=name;}}catch(e){}'
    'var links=document.querySelectorAll("a[href]");'
    'for(var n=0;n<links.length;n++){'
    'var href=links[n].getAttribute("href")||"";'
    'var next=href.replace(/wa\\.me\\/\\d{8,15}/g,"wa.me/"+phone).replace(/([?&]phone=)\\d{8,15}/g,"$1"+phone);'
    'if(next!==href)links[n].setAttribute("href",next);'
    '}'
    'if(name){var node=document.getElementById("reception-text");'
    'if(node)node.textContent="\\u672c\\u6b21\\u7531\\u52a9\\u7406"+name+" \\u63a5\\u5f85";}'
    '})();</script>'
)


def number_split_script(members, mode):
    """One script for every visitor. The cached page can still give each phone its own jump."""
    pool = []
    for item in members:
        phone = re.sub(r'\D', '', str(item.get('phone') or ''))
        if not phone:
            continue
        try:
            weight = int(item.get('weight') or 1)
        except (TypeError, ValueError):
            weight = 1
        pool.append({'phone': phone, 'name': str(item.get('display_name') or ''), 'weight': weight if weight > 0 else 1})
    if not pool:
        return ''
    spec = json.dumps(
        {'mode': 'weighted' if mode == 'weighted' else 'random', 'pool': pool},
        ensure_ascii=False, separators=(',', ':'),
    )
    spec = spec.replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')
    spec = spec.replace('\u2028', r'\u2028').replace('\u2029', r'\u2029')
    return _NUMBER_SPLIT_SCRIPT.replace('__SPEC__', spec)


def inject_number_split(data, members, mode):
    script = number_split_script(members, mode)
    if not script:
        return data
    bom = data.startswith(b'\xef\xbb\xbf')
    raw = data[3:] if bom else data
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return data
    if 'id="ab-number-split"' in text:
        return data
    match = None
    for found in re.finditer(r'</body\s*>', text, re.IGNORECASE):
        match = found
    index = match.start() if match else -1
    updated = text + script if index < 0 else text[:index] + script + text[index:]
    payload = updated.encode('utf-8')
    return (b'\xef\xbb\xbf' + payload) if bom else payload


def prepare_number_split(store, request, version_id):
    split = store.whatsapp_split()
    if not version_id or not split['enabled']:
        return None
    token = request.cookies.get(WA_COOKIE, '')
    is_new = not re.fullmatch(r'[a-f0-9]{64}', token)
    if is_new:
        token = secrets.token_hex(32)
    number = store.whatsapp_split_destination(hmac.new(store.secret, token.encode(), hashlib.sha256).hexdigest())
    if not number:
        return None
    return {
        'number': number,
        'occurrences': cached_split_scan(store.pages / version_id, version_id),
        'is_new': is_new,
        'token': token,
        'members': split['members'],
        'mode': split['mode'],
    }


def prepare_split(store, request, version_id):
    if not version_id or not store.redirect_split()['enabled']:
        return None
    token = request.cookies.get(SPLIT_COOKIE, '')
    is_new = not re.fullmatch(r'[a-f0-9]{64}', token)
    if is_new:
        token = secrets.token_hex(32)
    url = store.split_destination(hmac.new(store.secret, token.encode(), hashlib.sha256).hexdigest())
    if not url:
        return None
    occurrences = cached_split_scan(store.pages / version_id, version_id)

    def transform(data, relative):
        positions = [item for item in occurrences if item['path'] == relative and item['kind'] != 'whatsapp_number']
        if not positions:
            return data
        try:
            return rewrite_text(data, positions, url)
        except ValueError:
            return data

    return {'transform': transform, 'is_new': is_new, 'token': token, 'url': url}


def content_response(store, version_id, path, method='GET', transform=None):
    if not version_id:
        return HTMLResponse('<!doctype html><meta charset="utf-8"><h1>页面尚未发布</h1><p>请在管理后台为当前槽位导入并发布内容。</p>', status_code=503)
    try:
        root = store.pages / version_id
        file = resolve_file(root, path)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, '资源不存在') from None
    mime = {'.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}.get(file.suffix.lower()) or mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
    if transform:
        data = transform(file.read_bytes(), file.resolve().relative_to(root.resolve()).as_posix())
        payload = b'' if method == 'HEAD' else data
        length = len(data)
    else:
        payload = file.read_bytes() if method != 'HEAD' else b''
        length = file.stat().st_size if method == 'HEAD' else len(payload)
    return Response(payload, media_type=mime, headers={'Content-Length': str(length)})


def create_target(data_dir, port=8766, deployment=None, registry_dir=None):
    app, store = base_app(), Store(data_dir)
    account_store = store
    registry = Registry(data_dir, deployment.host(False) if deployment else '', registry_dir=registry_dir)
    app.add_middleware(LocalBoundary, port=port, deployment=deployment, registry=registry)

    def site_store(request: Request):
        return registry.store(request.state.site_id)

    @app.get('/.well-known/ab-lab-route/{site_id}')
    def route_proof(site_id: str, request: Request):
        if (not re.fullmatch('[a-f0-9]{32}', site_id)
                or request.state.site_id != site_id):
            raise HTTPException(404, '路由证明不存在')
        site = registry.get(site_id)
        return {'schema': 1, 'site_id': site_id, 'domain': site['domain']}

    @app.api_route('/_preview/{expiry}/{version_id}/{issuer}/{signature}/{path:path}', methods=['GET', 'HEAD'])
    def preview(expiry: int, version_id: str, issuer: str, signature: str, path: str, request: Request, store=Depends(site_store)):
        expected = preview_signature(store, account_store, registry.get(request.state.site_id), expiry, version_id, issuer, not deployment)
        if expiry < time.time() or expiry > time.time() + 301 or expected is None or not secrets.compare_digest(signature, expected) or not store.version(version_id):
            raise HTTPException(404, '预览不存在或已过期')
        redirect = directory_redirect(store, [version_id], path, request)
        if redirect is not None:
            return redirect
        return content_response(store, version_id, path, request.method)

    @app.api_route('/{path:path}', methods=['GET', 'HEAD'])
    def target(path: str, request: Request, store=Depends(site_store)):
        if path.startswith(('api/', 'data/', '_preview/', '_sites/')) or path in ('docs', 'openapi.json', 'redoc', '_sites'):
            raise HTTPException(404, '资源不存在')
        # ASGI already decodes the URL once. Do not unquote again.
        from .archives import safe_parts
        try:
            safe_parts(path.rstrip('/') or 'index.html')
        except ValueError:
            raise HTTPException(404, '资源不存在') from None
        config, _ = store.config()
        if config.content_mode == 'PAGE':
            redirect = directory_redirect(store, store.slots().values(), path, request)
            if redirect is not None:
                return redirect
        document = not path or path.endswith('/') or Path(path).suffix.lower() in ('.html', '.htm') or request.headers.get('sec-fetch-dest') == 'document'
        increment = document and request.method == 'GET'
        ip = getattr(request.state, 'visitor_ip', request.client.host)
        count = store.count(ip, config.rules.window_hours, increment)
        country = geo_country(ip)
        visitor = Visitor(ip=ip, country=country, ua=request.headers.get('user-agent', '')[:1024], language=request.headers.get('accept-language', '')[:512], visits=count)
        decision = decide(config, visitor)
        if increment:
            store.event(ip, country, decision, '/' + path, config.routing)
        slot = decision['slot']
        if config.content_mode == 'LINK':
            if not document:
                raise HTTPException(404, '资源不存在')
            link = store.choose_link(slot, config.distribution, consume=increment)
            if link:
                return RedirectResponse(link['url'], status_code=302)
            return HTMLResponse('<meta charset="utf-8"><h1>当前槽位尚未配置链接</h1>', status_code=503)
        version_id = store.slots()[slot]
        prepared = prepare_split(store, request, version_id) if slot == 'B' else None
        numbered = prepare_number_split(store, request, version_id) if slot == 'B' else None

        def visitor_transform(data, relative):
            if numbered:
                try:
                    data = assign_visitor_page(data, relative, numbered['occurrences'], prepared['url'] if prepared else None, numbered['number'])
                except ValueError:
                    pass
                if Path(relative).suffix.lower() in ('.html', '.htm'):
                    data = inject_number_split(data, numbered['members'], numbered['mode'])
                return data
            return prepared['transform'](data, relative)

        transform = visitor_transform if (prepared or numbered) else None
        response = content_response(store, version_id, path, request.method, transform)
        if prepared and prepared['is_new']:
            response.set_cookie(SPLIT_COOKIE, prepared['token'], max_age=365 * 24 * 3600, secure=bool(deployment), httponly=True, samesite='lax', path='/')
        if numbered and numbered['is_new']:
            response.set_cookie(WA_COOKIE, numbered['token'], max_age=365 * 24 * 3600, secure=bool(deployment), httponly=True, samesite='lax', path='/')
        return response

    return app
